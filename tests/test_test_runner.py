"""Check suite completeness and real worker failures/cleanup without pytest plugins."""

import importlib.util
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "test_runner", Path(__file__).parents[1] / "scripts" / "run_tests.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_sharding_preserves_every_file_once_and_balances_expensive_files():
    weights = {f"tests/test_{index}.py": weight for index, weight in enumerate([8, 7, 6, 5, 4])}
    shards = runner.file_shards(list(reversed(weights)), 3, weights)
    flattened = [path for shard in shards for path in shard]
    assert sorted(flattened) == sorted(weights)
    assert len(flattened) == len(set(flattened))
    assert max(sum(weights[path] for path in shard) for shard in shards) == 11
    assert runner.file_shards(list(weights), 3, weights) == shards


@pytest.mark.parametrize("suite_args", [[], ["--suite", "full"]], ids=["default", "full"])
def test_full_suite_includes_paging_and_future_files(tmp_path, monkeypatch, capsys, suite_args):
    files = [
        "tests/test_search_paging.py",
        "tests/test_thread_paging.py",
        "tests/test_future_guard.py",
        "tests/nested/test_new_confirmation.py",
        "tests/nested/new_guard_test.py",
    ]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert runner.test_files(tmp_path) == sorted(files)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    assert runner.main([*suite_args, "--list"]) == 0
    assert capsys.readouterr().out.splitlines() == sorted(files)


def test_worker_commands_isolate_temporary_state_and_disable_shared_pytest_cache(
    tmp_path, monkeypatch
):
    commands = [
        runner.pytest_command(["tests/test_guard.py"], tmp_path / str(index), tmp_path / "junit")
        for index in range(2)
    ]
    assert commands[0] != commands[1]
    for command in commands:
        assert command[:3] == [sys.executable, "-m", "pytest"]
        assert command[command.index("-p") + 1] == "no:cacheprovider"
        assert command[-1] == "tests/test_guard.py"

    def command(files, temporary, report):
        source = (
            "import os\n"
            "from pathlib import Path\n"
            "from proton_mail_mcp.config import Settings\n"
            "from proton_mail_mcp.search_index import SearchIndex\n"
            f"os.environ['XDG_STATE_HOME'] = {str(temporary / 'state')!r}\n"
            "index = SearchIndex(Settings(token='x' * 32, bridge_user='reader@example.test'))\n"
            "index.observe('INBOX', '1', {'1': {'headers': {'Subject': 'Synthetic'}}})\n"
            "assert index.enabled and index.path.is_file()\n"
            f"Path({str(tmp_path / files[0])!r}).write_text({str(temporary)!r})\n"
        )
        return [sys.executable, "-c", source]

    monkeypatch.setattr(runner, "pytest_command", command)
    # Logs belong in the checkout, while state must remain outside it: the
    # actual SearchIndex above rejects any cache below a .git ancestor.
    output = runner.ROOT / "out" / "test-runs"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runner-regression-", dir=output) as logs:
        assert runner.run_shards([["first"], ["second"]], runner.ROOT, Path(logs)) == 0
    first, second = (Path((tmp_path / name).read_text()) for name in ("first", "second"))
    assert first != second
    assert runner.ROOT not in first.parents and runner.ROOT not in second.parents


@pytest.mark.parametrize("weight", [0, -1, True, float("nan"), float("inf"), -float("inf")])
def test_corrupt_timing_weights_fail_before_scheduling(tmp_path, weight):
    timings = tmp_path / "timings.json"
    timings.write_text(json.dumps({"tests/test_guard.py": weight}))
    with pytest.raises(ValueError, match="finite positive seconds"):
        runner.load_weights(tmp_path, ["tests/test_guard.py"], timings)


