"""What the rest of the server does on Windows, checked on any platform with sys.platform set.

The seam's own Windows calls are in test_localfs_windows.py; real Windows behaviour is what
test_localfs.py checks where Windows, or Wine, is there to answer.
"""

import dataclasses
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_localfs_windows import kernel32  # noqa: F401

from proton_mail_mcp import attachments as attachments_module
from proton_mail_mcp import config as config_module
from proton_mail_mcp import localfs, service_entry
from proton_mail_mcp.attachments import safe_filename, save_attachment
from proton_mail_mcp.config import (
    ConfigError,
    Settings,
    default_attachments_dir,
    default_credential_store,
    windows_attachments_dir,
)
from proton_mail_mcp.installer import cli as installer_cli
from proton_mail_mcp.installer import clients, execute, templates
from proton_mail_mcp.installer import plan as planner
from proton_mail_mcp.installer import probe as probes
from proton_mail_mcp.installer.detect import detect, simulated
from proton_mail_mcp.installer.execute import Executor, remove_command
from proton_mail_mcp.installer.plan import Options, PlanError, install_plan, paths_for
from proton_mail_mcp.installer.probe import simulated_probe
from proton_mail_mcp.installer.steps import (
    BridgeLogin,
    Command,
    Note,
    SelfCheck,
    WaitForPort,
    WriteFile,
    powershell_literal,
)
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.search_index import SearchIndex
from proton_mail_mcp.service_entry import EnvFileError, read_env_file, service_env

ROOT = Path(__file__).resolve().parents[1]
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX behaviour")
BASE = {"PROTON_MCP_TOKEN": "t" * 43, "PROTON_BRIDGE_USER": "reader@example.com"}
USER = "you@example.com"
REAL_RUN = subprocess.run
MESSAGE = "<files@example.com>"


@pytest.fixture
def on_windows(monkeypatch):
    """sys.platform as Windows reports it; os.path stays this host's, so paths are real ones."""
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.fixture
def profile(on_windows, request, monkeypatch, tmp_path):
    # sys.platform is set by now: was this host really Windows when the tests began?
    if os.name == "posix":
        request.getfixturevalue("kernel32")
    home = tmp_path / "Users" / "me"
    local = home / "AppData" / "Local"
    local.mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser", lambda _path: str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


# Settings ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "PROTON_MCP_ACTION_LOG",
        "PROTON_MCP_SAVED_RULES",
        "PROTON_MCP_IDENTITY_INDEX_DIR",
        "PROTON_MCP_ATTACHMENTS_DIR",
    ],
)
def test_a_private_path_outside_the_profile_refuses_at_startup(profile, tmp_path, name):
    # A folder named here because the default is a Windows path, which this host may not read.
    base = {**BASE, "PROTON_MCP_ATTACHMENTS_DIR": str(profile / "files")}
    outside = str(tmp_path / "shared" / "data")
    with pytest.raises(ConfigError, match=f"{name} must be inside your user profile"):
        Settings.from_env({**base, name: outside})
    inside = str(profile / "data")
    assert Settings.from_env({**base, name: inside})


def test_every_default_path_is_inside_the_profile(profile):
    settings = Settings.from_env({**BASE, "PROTON_MCP_ATTACHMENTS_DIR": str(profile / "files")})
    assert settings.action_log_path is None and settings.identity_index_dir is None
    for path in (
        localfs.state_home() / "actions.jsonl",
        localfs.state_home() / "identity-index",
        localfs.state_home() / "saved-rules.json",
    ):
        assert localfs.path_refusal(path) is None


@posix
def test_posix_settings_take_any_path_as_before(tmp_path):
    settings = Settings.from_env({**BASE, "PROTON_MCP_ACTION_LOG": str(tmp_path / "log")})
    assert settings.action_log_path == str(tmp_path / "log")


@posix
def test_the_default_credential_store_follows_the_platform(monkeypatch):
    assert default_credential_store() == "keychain"
    assert Settings.from_env(BASE).credential_store == "keychain"
    monkeypatch.setattr(sys, "platform", "win32")
    assert default_credential_store() == "wincred"
    # A folder named here because the default is a Windows path, which this host may not read.
    monkeypatch.setattr(localfs, "path_refusal", lambda _path: None)
    settings = Settings.from_env({**BASE, "PROTON_MCP_ATTACHMENTS_DIR": "/home/me/files"})
    assert settings.credential_store == "wincred"
    assert Settings(token="t" * 43, bridge_user="a@b.example").credential_store == "wincred"


