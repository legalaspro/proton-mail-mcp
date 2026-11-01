"""Build the wheel and prove the two-command install from it, touching nothing real.

Every command runs in a scratch directory with its own HOME and without a session bus,
with stand-ins for `systemctl`, `pgrep`, `secret-tool` and Bridge first on PATH, so no real
unit, process, keyring or Bridge is reached. The stand-in `systemctl` starts what the
installed units name, so the MCP server that answers the self-check is the wheel's own,
installed by `setup` into a real virtual environment, logging in to a local stand-in IMAP
server with the password typed at the hidden prompt.

uv is required, and pip and uv fetch the dependencies, so this needs network access and
is not part of `make check`. Nothing is ever uploaded. The full apply needs Linux.

    python scripts/wheel_smoke.py [--work DIR] [--python 3.12] [--wheel FILE] [--keep]
"""

import argparse
import functools
import http.server
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "smoke@proton.invalid"
# Throwaway values for the stand-ins; they exist only in the scratch directory.
PASSWORD = "smoke-bridge-password-Xq7"
NOT_AN_ADDRESS = "pasted-by-mistake-Zk3"
TOKEN_SERVICE = "proton-mail-mcp-token"
BRIDGE_SERVICE = "proton-mail-mcp"
# Well under the CI job's 10 minutes, so a hang still prints the transcript.
STEP_TIMEOUT = 300

SYSTEMCTL = r"""#!PYTHON
# Stand-in for `systemctl --user`: runs what the installed units name.
import os, shlex, signal, subprocess, sys, time
from pathlib import Path

state = Path(os.environ["PMM_SMOKE_STATE"])
units = state / "units"
units.mkdir(exist_ok=True)
with open(state / "calls.log", "a") as log:
    log.write("systemctl " + " ".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
assert args[0] == "--user", args


def pid_of(unit):
    try:
        pid = int((units / (unit + ".pid")).read_text())
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat") as status:
            return None if status.read().split(") ")[-1].startswith("Z") else pid
    except (OSError, ValueError):
        return None


def stop(unit):
    pid = pid_of(unit)
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if pid_of(unit) is None:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    (units / (unit + ".pid")).unlink(missing_ok=True)


def start(unit):
    if pid_of(unit):
        return
    path = Path((units / (unit + ".enabled")).read_text())
    for line in path.read_text().splitlines():
        if line.startswith("ExecStart="):
            argv = shlex.split(line[len("ExecStart="):])
    output = open(state / (unit + ".log"), "a")
    child = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True
    )
    (units / (unit + ".pid")).write_text(str(child.pid))


command = args[1]
if command == "show-environment":
    print("HOME=" + os.environ["HOME"])
elif command == "show":
    prop, unit = args[2], args[-1]
    if prop == "--property=FragmentPath":
        enabled = units / (unit + ".enabled")
        print(enabled.read_text() if enabled.exists() else "")
    elif prop == "--property=ActiveState":
        print("active" if pid_of(unit) else "inactive")
elif command == "enable":
    for path in args[2:]:
        (units / (Path(path).name + ".enabled")).write_text(path)
elif command == "stop":
    stop(args[2])
elif command == "disable":
    for unit in [name for name in args[2:] if name != "--now"]:
        stop(unit)
        (units / (unit + ".enabled")).unlink(missing_ok=True)
elif command == "start":
    start(args[2])
elif command == "restart":
    stop(args[2])
    start(args[2])
elif command != "daemon-reload":
    sys.exit(f"unexpected systemctl call: {args}")
"""

PGREP = r"""#!PYTHON
# Stand-in for pgrep: only the stand-in Bridge service counts, never a process on this host.
import os, sys
from pathlib import Path

state = Path(os.environ["PMM_SMOKE_STATE"])
with open(state / "calls.log", "a") as log:
    log.write("pgrep " + " ".join(sys.argv[1:]) + "\n")
try:
    os.kill(int((state / "units" / "proton-mail-mcp-bridge.service.pid").read_text()), 0)
except (OSError, ValueError):
    sys.exit(1)
"""

SECRET_TOOL = r"""#!PYTHON
# Stand-in for secret-tool: answers like the real one, with its silent exit 1 for "none".
import os, sys
from pathlib import Path

state = Path(os.environ["PMM_SMOKE_STATE"])
with open(state / "calls.log", "a") as log:
    log.write("secret-tool " + " ".join(sys.argv[1:]) + "\n")
with open(state / "secret-tool-env.log", "a") as log:
    log.write(repr(sorted(os.environ.items())) + "\n")
args = [arg for arg in sys.argv[1:] if not arg.startswith("--label=")]
command, values = args[0], dict(zip(args[1::2], args[2::2]))
secrets = state / "secrets"
secrets.mkdir(mode=0o700, exist_ok=True)
path = secrets / f"{values['service']}__{values['account']}"
if command == "store":
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, sys.stdin.read().encode())
    os.close(fd)
elif command == "lookup":
    if not path.exists():
        sys.exit(1)
    sys.stdout.write(path.read_text())
elif command == "clear":
    path.unlink(missing_ok=True)
else:
    sys.exit(f"unexpected secret-tool call: {args}")
"""

