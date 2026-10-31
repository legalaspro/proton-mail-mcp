"""Run whole pytest files in isolated processes without additional dependencies."""

import argparse
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_files(root):
    return sorted(
        {
            path.relative_to(root).as_posix()
            for pattern in ("test_*.py", "*_test.py")
            for path in (root / "tests").rglob(pattern)
        }
    )


def file_shards(files, jobs, weights):
    """Greedy longest-file-first packing; each file runs in exactly one process."""
    shards = [[] for _ in range(min(jobs, len(files)))]
    totals = [0.0] * len(shards)
    for path in sorted(files, key=lambda path: (-weights[path], path)):
        index = min(range(len(shards)), key=lambda index: (totals[index], index))
        shards[index].append(path)
        totals[index] += weights[path]
    return shards


def load_weights(root, files, timings):
    if timings.is_file():
        measured = json.loads(timings.read_text())
        if not isinstance(measured, dict) or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in measured.values()
        ):
            raise ValueError("File timings must map test paths to finite positive seconds")
        fallback = statistics.median(measured.values()) if measured else 1.0
        return {path: measured.get(path, fallback) for path in files}
    return {path: (root / path).stat().st_size for path in files}


def pytest_command(files, temporary, report):
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--durations=20",
        "-o",
        "junit_family=legacy",
        f"--junitxml={report}",
        f"--basetemp={temporary}",
        *files,
    ]


def stop_processes(processes):
    running = [process for process in processes if process.poll() is None]
    # Even an already-exited pytest worker can leave children holding its group.
    # Every POSIX worker starts a new session, so each recorded group is ours.
    groups = processes if os.name == "posix" else running
    for process in groups:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
    # One shared grace period bounds cleanup even with many unresponsive shards.
    deadline = time.monotonic() + 3
    for process in running:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # A parent can exit on TERM while a child ignores it, or exit before cleanup
    # even starts. Kill every remaining group before removing temporary state.
    for process in groups:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass
    for process in running:
        process.wait()


def wait_for_shards(processes):
    remaining = set(range(len(processes)))
    while remaining:
        for index in sorted(remaining):
            code = processes[index].poll()
            if code is not None:
                remaining.remove(index)
                print(f"Shard {index + 1}: exit {code}", flush=True)
                if code:
                    return code if code > 0 else 128 - code
        if remaining:
            time.sleep(0.05)
    return 0


def run_shards(shards, root, output):
    processes = []
    with ExitStack() as stack:
        # Index caches intentionally refuse storage below any Git checkout.
        # Native temporary storage preserves pytest's normal location; TMPDIR
        # can confine test state to an external job directory when required.
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="pmm-tests-")))
        try:
            for index, files in enumerate(shards, 1):
                log = stack.enter_context((output / f"shard-{index}.log").open("w"))
                command = pytest_command(
                    files, temporary / f"shard-{index}", output / f"shard-{index}.xml"
                )
                processes.append(
                    subprocess.Popen(
                        command,
                        cwd=root,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=os.name == "posix",
                    )
                )
            return wait_for_shards(processes)
        finally:
            stop_processes(processes)


def summarize(output):
    totals = dict(tests=0, failures=0, errors=0, skipped=0)
    timings = {}
    for report in sorted(output.glob("shard-*.xml")):
        for suite in ET.parse(report).getroot().iter("testsuite"):
            for key in totals:
                totals[key] += int(suite.get(key, "0"))
            for case in suite.findall("testcase"):
                path = case.get("file")
                if path:
                    timings[path] = timings.get(path, 0.0) + float(case.get("time", "0"))
    totals["passed"] = totals["tests"] - sum(
        totals[key] for key in ("failures", "errors", "skipped")
    )
    return totals, {
        path: round(max(seconds, 0.001), 6) for path, seconds in sorted(timings.items())
    }


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("full",), default="full")
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument("--timings", type=Path, default=ROOT / "scripts/test_file_times.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "out/test-runs")
    parser.add_argument("--list", action="store_true", help="List selected files without running")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    files = test_files(ROOT)
    if not files:
        parser.error("No test files found")
    if args.list:
        print("\n".join(files))
        return 0
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    jobs = min(args.jobs, cpus or 1, len(files))
    shards = file_shards(files, jobs, load_weights(ROOT, files, args.timings))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=f"{args.suite}-", dir=args.output_dir)).resolve()
    print(
        f"{args.suite}: {len(files)} files across {jobs} pytest processes; logs: {output}",
        flush=True,
    )
    started = time.monotonic()
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        code = run_shards(shards, ROOT, output)
    except KeyboardInterrupt:
        print("Interrupted; all pytest processes stopped.", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
    totals, timings = summarize(output)
    results = dict(
        suite=args.suite,
        jobs=jobs,
        exit_code=code,
        seconds=round(time.monotonic() - started, 3),
        **totals,
    )
    (output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    (output / "file-times.json").write_text(json.dumps(timings, indent=2) + "\n")
    print(f"{totals['passed']} passed, {totals['skipped']} skipped in {results['seconds']:.2f}s")
    if code:
        print(f"Suite failed with exit {code}; counts cover completed shard reports.")
        for log in sorted(output.glob("shard-*.log")):
            print(f"\n--- {log.name} ---\n{log.read_text()}")
    else:
        print(f"Per-file times: {output / 'file-times.json'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