def test_a_service_with_no_store_setting_reads_credential_manager(on_windows, monkeypatch):
    seen = []
    monkeypatch.setattr(
        service_entry.credential_store, "lookup", lambda store, *_: seen.append(store) or "token"
    )
    assert service_env({"PROTON_BRIDGE_USER": USER}, {})["PROTON_MCP_TOKEN"] == "token"
    assert seen == ["wincred"]


# Default folders --------------------------------------------------------------


@pytest.mark.parametrize(
    "env,expected",
    [
        (
            {"LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local", "XDG_DATA_HOME": "C:\\xdg"},
            "C:\\Users\\me\\AppData\\Local\\proton-mail-mcp\\attachments",
        ),
        (
            {"LOCALAPPDATA": "relative", "USERPROFILE": "D:\\Home\\me"},
            "D:\\Home\\me\\AppData\\Local\\proton-mail-mcp\\attachments",
        ),
        (
            {"HOME": "C:\\Users\\me"},
            "C:\\Users\\me\\AppData\\Local\\proton-mail-mcp\\attachments",
        ),
    ],
)
def test_the_default_attachments_folder_on_windows(env, expected, monkeypatch):
    assert windows_attachments_dir(env) == expected
    monkeypatch.setattr(sys, "platform", "win32")
    assert default_attachments_dir(env) == expected


def test_no_usable_windows_home_gives_no_default_folder(monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda _path: "~")
    assert windows_attachments_dir({}) is None


def test_a_windows_style_folder_passes_the_validity_check_as_windows_reads_it():
    valid = config_module.valid_attachments_dir
    assert valid("C:\\Users\\me\\files", ntpath)
    assert not valid("C:\\", ntpath)
    assert not valid("C:\\Users\\me\\files\\", ntpath)
    assert not valid("C:/Users/me/files", ntpath)
    assert not valid("relative\\files", ntpath)


# Env file ---------------------------------------------------------------------


def test_an_env_file_outside_the_profile_refuses(profile, tmp_path):
    outside = tmp_path / "shared.env"
    outside.write_text(f"PROTON_BRIDGE_USER={USER}\n", encoding="utf-8")
    outside.chmod(0o600)
    with pytest.raises(EnvFileError, match="inside your user profile"):
        read_env_file(outside)
    inside = profile / "settings.env"
    inside.write_text(f"PROTON_BRIDGE_USER={USER}\n", encoding="utf-8")
    inside.chmod(0o600)
    assert read_env_file(inside) == {"PROTON_BRIDGE_USER": USER}


def test_a_byte_order_mark_is_accepted_on_windows_and_refused_elsewhere(monkeypatch, tmp_path):
    # Windows PowerShell 5.1's Set-Content -Encoding UTF8 writes one.
    path = tmp_path / "settings.env"
    path.write_bytes(b"\xef\xbb\xbfPROTON_BRIDGE_USER=" + USER.encode() + b"\n")
    path.chmod(0o600)
    monkeypatch.setattr(localfs, "path_refusal", lambda _path: None)
    monkeypatch.setattr(localfs, "acl_refusal", lambda _descriptor: None)
    if sys.platform != "win32":
        with pytest.raises(EnvFileError, match="expected PROTON_NAME=value"):
            read_env_file(path)
    monkeypatch.setattr(sys, "platform", "win32")
    assert read_env_file(path) == {"PROTON_BRIDGE_USER": USER}


# The search index ------------------------------------------------------------


def test_sqlite_keeps_its_spill_files_in_memory_on_windows(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        action_log_path=str(tmp_path / "actions.jsonl"),
        bridge_user=USER,
        imap_host="127.0.0.1",
        imap_port=1143,
    )
    index = SearchIndex(settings, rebuild=True)
    with closing(index._connect()) as database:
        default = database.execute("PRAGMA temp_store").fetchone()[0]
    monkeypatch.setattr(sys, "platform", "win32")
    with closing(index._connect()) as database:
        assert database.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
    if os.name == "posix":
        assert default == 0  # DEFAULT, as ever


# Attachment names -------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "C:evil.exe",
        "report.pdf:hidden",
        "con",
        "NUL.txt",
        "a<b.txt",
        "star*.txt",
        "secret.txt.",
        "secret.txt ",
        "ABCDEF~1.TXT",
        "folder/C:evil.exe",
    ],
)
def test_a_local_name_windows_reads_as_something_else_refuses(on_windows, name):
    with pytest.raises(MailValidationError, match="File names must be plain"):
        attachments_module._steps(name)


@posix
def test_the_same_names_stay_plain_files_on_posix():
    assert attachments_module._steps("con") == ["con"]
    assert attachments_module._steps("a:b.txt") == ["a:b.txt"]


