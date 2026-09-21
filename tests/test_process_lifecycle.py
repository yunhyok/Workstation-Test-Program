from __future__ import annotations

import _thread
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / "tools" / "engine_studies" / "workstation"
sys.path.insert(0, str(REMOTE))
import common  # noqa: E402
import ws_validate  # noqa: E402


TREE_CHILD = r"""
import json, os, pathlib, subprocess, sys, time
pid_file = pathlib.Path(sys.argv[1])
stop_file = pathlib.Path(sys.argv[2])
action = sys.argv[3]
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
temporary = pid_file.with_suffix(".tmp")
temporary.write_text(json.dumps({"parent": os.getpid(), "grandchild": grandchild.pid}), encoding="utf-8")
os.replace(temporary, pid_file)
if action == "stop":
    stop_file.write_text("now\n", encoding="utf-8")
while True:
    time.sleep(.1)
"""


def _wait_json(path: Path, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(.01)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_dead(pids: tuple[int, ...], timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(common.process_alive(pid) for pid in pids):
            return
        time.sleep(.05)
    alive = [pid for pid in pids if common.process_alive(pid)]
    raise AssertionError(f"processes survived tree cleanup: {alive}")


def _cleanup_tree(parent_pid: int) -> None:
    """Best-effort cleanup for an assertion failure, scoped to a known test PID."""
    if not common.process_alive(parent_pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(parent_pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(parent_pid, signal.SIGKILL)


class DummyStatus:
    def __init__(self):
        self.writes = 0

    def write(self):
        self.writes += 1


def test_run_process_immediate_stop_kills_child_and_grandchild(tmp_path: Path):
    helper = tmp_path / "tree_child.py"
    helper.write_text(TREE_CHILD, encoding="utf-8")
    pid_file = tmp_path / "pids.json"
    stop_file = tmp_path / "stop.request"
    log_path = tmp_path / "run.log"
    pids: dict[str, int] = {}
    try:
        with log_path.open("w", encoding="utf-8") as log:
            returncode, stopped = ws_validate._run_process(
                [sys.executable, str(helper), str(pid_file), str(stop_file), "stop"],
                cwd=tmp_path,
                env=os.environ.copy(),
                log=log,
                status=DummyStatus(),
                stop_file=stop_file,
                stop_now=False,
            )
        pids = _wait_json(pid_file)
        assert stopped == "now"
        assert returncode != 0
        _wait_dead((pids["parent"], pids["grandchild"]))
    finally:
        if pids:
            _cleanup_tree(pids["parent"])


def test_run_process_keyboard_interrupt_kills_child_and_grandchild(tmp_path: Path):
    helper = tmp_path / "tree_child.py"
    helper.write_text(TREE_CHILD, encoding="utf-8")
    pid_file = tmp_path / "pids.json"
    stop_file = tmp_path / "unused-stop.request"
    log_path = tmp_path / "run.log"
    pids: dict[str, int] = {}

    def interrupt_after_tree_exists() -> None:
        _wait_json(pid_file)
        _thread.interrupt_main()

    interrupter = threading.Thread(target=interrupt_after_tree_exists, daemon=True)
    interrupter.start()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            with pytest.raises(KeyboardInterrupt):
                ws_validate._run_process(
                    [sys.executable, str(helper), str(pid_file), str(stop_file), "wait"],
                    cwd=tmp_path,
                    env=os.environ.copy(),
                    log=log,
                    status=DummyStatus(),
                    stop_file=stop_file,
                    stop_now=False,
                )
        interrupter.join(timeout=2)
        pids = _wait_json(pid_file)
        _wait_dead((pids["parent"], pids["grandchild"]))
    finally:
        if pids:
            _cleanup_tree(pids["parent"])


def test_contending_main_env_does_not_overwrite_owner_status(tmp_path: Path):
    ready = tmp_path / "owner.ready"
    release = tmp_path / "owner.release"
    owner_code = r"""
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import ws_validate
root, ready, release = map(pathlib.Path, sys.argv[2:5])
def hold(args, status, run_dir, log):
    ready.write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 20
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    return 0
ws_validate.COMMANDS["env"] = hold
raise SystemExit(ws_validate.main(["env", "--root", str(root)]))
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(REMOTE), str(tmp_path), str(ready), str(release)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while not ready.exists() and time.monotonic() < deadline:
            if owner.poll() is not None:
                stdout, stderr = owner.communicate()
                raise AssertionError(f"owner exited early ({owner.returncode}): {stdout}\n{stderr}")
            time.sleep(.02)
        assert ready.exists(), "owner did not acquire RunLock"
        status_path = tmp_path / "status.json"
        before = _wait_json(status_path)
        assert before["running"] is True
        assert before["pid"] == owner.pid

        contender = subprocess.run(
            [sys.executable, str(REMOTE / "ws_validate.py"), "env", "--root", str(tmp_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        assert contender.returncode != 0
        assert "another validation run" in contender.stderr
        after = _wait_json(status_path)
        assert after == before
        assert common.process_alive(owner.pid)
    finally:
        release.write_text("release\n", encoding="utf-8")
        try:
            owner.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _cleanup_tree(owner.pid)
            owner.wait(timeout=5)
    assert owner.returncode == 0