FAKE_BRIDGE = r"""#!PYTHON
# Stand-in for Bridge: --cli is a small prompt on the terminal; --noninteractive serves
# just enough IMAP over STARTTLS for count_mail, and checks the password it is given.
import os, socket, ssl, sys, threading
from pathlib import Path

state = Path(os.environ["PMM_SMOKE_STATE"])
if "--cli" in sys.argv:
    if (state / "units" / "proton-mail-mcp-bridge.service.pid").exists():
        (state / "cli-beside-bridge").write_text("")
        sys.exit("fake Bridge: another instance is running")
    print("fake Bridge CLI", flush=True)
    while True:
        print(">>> ", end="", flush=True)
        line = sys.stdin.readline().strip()
        if not line or line == "exit":
            break
        if line == "login":
            (state / "no-account").unlink(missing_ok=True)
        print(f"fake Bridge: did {line}", flush=True)
    raise SystemExit(0)

expected = (state / "expected-password").read_text()
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(state / "bridge-cert.pem", state / "bridge-key.pem")


def log(text):
    with open(state / "imap.log", "a") as handle:
        handle.write(text + "\n")


def serve(client):
    stream, wrapped = client.makefile("rwb"), client
    stream.write(b"* OK [CAPABILITY IMAP4rev1 STARTTLS] fake Bridge ready\r\n")
    stream.flush()
    selected = False
    while True:
        line = stream.readline()
        if not line:
            return
        parts = line.decode("utf-8", "replace").rstrip("\r\n").split(" ")
        tag, command = parts[0], parts[1].upper() if len(parts) > 1 else ""
        out = []
        if command == "CAPABILITY":
            out = [b"* CAPABILITY IMAP4rev1 STARTTLS", tag.encode() + b" OK done"]
        elif command == "STARTTLS":
            stream.write(tag.encode() + b" OK begin TLS\r\n")
            stream.flush()
            wrapped = context.wrap_socket(client, server_side=True)
            stream = wrapped.makefile("rwb")
            continue
        elif command == "LOGIN":
            given = parts[3].strip('"') if len(parts) > 3 else ""
            # Like the real one, it listens with no account logged in, and refuses logins.
            refused = (state / "reject-login").exists() or (state / "no-account").exists()
            if given == expected and not refused:
                log("LOGIN ok")
                out = [tag.encode() + b" OK logged in"]
            else:
                log("LOGIN refused")
                out = [tag.encode() + b" NO bad credentials"]
        elif command == "LIST":
            out = [b'* LIST (\\HasNoChildren) "/" "INBOX"', tag.encode() + b" OK done"]
        elif command in {"EXAMINE", "SELECT"}:
            log(command + " " + " ".join(parts[2:]))
            selected = True
            out = [
                b"* 3 EXISTS",
                b"* OK [UIDVALIDITY 7] ok",
                b"* OK [UIDNEXT 4] ok",
                tag.encode() + b" OK [READ-ONLY] done",
            ]
        elif command == "LOGOUT":
            stream.write(b"* BYE bye\r\n" + tag.encode() + b" OK done\r\n")
            stream.flush()
            return
        elif command == "NOOP":
            out = [tag.encode() + b" OK done"]
        else:
            log("unsupported " + command + (" selected" if selected else ""))
            out = [tag.encode() + b" BAD not in this stand-in"]
        stream.write(b"".join(item + b"\r\n" for item in out))
        stream.flush()


def guarded(client):
    try:
        serve(client)
    except (OSError, ValueError):
        pass
    finally:
        client.close()


server = socket.create_server(("127.0.0.1", int((state / "imap-port").read_text())))
while True:
    client, _address = server.accept()
    threading.Thread(target=guarded, args=(client,), daemon=True).start()
"""


class Failed(Exception):
    pass