@pytest.fixture
def stored(on_windows, tmp_path):
    folder = tmp_path / "files"
    folder.mkdir()
    return folder


@pytest.mark.parametrize(
    "sender,saved",
    [
        ("report.pdf", "report.pdf"),
        ("C:evil.exe", "C_evil.exe"),
        ("report.pdf:hidden", "report.pdf_hidden"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("a<b>c.txt", "a_b_c.txt"),
        ("..\\..\\win.ini", "win.ini"),
    ],
)
def test_a_senders_name_becomes_a_plain_file_on_windows(stored, monkeypatch, sender, saved):
    # POSIX os calls behind the Windows branch of nothing but the name rules.
    monkeypatch.setattr(sys, "platform", "linux")
    assert localfs.plain_name(safe_filename(sender, "fallback")) == safe_filename(
        sender, "fallback"
    )
    monkeypatch.setattr(sys, "platform", "win32")
    assert localfs.plain_name(safe_filename(sender, "fallback")) == saved


def test_save_attachment_cleans_a_senders_name_only_for_the_disk(on_windows, monkeypatch):
    attached = {"filename": "report.pdf:hidden", "type": "text/plain"}
    written = []
    monkeypatch.setattr(
        attachments_module, "_open_root", lambda *_a, **_k: os.open(os.devnull, os.O_RDONLY)
    )
    monkeypatch.setattr(attachments_module, "_message", lambda *_: {})
    monkeypatch.setattr(attachments_module, "_fetch_parts", lambda *_: [(1, attached, b"x")])
    monkeypatch.setattr(
        attachments_module, "_write_new", lambda _s, name, _d: written.append(name) or name
    )
    mailbox = SimpleNamespace(settings=None)
    assert save_attachment(mailbox, MESSAGE, 1)["filename"] == "report.pdf_hidden"
    assert written == ["report.pdf_hidden"]
    # A draft's attachment name never touches the disk, and stays as the sender wrote it.
    assert safe_filename("report.pdf:hidden", "x") == "report.pdf:hidden"


# The PDF worker ---------------------------------------------------------------


def worker_launch(monkeypatch):
    seen = {}

    class Process:
        returncode = 0

        def __init__(self, command, **options):
            seen.update(options, command=command)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def communicate(self, data=None, timeout=None):
            return (b'{"pages": 0, "read": 0, "failed": 0, "text": ""}', b"")

    monkeypatch.setattr(subprocess, "Popen", Process)
    attachments_module._pdf_text(b"%PDF")
    return seen


def test_the_pdf_worker_on_windows_joins_a_job_and_keeps_only_systemroot(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    monkeypatch.setenv("PROTON_MCP_TOKEN", "secret")
    seen = worker_launch(monkeypatch)
    assert seen["env"] == {"SYSTEMROOT": "C:\\Windows"}
    assert seen["command"][1:3] == ["-I", "-B"]
    script = seen["command"][-1]
    assert "resource" not in script and "AssignProcessToJobObject" in script
    assert f"process_memory={attachments_module._PDF_MEMORY_BYTES}" in script
    assert f"process_time = {attachments_module._PDF_SECONDS} * 10000000" in script
    # 0x2 process time, 0x8 process count, 0x100 process memory.
    assert "limits.basic.flags = 0x2 | 0x8 | 0x100" in script
    # Refuses rather than parsing without the limits.
    assert "raise SystemExit(1)" in script
    compile(script, "worker", "exec")


@posix
def test_the_pdf_worker_on_posix_has_its_rlimits_empty_environment_and_root(monkeypatch):
    seen = worker_launch(monkeypatch)
    assert seen["env"] == {} and seen["cwd"] == "/"
    script = seen["command"][-1]
    assert "AssignProcessToJobObject" not in script
    assert "import resource" in script
    assert '("RLIMIT_AS", 1073741824), ("RLIMIT_CPU", 30), ("RLIMIT_FSIZE", 0)' in script
    assert script.index("resource.setrlimit") < script.index("from pypdf import PdfReader")
    compile(script, "worker", "exec")


# Installer --------------------------------------------------------------------


def windows_plan(**overrides):
    platform = simulated("windows")
    probe = overrides.pop("probe", None) or simulated_probe(platform)
    values = {
        "bridge_user": USER,
        "source": "C:\\src\\checkout",
        "python": "C:\\Python\\python.exe",
    }
    options = Options(**{**values, **overrides})
    return install_plan(platform, options, probe, paths_for(platform, probe.env, probe.home))


def test_state_lives_with_the_rest_of_the_servers_files():
    platform = simulated("windows")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    assert paths.state_dir == "C:\\Users\\you\\AppData\\Local\\proton-mail-mcp\\state"
    assert ".local" not in " ".join(str(value) for value in vars(paths).values())


def test_the_plan_says_windows_has_not_run_for_real():
    notes = " ".join(step.text for step in windows_plan() if isinstance(step, Note))
    assert "not yet on Windows 10" in notes and "docs/windows.md" in notes


# Paths a pasted PowerShell command must keep whole, and each as its literal doubles any quote.
POWERSHELL_PATHS = [
    pytest.param("C:\\Program Files\\pm", "C:\\Program Files\\pm", id="space"),
    pytest.param("C:\\it's\\pm", "C:\\it''s\\pm", id="apostrophe"),
    pytest.param("C:\\$HOME\\pm", "C:\\$HOME\\pm", id="dollar"),
    pytest.param("C:\\$(Write-Output x)\\pm", "C:\\$(Write-Output x)\\pm", id="subexpression"),
    pytest.param("C:\\a`b\\pm", "C:\\a`b\\pm", id="backtick"),
    pytest.param('C:\\a"b\\pm', 'C:\\a"b\\pm', id="double-quote"),
    pytest.param("C:\\it\u2019s\\pm", "C:\\it\u2019\u2019s\\pm", id="curly-apostrophe"),
    pytest.param(
        "C:\\\u2018\u201a\u201b\\pm",
        "C:\\\u2018\u2018\u201a\u201a\u201b\u201b\\pm",
        id="other-curly-quotes",
    ),
    pytest.param("C:\\'$('\\pm", "C:\\''$(''\\pm", id="trying-to-get-out"),
    pytest.param("C:\\it''s\\pm", "C:\\it''''s\\pm", id="already-doubled"),
    pytest.param(
        "C:\\Users\\x\u2019; Write-Output hit; \u2019y\\venv",
        "C:\\Users\\x\u2019\u2019; Write-Output hit; \u2019\u2019y\\venv",
        id="curly-break-out",
    ),
]


@pytest.mark.parametrize(("venv", "literal"), POWERSHELL_PATHS)
def test_both_token_commands_quote_the_path_as_a_powershell_literal(venv, literal):
    command = f"& '{literal}\\Scripts\\proton-mail-mcp.exe' show-token"
    assert f"`{command} --copy`" in planner.token_hint(venv, windows=True)
    platform = simulated("windows")
    probe = simulated_probe(platform)
    paths = dataclasses.replace(paths_for(platform, probe.env, probe.home), venv=venv)
    steps = planner._self_check_steps(platform, "wincred", USER, Options(), paths, {})
    assert f"`{command}`" in steps[0].text


@pytest.mark.parametrize(("path", "literal"), POWERSHELL_PATHS)
def test_the_other_pasted_commands_quote_the_path_as_a_powershell_literal(path, literal):
    platform = simulated("windows")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    login = planner._login_note(platform, Options(bridge_bin=path), probe, paths).text
    assert f"`& '{literal}' --cli`" in login
    remove = f"Remove-Item -LiteralPath '{literal}'"
    assert planner._removal_command(platform, path) == f"{remove} -Recurse -Force"
    assert planner._removal_command(platform, path, "C:\\b") == f"{remove}, 'C:\\b' -Recurse -Force"
    assert planner._file_removal_command(platform, path) == f"{remove} -Force"
    assert remove_command(path, True) == remove
    assert remove_command(path, True, force=True) == f"{remove} -Force"


def test_a_pasted_task_name_has_nothing_powershell_reads():
    # schtasks commands name the task in double quotes, which would expand $ or end at a quote.
    name = templates.task_name(templates.BRIDGE_TASK_PREFIX, "HOST\\o'b$(x)`\"")
    assert re.fullmatch(r"[\\A-Za-z0-9._-]+", name)


def quoted_command(text, pattern):
    """The command the pattern's group finds in the text, or a failure naming the wording."""
    found = re.search(pattern, text, re.S)
    assert found, f"no pasted command matches {pattern!r} in {text!r}"
    return found[1]


def pasted_powershell(path):
    """Each command naming this path that the plan tells a person to paste, as it renders them."""
    platform = simulated("windows")
    probe = simulated_probe(platform)
    paths = dataclasses.replace(paths_for(platform, probe.env, probe.home), venv=path)
    token = planner._self_check_steps(platform, "wincred", USER, Options(), paths, {})[0].text
    # A person pastes configure-client with a client's name in place of <name>.
    hint = planner.token_hint(path, windows=True).replace("<name>", next(iter(clients.CLIENTS)))
    login = planner._login_note(platform, Options(bridge_bin=path), probe, paths).text
    tool = f"{path}\\Scripts\\proton-mail-mcp.exe"
    return [
        (quoted_command(token, r"`(& .*)` \(prints"), [tool]),
        (quoted_command(hint, r"run `(& .*)` \("), [tool]),
        (quoted_command(hint, r"start `(& .*)`, which reads"), [tool]),
        (quoted_command(hint, r"none\. `(& .*)`, at a terminal"), [tool]),
        (quoted_command(login, r"run `(& .*)`, then"), [path]),
        (planner._removal_command(platform, path, "C:\\b"), [path, "C:\\b"]),
        (planner._file_removal_command(platform, path), [path]),
        (remove_command(path, True, force=True), [path]),
    ]


@pytest.mark.parametrize(("path", "literal"), POWERSHELL_PATHS)
def test_each_pasted_command_is_found_without_powershell(path, literal):
    # The next test skips without pwsh, so finding the commands is checked everywhere: each one
    # found, and none running on into the next.
    for command, _ in pasted_powershell(path):
        assert command.count(f"'{literal}") == 1


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 not installed")
def test_powershell_itself_reads_each_pasted_command_back_as_its_paths():
    pasted = [row for param in POWERSHELL_PATHS for row in pasted_powershell(param.values[0])]
    # One command in one statement, and its only quoted strings are the paths, exactly.
    read = (
        "$env:PMM_PASTED | ConvertFrom-Json | ForEach-Object { "
        "$errors = $null; "
        "$ast = [System.Management.Automation.Language.Parser]::ParseInput("
        "$_, [ref]$null, [ref]$errors); "
        "$found = { param($type) $ast.FindAll({ param($node) $node -is $type }, $true) }; "
        "$strings = & $found ([System.Management.Automation.Language.StringConstantExpressionAst]) "
        "| Where-Object StringConstantType -eq 'SingleQuoted' | ForEach-Object Value; "
        "[pscustomobject]@{ "
        "errors = @($errors).Count; "
        "statements = $ast.EndBlock.Statements.Count; "
        "commands = @(& $found ([System.Management.Automation.Language.CommandAst])).Count; "
        "strings = @($strings) } "
        "} | ConvertTo-Json -Compress -Depth 3 -AsArray -EscapeHandling EscapeNonAscii"
    )
    result = REAL_RUN(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", read],
        env={**os.environ, "PMM_PASTED": json.dumps([command for command, _ in pasted])},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"errors": 0, "statements": 1, "commands": 1, "strings": strings} for _, strings in pasted
    ]


def test_a_powershell_literal_has_no_way_out_of_its_quotes():
    # Any of these ends a single-quoted string in PowerShell unless it is doubled.
    marks = re.compile("['\u2018\u2019\u201a\u201b]+")
    for text in ("a'b", "\u2018", "x\u2019\u2019y", "''", "\u201a'\u201b", "$(x)`n", ""):
        literal = powershell_literal(text)
        assert literal[0] == "'" and literal[-1] == "'"
        assert all(len(run) % 2 == 0 for run in marks.findall(literal[1:-1]))


@posix
def test_posix_token_commands_keep_their_shell_quoting():
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = dataclasses.replace(paths_for(platform, probe.env, probe.home), venv="/it's $x/venv")
    steps = planner._self_check_steps(platform, "libsecret", USER, Options(), paths, {})
    assert "`'/it'\"'\"'s $x/venv/bin/proton-mail-mcp' show-token`" in steps[0].text


def test_a_state_path_outside_the_profile_stops_the_plan():
    platform = simulated("windows")
    probe = simulated_probe(platform)
    probe.env["PROTON_MCP_ACTION_LOG"] = "D:\\shared\\actions.jsonl"
    with pytest.raises(PlanError, match="inside your user profile"):
        windows_plan(probe=probe)
    probe.env["PROTON_MCP_ACTION_LOG"] = "C:\\Users\\you\\state\\actions.jsonl"
    assert windows_plan(probe=probe)


def test_setup_checks_bridge_and_its_task_the_windows_way():
    platform = simulated("windows")
    probe = simulated_probe(platform)
    task = "\\proton-mail-mcp-bridge-HOST-you"
    probe.outputs[("schtasks", "/Query", "/TN", task, "/XML")] = planner.MARKER
    steps = windows_plan(probe=probe, setup=True)
    [login] = [step for step in steps if isinstance(step, BridgeLogin)]
    assert login.processes[0] == "powershell" and "Get-Process" in login.processes[-1]
    assert "pgrep" not in login.processes
    assert login.service.stop == ("schtasks", "/End", "/TN", task)
    assert login.service.start == ("schtasks", "/Run", "/TN", task)
    assert login.service.stopped.status == planner.TASK_NOT_RUNNING
    [check] = [step for step in steps if isinstance(step, SelfCheck)]
    assert check.hint.endswith("logs\\mcp.log")
    [bridge] = [step for step in steps if isinstance(step, WaitForPort) and step.port == 1143]
    assert bridge.hint == "Task Scheduler"
    notes = " ".join(step.text for step in steps if isinstance(step, Note))
    assert "venv\\Scripts\\proton-mail-mcp.exe' show-token" in notes
    assert "journalctl" not in notes and "/bin/" not in notes


def test_a_bridge_task_of_someone_else_is_never_stopped_for_a_login():
    steps = windows_plan(setup=True)
    [login] = [step for step in steps if isinstance(step, BridgeLogin)]
    assert login.service is None


def test_the_plan_runs_bridge_itself_not_its_launcher_which_exits_at_once():
    platform = simulated("windows")
    probe = simulated_probe(platform)
    exe = "C:\\Program Files\\Proton AG\\Proton Mail Bridge\\bridge.exe"
    assert planner.bridge_executable(platform, Options(), probe) == exe
    steps = windows_plan(probe=probe)
    [signature] = [
        step
        for step in steps
        if isinstance(step, Command) and step.summary.startswith("Verify Bridge")
    ]
    assert signature.env == (("PMM_VERIFY_PATH", exe),)
    [task] = [
        step
        for step in steps
        if isinstance(step, WriteFile) and step.path.endswith("bridge-task.xml")
    ]
    assert f"<Command>{exe}</Command>" in task.content
    notes = " ".join(step.text for step in steps if isinstance(step, Note))
    assert f"& '{exe}' --cli" in notes and "proton-bridge.exe" not in notes


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"PROGRAMW6432": "D:\\Program Files", "PROGRAMFILES": "D:\\Program Files (x86)"}, "D:"),
        ({"PROGRAMFILES": "E:\\Program Files"}, "E:"),
        ({}, "C:"),
    ],
)
def test_the_program_files_folder_comes_from_uppercase_keys_as_os_environ_has_them(env, expected):
    platform = simulated("windows")
    probe = simulated_probe(platform)
    probe.env = {key: value for key, value in probe.env.items() if key != "PROGRAMFILES"} | env
    exe = planner.bridge_executable(platform, Options(), probe)
    assert exe.startswith(f"{expected}\\Program Files\\Proton AG")