def wait_for_file(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, "Worker did not start"
        time.sleep(0.01)


@pytest.mark.skipif(os.name != "posix", reason="Process-group cleanup is POSIX-specific")
def test_worker_failure_propagates_and_stops_siblings_with_logs(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    pid = tmp_path / "pid"

    def command(files, temporary, report):
        if files == ["failure"]:
            source = (
                "import pathlib, time\n"
                f"ready = pathlib.Path({str(ready)!r})\n"
                "deadline = time.monotonic() + 5\n"
                "while not ready.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
                "print('intentional worker failure', flush=True)\n"
                "raise SystemExit(7)\n"
            )
        else:
            source = (
                "import os, pathlib, time\n"
                f"pathlib.Path({str(pid)!r}).write_text(str(os.getpid()))\n"
                f"pathlib.Path({str(ready)!r}).touch()\n"
                "time.sleep(30)\n"
            )
        return [sys.executable, "-c", source]

    monkeypatch.setattr(runner, "pytest_command", command)
    started = time.monotonic()
    assert runner.run_shards([["failure"], ["sleeping"]], tmp_path, tmp_path) == 7
    assert time.monotonic() - started < 10
    assert "intentional worker failure" in (tmp_path / "shard-1.log").read_text()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


@pytest.mark.skipif(os.name != "posix", reason="Process-group cleanup is POSIX-specific")
def test_exited_worker_cannot_leave_a_descendant_that_ignores_term(tmp_path, monkeypatch):
    pid = tmp_path / "child-pid"
    ready = tmp_path / "child-ready"
    fifo = tmp_path / "child-alive"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    child = (
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"writer = open({str(fifo)!r}, 'wb', buffering=0)\n"
        "writer.write(b'alive')\n"
        f"pathlib.Path({str(pid)!r}).write_text(str(os.getpid()))\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "time.sleep(30)\n"
    )
    parent = (
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        f"ready = pathlib.Path({str(ready)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not ready.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "raise SystemExit(7)\n"
    )
    monkeypatch.setattr(runner, "pytest_command", lambda *args: [sys.executable, "-c", parent])
    try:
        assert runner.run_shards([["exited-parent"]], tmp_path, tmp_path) == 7
        assert pid.read_text()
        assert os.read(reader, 32) == b"alive"
        # EOF proves the child has closed its descriptor, including on systems
        # where an exited orphan briefly remains as a zombie awaiting reaping.
        deadline = time.monotonic() + 3
        while True:
            try:
                assert os.read(reader, 32) == b""
                break
            except BlockingIOError:
                assert time.monotonic() < deadline, "Worker descendant is still alive"
                time.sleep(0.01)
    finally:
        if pid.exists() and pid.read_text():
            try:
                os.kill(int(pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(reader)


@pytest.mark.skipif(os.name != "posix", reason="Process-group cleanup is POSIX-specific")
def test_interruption_stops_started_workers(tmp_path, monkeypatch):
    pid = tmp_path / "pid"
    monkeypatch.setattr(
        runner,
        "pytest_command",
        lambda *args: [
            sys.executable,
            "-c",
            "import os, pathlib, time; "
            f"pathlib.Path({str(pid)!r}).write_text(str(os.getpid())); time.sleep(30)",
        ],
    )

    def interrupt(processes):
        wait_for_file(pid)
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "wait_for_shards", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run_shards([["sleeping"]], tmp_path, tmp_path)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


def test_junit_summary_adds_every_file_and_preserves_failures(tmp_path):
    (tmp_path / "shard-1.xml").write_text(
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1">'
        '<testcase file="tests/test_a.py" time="0.1" />'
        '<testcase file="tests/test_a.py" time="0.2"><failure /></testcase>'
        '<testcase file="tests/test_b.py" time="0.0"><skipped /></testcase>'
        "</testsuite></testsuites>"
    )
    (tmp_path / "shard-2.xml").write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase file="tests/test_c.py" time="0.5" />'
        "</testsuite></testsuites>"
    )
    totals, timings = runner.summarize(tmp_path)
    assert totals == dict(tests=4, failures=1, errors=0, skipped=1, passed=2)
    assert timings == {"tests/test_a.py": 0.3, "tests/test_b.py": 0.001, "tests/test_c.py": 0.5}