class Smoke:
    def __init__(self, work: Path, python: str) -> None:
        self.work = work
        self.python = python
        self.home = work / "home"
        self.state = work / "state"
        self.shims = work / "shims"
        self.logs = work / "logs"
        for path in (self.home, self.state, self.shims, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        self.checks = 0
        self.env = self._env()

    def _env(self) -> dict[str, str]:
        """Only what a clean login needs: no session bus and no real configuration."""
        env = {"PATH": f"{self.shims}{os.pathsep}{os.environ.get('PATH', '')}"}
        for name in ("LANG", "LC_ALL", "TERM", "SSL_CERT_FILE"):
            if name in os.environ:
                env[name] = os.environ[name]
        env.update(
            HOME=str(self.home),
            TMPDIR=str(self.work / "tmp"),
            PMM_SMOKE_STATE=str(self.state),
            UV_CACHE_DIR=os.environ.get("UV_CACHE_DIR", str(self.work / "uv-cache")),
            UV_PYTHON=self.python,
            UV_NO_PROGRESS="1",
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            PIP_CACHE_DIR=str(self.work / "pip-cache"),
        )
        (self.work / "tmp").mkdir(exist_ok=True)
        return env

    def check(self, passed: object, what: str) -> None:
        self.checks += 1
        if not passed:
            raise Failed(what)
        print(f"  ok {self.checks}: {what}", flush=True)

    def run(self, *argv: object, expect: int = 0, env: dict | None = None, timeout=STEP_TIMEOUT):
        """Run with no terminal; returns stdout + stderr, and checks the exit status."""
        words = [str(word) for word in argv]
        result = subprocess.run(
            words,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env={**self.env, **(env or {})},
            timeout=timeout,
            check=False,
            cwd=self.work,
        )
        output = result.stdout + result.stderr
        if result.returncode != expect:
            raise Failed(
                f"{shlex_join(words)}: exit {result.returncode}, wanted {expect}\n{output}"
            )
        return output

    def in_terminal(self, *argv: object, script=(), log="terminal", timeout=STEP_TIMEOUT):
        """Run on a real pty, answering each (expected text, reply) in order."""
        words = [str(word) for word in argv]
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(self.work)
            os.execvpe(words[0], words, self.env)
        transcript = ""
        seen = 0
        pending = list(script)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if time.monotonic() > deadline:
                    raise Failed(f"{shlex_join(words)}: timed out\n{transcript[-2000:]}")
                ready, _, _ = select.select([master], [], [], 1.0)
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    transcript += data.decode("utf-8", "replace")
                while pending and pending[0][0] in transcript[seen:]:
                    wanted, reply = pending.pop(0)
                    seen = transcript.index(wanted, seen) + len(wanted)
                    os.write(master, reply.encode() + b"\n")
        finally:
            os.close(master)
            (self.logs / f"{log}.log").write_text(transcript)
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        if pending:
            raise Failed(f"never asked {pending[0][0]!r}; exit {code}\n{transcript[-3000:]}")
        return code, transcript

    # Phases --------------------------------------------------------------------

    def build(self) -> Path:
        out = self.work / "dist"
        self.run("uv", "build", "--wheel", "--out-dir", out, ROOT)
        wheels = sorted(out.glob("proton_mail_mcp-*.whl"))
        self.check(len(wheels) == 1, f"uv build made one wheel: {wheels[0].name}")
        return wheels[0]

    def inspect(self, wheel: Path) -> None:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            info = f"proton_mail_mcp-{version_of(wheel)}.dist-info"
            metadata = archive.read(f"{info}/METADATA").decode()
            entry_points = archive.read(f"{info}/entry_points.txt").decode()
        package = ROOT / "src" / "proton_mail_mcp"
        expected = {
            path.relative_to(package.parent).as_posix()
            for path in package.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.check(expected <= names, "the wheel holds every file of the package")
        self.check(
            all(not name.startswith(("tests/", "scripts/", "docs/")) for name in names),
            "the wheel holds no tests, scripts or docs",
        )
        templates = "proton_mail_mcp/installer/service_templates/"
        self.check(
            {f"{templates}proton-mail-mcp.service", f"{templates}com.proton-mail-mcp.plist"}
            <= names,
            "the service unit and LaunchAgent templates are package data",
        )
        self.check(
            {
                "proton_mail_mcp/examples/com.proton-mail-mcp.plist",
                "proton_mail_mcp/examples/protected-senders.example.json",
            }
            <= names,
            "the examples are package data",
        )
        for field in (
            "Name: proton-mail-mcp",
            "License-Expression: Apache-2.0",
            "Classifier: License :: OSI Approved :: Apache Software License",
            "Author-email: Dmitri Manajev <dmitri@manajev.com>",
            "Requires-Python: >=3.12",
            "Classifier: Operating System :: POSIX :: Linux",
            "Project-URL: Repository, https://github.com/legalaspro/proton-mail-mcp",
            "Description-Content-Type: text/markdown",
        ):
            self.check(field in metadata, f"metadata has {field}")
        self.check(
            "proton-mail-mcp = proton_mail_mcp.cli:main" in entry_points
            and "proton-mail-mcp-service = proton_mail_mcp.service_entry:main" in entry_points,
            "the entry points are the dispatcher and the unchanged service launcher",
        )

    def stand_ins(self) -> None:
        for name, text in (
            ("systemctl", SYSTEMCTL),
            ("pgrep", PGREP),
            ("secret-tool", SECRET_TOOL),
            ("fake-bridge", FAKE_BRIDGE),
        ):
            path = self.shims / name
            path.write_text(text.replace("#!PYTHON", f"#!{sys.executable}", 1))
            path.chmod(0o755)
        (self.state / "expected-password").write_text(PASSWORD)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(self.state / "bridge-key.pem"),
                "-out",
                str(self.state / "bridge-cert.pem"),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
            ],  # fmt: skip
            capture_output=True,
            check=True,
        )
        found = shutil.which("systemctl", path=self.env["PATH"])
        self.check(found == str(self.shims / "systemctl"), "systemctl resolves to the stand-in")

    def dry_runs(self, tool: list[object], label: str) -> None:
        version = self.run(*tool, "--version")
        self.check("proton-mail-mcp 1.0.0" in version, f"{label}: --version says 1.0.0")
        report = json.loads(self.run(*tool, "detect", "--json"))
        self.check(
            report["env_file"].startswith(str(self.home)), f"{label}: detect uses the scratch HOME"
        )
        before = sorted(self.home.rglob("proton-mail-mcp*"))
        output = self.run(*tool, "setup", "--dry-run", "--skip-bridge-service", "--skip-bridge")
        self.check("Dry run: nothing was changed." in output, f"{label}: setup --dry-run finishes")
        self.check("you@proton.me" in output, f"{label}: the plan shows a placeholder username")
        self.check("reads mail through Bridge" in output, f"{label}: the plan shows the self-check")
        service = "Write the MCP user unit" if sys.platform == "linux" else "LaunchAgent"
        self.check(service in output, f"{label}: the plan writes the service")
        after = sorted(self.home.rglob("proton-mail-mcp*"))
        self.check(before == after, f"{label}: the dry run created nothing named for the package")
        for family in ("debian", "fedora", "arch", "macos", "windows"):
            output = self.run(*tool, "setup", "--dry-run", "--simulate", family)
            self.check("Dry run: nothing was changed." in output, f"{label}: simulated {family}")
        output = self.run(*tool, "uninstall", "--dry-run")
        self.check("Dry run: nothing was changed." in output, f"{label}: uninstall --dry-run")
        refused = self.run(*tool, "show-token", "--bridge-user", ACCOUNT, expect=2)
        self.check("only to a terminal" in refused, f"{label}: show-token refuses a pipe")

    def serve(self, wheel: Path) -> None:
        # The tool environment's own command: uvx keeps a parent process, and these
        # servers are stopped by exact PID.
        code = "import sys; print('PREFIX=' + sys.prefix)"
        output = self.run("uvx", "--from", wheel, "python", "-c", code)
        prefix = re.search(r"^PREFIX=(.+)$", output, re.MULTILINE).group(1)
        tool = [Path(prefix) / "bin" / "proton-mail-mcp"]
        refused = self.run(*tool, expect=2)
        self.check(
            "Cannot start proton-mail-mcp" in refused, "bare command without settings: exit 2"
        )
        self.check("proton-mail-mcp setup" in refused, "bare command points at setup")
        port = free_port()
        env = {
            **self.env,
            "PROTON_MCP_TOKEN": "t" * 40,
            "PROTON_MCP_PORT": str(port),
            "PROTON_BRIDGE_USER": ACCOUNT,
            "PROTON_BRIDGE_PASSWORD": PASSWORD,
            "PROTON_MCP_ACTION_LOG": str(self.work / "serve-state" / "actions.jsonl"),
            "PROTON_MCP_INDEX_AUTOREBUILD_ENABLED": "0",
        }
        for label, extra in (("bare command", []), ("serve", ["serve"])):
            words = [str(word) for word in [*tool, *extra]]
            server = subprocess.Popen(
                words,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=self.work,
            )
            try:
                status = unauthenticated_status(port, deadline=time.monotonic() + 60)
                self.check(status == 401, f"{label} starts the server: no token gets 401")
            finally:
                stop_exact(server)
        unknown = self.run(*tool, "bogus", expect=2)
        self.check("usage:" in unknown, "an unknown command prints usage and exits 2")

    def full_setup(self, tool: list[object]) -> None:
        imap_port, mcp_port = free_port(), free_port()
        (self.state / "imap-port").write_text(str(imap_port))
        bridge = self.shims / "fake-bridge"
        common = ["--bridge-bin", bridge, "--bridge-imap-port", imap_port, "--port", mcp_port]
        code, transcript = self.in_terminal(
            *tool,
            "setup",
            *common,
            script=[
                ("Bridge IMAP username", NOT_AN_ADDRESS),
                ("Bridge IMAP username", ACCOUNT),
                ("Folder for attachment files", ""),
                ("Store new ones", "y"),
                ("Apply these steps?", "y"),
                ("Open Bridge's CLI here", ""),
                (">>> ", "login"),
                (">>> ", "info"),
                (">>> ", "exit"),
                ("Bridge IMAP password for", PASSWORD),
                ("Repeat the Bridge password", PASSWORD),
            ],
            log="setup",
        )
        if code:
            raise Failed(f"setup exited {code}\n{transcript[-4000:]}")
        self.check(code == 0, "setup on a terminal exits 0")
        self.check(
            "fake Bridge: did login" in transcript, "setup opened Bridge's CLI on the terminal"
        )
        self.check(
            "ok: the MCP server on" in transcript and "enforces and accepts" in transcript,
            "self-check: the server enforces and accepts the stored token",
        )
        self.check("reads INBOX through Bridge" in transcript, "self-check: it reads INBOX")
        self.check("setup finished" in transcript, "setup reports it finished")
        secrets = self.state / "secrets"
        stored = secrets / f"{BRIDGE_SERVICE}__{ACCOUNT}"
        token_file = secrets / f"{TOKEN_SERVICE}__{ACCOUNT}"
        self.check(stored.read_text() == PASSWORD, "the typed password is in the credential store")
        self.check(
            stat.S_IMODE(stored.stat().st_mode) == 0o600, "the stand-in store file is owner-only"
        )
        token = token_file.read_text()
        self.check(len(token) >= 32, "a bearer token was generated and stored")
        imap_log = (self.state / "imap.log").read_text()
        self.check("LOGIN ok" in imap_log, "the server logged in to Bridge with the typed password")
        self.check('EXAMINE "INBOX"' in imap_log, "the probe read INBOX read-only")
        self.no_secret(transcript, token, "the setup transcript")
        self.check(
            transcript.count(NOT_AN_ADDRESS) == 1,
            "a rejected username appears only as what the terminal echoed",
        )
        unit_dir = self.home / ".config" / "systemd" / "user"
        units = {path.name for path in unit_dir.iterdir()}
        self.check(
            units == {"proton-mail-mcp.service", "proton-mail-mcp-bridge.service"},
            "both units are written under the scratch HOME",
        )
        venv = self.home / ".local" / "share" / "proton-mail-mcp" / "venv"
        unit = (unit_dir / "proton-mail-mcp.service").read_text()
        self.check(
            f"ExecStart={venv}/bin/proton-mail-mcp-service --env-file " in unit,
            "the unit keeps the existing service entry as ExecStart",
        )
        settings = self.home / ".config" / "proton-mail-mcp" / "proton-mail-mcp.env"
        self.check(
            stat.S_IMODE(settings.stat().st_mode) == 0o600, "the settings file is owner-only"
        )
        for path, what in (
            (settings, "the settings file"),
            (unit_dir / "proton-mail-mcp.service", "the MCP unit"),
            (unit_dir / "proton-mail-mcp-bridge.service", "the Bridge unit"),
            (self.state / "calls.log", "every systemctl and secret-tool argv"),
            (self.state / "secret-tool-env.log", "every secret-tool environment"),
        ):
            self.no_secret(path.read_text(), token, what)
            self.check(NOT_AN_ADDRESS not in path.read_text(), f"no rejected answer in {what}")
        for name, unit_name in (("mcp", "proton-mail-mcp.service"),
                                ("bridge", "proton-mail-mcp-bridge.service")):  # fmt: skip
            pid = int((self.state / "units" / f"{unit_name}.pid").read_text())
            for part in ("cmdline", "environ"):
                seen = Path(f"/proc/{pid}/{part}").read_bytes().decode("utf-8", "replace")
                self.no_secret(seen, token, f"the {name} service's {part}")
        python = venv / "bin" / "python"
        code_check = "import importlib.metadata as m; print(m.version('proton-mail-mcp'))"
        version = self.run(python, "-I", "-c", code_check)
        self.check(version.strip() == "1.0.0", "the private venv runs this version")
        self.check((venv / "bin" / "proton-mail-mcp").exists(), "the private venv has the command")
        base = (venv / "pyvenv.cfg").read_text().splitlines()[0]
        self.check(
            base.startswith("home = ") and self.env["UV_CACHE_DIR"] not in base,
            "the private venv does not depend on uvx's temporary environment",
        )
        self.rerun(tool, common, token, venv)
        self.login_around_service(tool, common)
        self.wrong_password(tool, common)
        self.token_and_uninstall(tool, token, venv, settings, unit_dir)

    def no_secret(self, text: str, token: str, where: str) -> None:
        self.check(PASSWORD not in text, f"no Bridge password in {where}")
        self.check(token not in text, f"no token in {where}")

    def rerun(self, tool: list[object], common: list[object], token: str, venv: Path) -> None:
        output = self.run(*tool, "setup", "--yes", *common)
        self.check("is already in libsecret; keeping it" in output, "a rerun keeps credentials")
        self.check("reads INBOX through Bridge" in output, "a rerun passes the self-check")
        self.check("not opened" in output, "a rerun does not open Bridge's CLI beside Bridge")
        stored = (self.state / "secrets" / f"{TOKEN_SERVICE}__{ACCOUNT}").read_text()
        self.check(stored == token, "a rerun keeps the token")
        self.no_secret(output, token, "the rerun output")
        direct = self.run(venv / "bin" / "proton-mail-mcp", "--version")
        self.check("1.0.0" in direct, "the private venv's command works without uvx")

    def login_around_service(self, tool: list[object], common: list[object]) -> None:
        """A rerun on a terminal, with the installer's own Bridge service running."""
        bridge = "proton-mail-mcp-bridge.service"
        calls = self.state / "calls.log"
        seen = len(calls.read_text().splitlines())
        code, _transcript = self.in_terminal(
            *tool,
            "setup",
            *common,
            script=[
                ("Folder for attachment files", ""),
                ("Apply these steps?", "y"),
                ("A Bridge password is stored", ""),
            ],
            log="setup-rerun-enter",
        )
        self.check(code == 0, "a rerun on a terminal exits 0")
        self.check(
            f"systemctl --user stop {bridge}" not in calls.read_text().splitlines()[seen:],
            "with a password stored, Enter stops nothing",
        )
        # The first setup's login was declined: no account, and no password was stored.
        no_account = self.state / "no-account"
        no_account.write_text("")
        (self.state / "secrets" / f"{BRIDGE_SERVICE}__{ACCOUNT}").unlink()
        self.run("systemctl", "--user", "restart", bridge)
        seen = len(calls.read_text().splitlines())
        code, transcript = self.in_terminal(
            *tool,
            "setup",
            *common,
            script=[
                ("Folder for attachment files", ""),
                ("Store new ones", "y"),
                ("Apply these steps?", "y"),
                ("No Bridge password is stored yet", ""),
                (">>> ", "login"),
                (">>> ", "info"),
                (">>> ", "exit"),
                ("Bridge IMAP password for", PASSWORD),
                ("Repeat the Bridge password", PASSWORD),
            ],
            log="setup-login-around-service",
        )
        if code:
            raise Failed(f"setup exited {code}\n{transcript[-4000:]}")
        recent = calls.read_text().splitlines()[seen:]
        stop = recent.index(f"systemctl --user stop {bridge}")
        self.check(
            f"systemctl --user start {bridge}" in recent[stop:],
            "the rerun stopped the Bridge service and started it again",
        )
        self.check("fake Bridge: did login" in transcript, "the rerun opened Bridge's CLI")
        self.check(
            not (self.state / "cli-beside-bridge").exists(), "the CLI never ran beside the service"
        )
        self.check(not no_account.exists(), "the CLI logged the account in")
        self.check(
            "reads INBOX through Bridge" in transcript, "the self-check then passes on that rerun"
        )

    def wrong_password(self, tool: list[object], common: list[object]) -> None:
        reject = self.state / "reject-login"
        reject.write_text("")
        try:
            output = self.run(*tool, "setup", "--yes", *common, expect=1)
        finally:
            reject.unlink()
        self.check(
            "cannot read INBOX through Bridge" in output, "a refused Bridge login fails the check"
        )
        self.check(PASSWORD not in output, "the failed check shows no password")

    def token_and_uninstall(
        self, tool: list[object], token: str, venv: Path, settings: Path, unit_dir: Path
    ) -> None:
        code, transcript = self.in_terminal(*tool, "show-token", log="show-token")
        self.check(code == 0 and token in transcript, "show-token prints the token to a terminal")
        code, transcript = self.in_terminal(
            venv / "bin" / "proton-mail-mcp", "show-token", log="show-token-venv"
        )
        self.check(code == 0 and token in transcript, "the venv's show-token works too")
        pids = [
            int((self.state / "units" / f"{unit}.pid").read_text())
            for unit in ("proton-mail-mcp.service", "proton-mail-mcp-bridge.service")
        ]
        output = self.run(*tool, "uninstall", "--dry-run")
        self.check(
            settings.exists() and all(alive(pid) for pid in pids), "uninstall --dry-run keeps"
        )
        output = self.run(*tool, "uninstall", "--yes")
        self.check("uninstall finished" in output, "uninstall finishes")
        self.check(not any(alive(pid) for pid in pids), "uninstall stopped both services")
        self.check(not settings.exists(), "uninstall removed the settings file")
        self.check(not list(unit_dir.iterdir()), "uninstall removed the unit files")
        self.check(
            (venv / "bin" / "python").exists(), "uninstall keeps the venv, as install.sh does"
        )
        stored = self.state / "secrets" / f"{BRIDGE_SERVICE}__{ACCOUNT}"
        self.check(stored.exists(), "uninstall keeps the credentials without the flag")
        output = self.run(
            *tool, "uninstall", "--yes", "--remove-credentials", "--bridge-user", ACCOUNT
        )
        self.check(not stored.exists(), "--remove-credentials removes them")
        self.no_secret(output, token, "the uninstall output")

    def pipx(self, wheel: Path) -> None:
        env = {
            "PIPX_HOME": str(self.work / "pipx-home"),
            "PIPX_BIN_DIR": str(self.work / "pipx-bin"),
            "PIPX_MAN_DIR": str(self.work / "pipx-man"),
        }
        self.run("uvx", "pipx", "install", "--python", self.python_command(), wheel, env=env)
        command = self.work / "pipx-bin" / "proton-mail-mcp"
        self.check(command.exists(), "pipx install puts the command on its bin directory")
        self.dry_runs([command], "pipx")
        output = self.run(command, "setup", "--dry-run", "--bridge-user", ACCOUNT, "--skip-bridge",
                          "--skip-bridge-service")  # fmt: skip
        self.check(str(wheel) in output, "from a local wheel, pip is pointed at that wheel")

    def named_installs(self, wheel: Path) -> None:
        """Installs by name leave no direct_url.json. pipx and uv keep the index they used,
        plain pip and uvx keep nothing, and only the installed files can prove the copy."""
        root = self.work / "named"
        files = root / "files"
        files.mkdir(parents=True)
        shutil.copy(wheel, files)
        other = root / "other"
        other.mkdir()
        with_another_file(wheel, other / wheel.name)
        for name, target in (("index", "files"), ("other-index", "other")):
            project = root / name / "proton-mail-mcp"
            project.mkdir(parents=True)
            project.joinpath("index.html").write_text(
                f'<!DOCTYPE html><a href="../../{target}/{wheel.name}">{wheel.name}</a>'
            )
        (root / "empty-index").mkdir()
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(QuietFiles, directory=str(root))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            self.pipx_by_name(base, root)
            self.uv_tool_by_name(base)
            self.pip_by_name(base, files)
            self.uvx_by_name(files)
        finally:
            server.shutdown()
            server.server_close()

    def apply_by_name(self, tool: list[object], label: str, expect: int = 0, env=None) -> str:
        """Only the private environment: no Bridge, service or credential is involved."""
        home = self.work / "named" / "homes" / label
        home.mkdir(parents=True)
        return self.run(
            *tool, "setup", "--yes", "--bridge-user", ACCOUNT, "--skip-bridge", "--skip-services",
            "--skip-credentials", expect=expect, env={"HOME": str(home), **(env or {})},
        )  # fmt: skip

    def pipx_by_name(self, base: str, root: Path) -> None:
        env = {
            "PIPX_HOME": str(self.work / "named" / "pipx-home"),
            "PIPX_BIN_DIR": str(self.work / "named" / "pipx-bin"),
            "PIPX_MAN_DIR": str(self.work / "named" / "pipx-man"),
        }
        self.run(
            "uvx", "pipx", "install", "--backend", "pip", "--python", self.python_command(),
            "--index-url", f"{base}/index/",
            "--pip-args=--extra-index-url https://pypi.org/simple", "proton-mail-mcp", env=env,
        )  # fmt: skip
        command = [self.work / "named" / "pipx-bin" / "proton-mail-mcp"]
        plan = self.run(
            *command, "setup", "--dry-run", "--bridge-user", ACCOUNT, "--skip-bridge",
            "--skip-bridge-service",
        )  # fmt: skip
        self.check(
            f"--index-url {base}/index/ --extra-index-url https://pypi.org/simple "
            "proton-mail-mcp==1.0.0" in plan,
            "pipx by name: the plan passes on the index pipx recorded",
        )
        output = self.apply_by_name(command, "pipx")
        self.check("ok: the same files" in output, "pipx by name: real pip got the same copy")
        self.check("setup finished" in output, "pipx by name: setup finishes")
        # The same record, while that index now serves another build of this version.
        record = root / "index" / "proton-mail-mcp" / "index.html"
        served = record.read_text()
        record.write_text(served.replace("../../files/", "../../other/"))
        try:
            output = self.apply_by_name(command, "pipx-other", expect=1)
        finally:
            record.write_text(served)
        self.check(
            "something other than the copy running" in output and "--source" in output,
            "pipx by name: another build of the version stops setup and names --source",
        )
        self.check("setup finished" not in output, "pipx by name: that run does not finish")

    def uv_tool_by_name(self, base: str) -> None:
        env = {
            "UV_TOOL_DIR": str(self.work / "named" / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(self.work / "named" / "uv-bin"),
        }
        self.run(
            "uv", "tool", "install", "--index-url", f"{base}/index/", "--extra-index-url",
            "https://pypi.org/simple", "--index-strategy", "unsafe-best-match", "proton-mail-mcp",
            env=env,
        )  # fmt: skip
        plan = self.run(
            self.work / "named" / "uv-bin" / "proton-mail-mcp", "setup", "--dry-run",
            "--bridge-user", ACCOUNT, "--skip-bridge", "--skip-bridge-service",
        )  # fmt: skip
        self.check(
            f"--index-url {base}/index/ --extra-index-url https://pypi.org/simple "
            "proton-mail-mcp==1.0.0" in plan,
            "uv tool by name: the plan passes on the index uv recorded",
        )

    def pip_by_name(self, base: str, files: Path) -> None:
        venv = self.work / "named" / "plain"
        self.run(self.python_command(), "-m", "venv", venv)
        self.run(venv / "bin" / "python", "-m", "pip", "install", "--find-links", files,
                 "proton-mail-mcp")  # fmt: skip
        command = [venv / "bin" / "proton-mail-mcp"]
        output = self.apply_by_name(
            command, "pip-nothing", expect=1, env={"PIP_INDEX_URL": f"{base}/empty-index/"}
        )
        self.check(
            "If this copy came from another index" in output and "--source" in output,
            "plain pip by name: an index without this copy stops setup and names --source",
        )
        output = self.apply_by_name(command, "pip-settings", env={"PIP_FIND_LINKS": str(files)})
        self.check(
            "--index-url" not in output and "ok: the same files" in output,
            "plain pip by name: pip's own settings find the same copy",
        )

    def uvx_by_name(self, files: Path) -> None:
        command = ["uvx", "--find-links", files, "--from", "proton-mail-mcp", "proton-mail-mcp"]
        plan = self.run(
            *command, "setup", "--dry-run", "--bridge-user", ACCOUNT, "--skip-bridge",
            "--skip-bridge-service",
        )  # fmt: skip
        self.check(
            "--only-binary=proton-mail-mcp proton-mail-mcp==1.0.0" in plan
            and "holds the copy running this installer" in plan,
            "uvx by name: no record, so the plan pins the version and checks the copy",
        )
        # This copy was installed by uv and the service's by pip; only their own files differ.
        output = self.apply_by_name(command, "uvx", env={"PIP_FIND_LINKS": str(files)})
        self.check("ok: the same files" in output, "uvx by name: pip's copy matches uv's")

    def python_command(self) -> str:
        return shutil.which(f"python{self.python}", path=self.env["PATH"]) or sys.executable


class QuietFiles(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass


def with_another_file(wheel: Path, other: Path) -> None:
    """The same wheel with a changed module and its RECORD left as it was."""
    module = "proton_mail_mcp/__init__.py"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(other, "w") as copy:
        for item in source.infolist():
            data = source.read(item.filename)
            copy.writestr(item, data + b"# another build\n" if item.filename == module else data)


def shlex_join(words: list[str]) -> str:
    return " ".join(words)


def version_of(wheel: Path) -> str:
    return re.fullmatch(r"proton_mail_mcp-([^-]+)-.*\.whl", wheel.name).group(1)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as status:
            return not status.read().split(") ")[-1].startswith("Z")
    except OSError:
        return False


def unauthenticated_status(port: int, deadline: float) -> int | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", b"{}", method="POST")
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except OSError:
            time.sleep(0.25)
    return None


def stop_exact(process: subprocess.Popen) -> None:
    if process.poll() is None:
        os.kill(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.kill(process.pid, signal.SIGKILL)
            process.wait()


def stop_leftovers(smoke: Smoke) -> None:
    """Stop by exact recorded PID whatever a failed run left."""
    for path in (smoke.state / "units").glob("*.pid") if (smoke.state / "units").exists() else ():
        try:
            os.kill(int(path.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, help="scratch directory (default: under TMPDIR)")
    parser.add_argument("--python", default="3.12", help="Python version for uv and pipx")
    parser.add_argument("--wheel", type=Path, help="an already built wheel")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args()
    for tool in ("uv", "uvx"):
        if shutil.which(tool) is None:
            print(f"wheel_smoke: {tool} is required", file=sys.stderr)
            return 3
    work = args.work or Path(tempfile.mkdtemp(prefix="pmm-wheel-smoke-"))
    work.mkdir(parents=True, exist_ok=True)
    work = work.resolve()
    smoke = Smoke(work, args.python)
    print(f"Scratch directory: {work}", flush=True)
    code = 1
    try:
        wheel = args.wheel.resolve() if args.wheel else smoke.build()
        smoke.inspect(wheel)
        smoke.stand_ins()
        uvx = ["uvx", "--from", wheel, "proton-mail-mcp"]
        smoke.dry_runs(uvx, "uvx")
        smoke.serve(wheel)
        if sys.platform == "linux":
            smoke.full_setup(uvx)
            smoke.pipx(wheel)
            smoke.named_installs(wheel)
        print(f"PASS: {smoke.checks} checks", flush=True)
        code = 0
    except (Failed, subprocess.SubprocessError, OSError) as error:
        print(f"FAIL after {smoke.checks} checks: {error}", file=sys.stderr, flush=True)
    finally:
        stop_leftovers(smoke)
        if code == 0 and not args.keep and not args.work:
            shutil.rmtree(work, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