def test_every_windows_powershell_command_starts_from_the_machines_module_path():
    # A child of PowerShell 7 inherits its module path, and 5.1 then cannot load its own cmdlets.
    platform = simulated("windows")
    probe = simulated_probe(platform)
    reset = "$env:PSModulePath = [Environment]::GetEnvironmentVariable('PSModulePath', 'Machine');"
    assert planner.AUTHENTICODE_CHECK.startswith(reset)
    assert planner._task_running_argv("\\task")[-1].startswith(reset)
    processes = planner.bridge_processes(platform, probe)[-1]
    assert processes.startswith(reset)
    # Another program's Bridge.exe, Adobe's say, is not this Bridge.
    assert "$_.Path -like '*Proton*'" in processes


def test_task_names_of_accounts_that_differ_only_in_other_letters_stay_apart():
    assert templates.task_name("p", "HOST\\you") == "\\p-HOST-you"
    anna, irina = (templates.task_name("p", f"HOST\\{name}") for name in ("Анна", "Ирина"))
    assert anna != irina and anna.startswith("\\p-HOST-") and irina.startswith("\\p-HOST-")


def test_the_token_hint_names_a_command_that_exists_without_a_checkout():
    venv = "C:\\Users\\you\\AppData\\Local\\proton-mail-mcp\\venv"
    hint = planner.token_hint(venv, windows=True)
    assert f"& '{venv}\\Scripts\\proton-mail-mcp.exe' show-token --copy" in hint
    assert "install.ps1" not in hint


def real_run(action="install", dry_run=False):
    return SimpleNamespace(action=action, dry_run=dry_run)


def test_applying_from_an_elevated_shell_refuses_on_windows(monkeypatch):
    # What it creates would belong to Administrators, out of reach of the normal logon task.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(installer_cli, "_elevated", lambda: True)
    assert "not as administrator" in installer_cli._windows_refusal(real_run())
    assert "not as administrator" in installer_cli._windows_refusal(real_run("uninstall"))
    # A dry run changes nothing, and CI runners are elevated.
    assert installer_cli._windows_refusal(real_run(dry_run=True)) is None
    monkeypatch.setattr(installer_cli, "_elevated", lambda: False)
    assert installer_cli._windows_refusal(real_run()) is None


def test_python_from_the_microsoft_store_refuses_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(installer_cli, "_elevated", lambda: False)
    store = "C:\\Program Files\\WindowsApps\\PythonSoftwareFoundation.Python.3.12_x64"
    monkeypatch.setattr(sys, "base_prefix", store)
    assert "Microsoft Store" in installer_cli._windows_refusal(real_run())
    assert "Microsoft Store" in installer_cli._windows_refusal(real_run("setup", dry_run=True))
    assert installer_cli._windows_refusal(real_run("uninstall")) is None


@posix
def test_neither_windows_refusal_applies_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "base_prefix", "/x\\WindowsApps\\y")
    assert installer_cli._windows_refusal(real_run()) is None


def test_the_installer_leaves_the_folder_where_a_program_could_be_planted(monkeypatch, tmp_path):
    moved = []
    monkeypatch.setattr(os, "chdir", moved.append)
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    if sys.platform == "win32":
        monkeypatch.undo()
        monkeypatch.setattr(os, "chdir", moved.append)
        monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    else:
        installer_cli._leave_current_folder()
        assert moved == []
        monkeypatch.setattr(sys, "platform", "win32")
    leave = vars(installer_cli)["_leave_current_folder"]
    leave()
    assert moved == ["C:\\Windows"]


def test_the_folder_changes_after_relative_paths_are_made_absolute_and_before_any_tool_runs(
    monkeypatch, capsys
):
    platform = simulated("debian")
    probe = simulated_probe(platform)
    order = []
    real_options = installer_cli._options

    def options(args):
        order.append("paths made absolute")
        return real_options(args)

    def plan(*_args):
        order.append("plan, which asks the tools")
        return []

    monkeypatch.setattr(installer_cli, "_options", options)
    monkeypatch.setattr(installer_cli, "install_plan", plan)
    monkeypatch.setattr(installer_cli, "detect_host", lambda: platform)
    monkeypatch.setattr(installer_cli, "HostProbe", lambda _platform: probe)
    monkeypatch.setattr(
        installer_cli, "_leave_current_folder", lambda: order.append("left the folder")
    )
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert installer_cli.main(["install", "--dry-run", "--bridge-user", USER]) == 0
    assert order == ["paths made absolute", "left the folder", "plan, which asks the tools"]
    order.clear()
    assert (
        installer_cli.main(["install", "--dry-run", "--simulate", "debian", "--bridge-user", USER])
        == 0
    )
    assert "left the folder" not in order
    capsys.readouterr()


def test_tool_output_decodes_whatever_the_code_page(monkeypatch):
    source = (ROOT / "src" / "proton_mail_mcp" / "installer" / "execute.py").read_text(
        encoding="utf-8"
    )
    assert "text=True" not in source
    monkeypatch.setattr(sys, "platform", "win32")
    assert execute._text() == {"text": True, "errors": "replace"}
    monkeypatch.setattr(sys, "platform", "linux")
    assert execute._text() == {"text": True}


def test_the_default_attachments_folder_of_a_plan_follows_the_planned_platform():
    platform = simulated("windows")
    probe = simulated_probe(platform)
    assert planner.default_attachments(platform, probe) == (
        "C:\\Users\\you\\AppData\\Local\\proton-mail-mcp\\attachments"
    )
    if sys.platform != "win32":
        linux = simulated("debian")
        assert planner.default_attachments(linux, simulated_probe(linux)) == (
            "/home/you/.local/share/proton-mail-mcp/attachments"
        )


def test_the_dry_run_names_access_rules_not_modes_on_windows(tmp_path, capsys):
    from proton_mail_mcp.installer.steps import EnsureDir

    steps = [
        EnsureDir(str(tmp_path / "new"), "Make a folder"),
        WriteFile(str(tmp_path / "new" / "file"), "text\n", "Write a file"),
    ]
    Executor(dry_run=True, windows=True).run(steps)
    out = capsys.readouterr().out
    assert "its folder's access rules" in out and "mode 0" not in out
    Executor(dry_run=True, windows=False).run(steps)
    out = capsys.readouterr().out
    assert "mode 0700" in out and "mode 0600" in out and "access rules" not in out


def test_removal_hints_fit_the_shell_of_the_platform():
    assert remove_command("C:\\a b\\it's", True) == "Remove-Item -LiteralPath 'C:\\a b\\it''s'"
    assert remove_command("C:\\f", True, force=True) == "Remove-Item -LiteralPath 'C:\\f' -Force"
    assert remove_command("/a b/f", False) == "rm -- '/a b/f'"
    assert remove_command("/f", False, force=True) == "rm -f -- /f"


def test_a_junction_counts_as_a_link_for_the_installer(monkeypatch, tmp_path):
    probe = probes.HostProbe(simulated("debian"))
    (tmp_path / "junction").mkdir()
    monkeypatch.setattr(os.path, "isjunction", lambda path: str(path).endswith("junction"))
    monkeypatch.setattr(os, "readlink", lambda _path: "C:\\elsewhere")
    assert probe.readlink(str(tmp_path / "junction")) == "C:\\elsewhere"
    assert probe.tree_size(str(tmp_path / "junction")) is None


def test_the_size_walk_never_enters_a_junction(tmp_path):
    probe = probes.HostProbe(simulated("debian"))
    (tmp_path / "file").write_bytes(b"1234")

    class Entry:
        path = str(tmp_path / "junction")

        @staticmethod
        def is_junction():
            return True

    real_scandir = os.scandir

    class Listing:
        def __init__(self, path):
            self.inner = real_scandir(path)

        def __enter__(self):
            return [Entry(), *self.inner]

        def __exit__(self, *_):
            self.inner.close()

    original, os.scandir = os.scandir, Listing
    try:
        assert probe.tree_size(str(tmp_path)) == 4
    finally:
        os.scandir = original


def setup_dry_run(capsys, *arguments):
    argv = ["setup", "--dry-run", "--simulate", "windows", "--bridge-user", USER, *arguments]
    code = installer_cli.main(argv)
    return code, capsys.readouterr()


def test_a_simulated_setup_takes_a_windows_attachments_folder(capsys):
    code, shown = setup_dry_run(capsys, "--attachments-dir", "C:\\Users\\you\\files")
    assert code == 0, shown.err
    assert "C:\\Users\\you\\files" in shown.out
    code, shown = setup_dry_run(capsys, "--attachments-dir", "~\\files")
    assert code == 0, shown.err
    assert "C:\\Users\\you\\files" in shown.out
    code, shown = setup_dry_run(capsys, "--attachments-dir", "relative\\files")
    assert code == 2 and "must be an absolute path" in shown.err


@pytest.mark.parametrize("simulate", [None, "windows"])
def test_setup_checks_a_folder_against_the_profile_only_on_the_real_host(monkeypatch, simulate):
    platform = simulated("windows")
    arguments = SimpleNamespace(attachments_dir="D:\\shared", simulate=simulate)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        localfs, "path_refusal", lambda _path: "must be inside your user profile on Windows"
    )

    def choose():
        return installer_cli._choose_attachments(
            arguments, platform, simulated_probe(platform), {}, False
        )

    if simulate:
        assert choose()[0] == "D:\\shared"
    else:
        with pytest.raises(PlanError, match="must be inside your user profile"):
            choose()


@pytest.mark.parametrize("system", ["CYGWIN_NT-10.0", "MSYS_NT-10.0", "MINGW64_NT-10.0"])
def test_python_from_a_windows_layer_is_told_what_to_use_instead(system):
    refusal = detect(system, "x86_64").refusal
    assert refusal.startswith(f"Unsupported platform: {system}")
    assert "Python for Windows" in refusal and "WSL2" in refusal and "docs/windows.md" in refusal


# Workflows, packaging and docs ------------------------------------------------


def test_the_checks_workflow_runs_the_same_selection_on_windows():
    workflow = (ROOT / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
    assert "windows-latest" in workflow and "WINDOWS_TESTS" not in workflow
    assert "not yet supported" not in workflow


def test_the_windows_docs_say_what_has_not_been_verified():
    text = (ROOT / "docs" / "windows.md").read_text(encoding="utf-8")
    assert "It has not been run on Windows 10\nor 11" in text
    for gap in ("A normal account", "A logon", "A real Proton account", "winget"):
        assert gap in text


def test_tests_close_every_sqlite_connection_they_open():
    # A connection's own `with` commits but never closes, so the connection waits in a reference
    # cycle for a collection, which conftest makes rare. Windows won't delete a file left open.
    unclosed = [
        path.name
        for path in (ROOT / "tests").rglob("*.py")
        if re.search(
            r"sqlite3\.connect\(",
            re.sub(r"closing\(\s*sqlite3\.connect\(", "", path.read_text(encoding="utf-8")),
        )
    ]
    assert not unclosed
