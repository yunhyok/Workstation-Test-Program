from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKSTATION = ROOT / "tools" / "engine_studies" / "workstation"
sys.path.insert(0, str(WORKSTATION))
import common
import ws_validate as validate


EXPECTED = [
    ("env", ["env"]),
    ("gates", ["gates"]),
    ("baseline-p9", ["baseline", "--ports", "P9"]),
    ("baseline-p92", ["baseline", "--ports", "P92"]),
    ("matrix-b", ["matrix", "--axis", "B", "--ports", "P9"]),
    ("matrix-c", ["matrix", "--axis", "C", "--ports", "P92"]),
    ("matrix-d", ["matrix", "--axis", "D", "--ports", "P92"]),
    ("matrix-e", ["matrix", "--axis", "E", "--ports", "P20"]),
    ("converge", ["converge", "--ports", "P92"]),
    ("report", ["report"]),
]


def _args(root: Path, stage="all", stop_file=None):
    return SimpleNamespace(root=root, stop_file=stop_file, stop_now=False, stage=stage)


def _run_batch(tmp_path, monkeypatch, handlers, *, stage="all", stop_file=None):
    for command, handler in handlers.items():
        monkeypatch.setitem(validate.COMMANDS, command, handler)
    status = validate.Status(tmp_path, "batch")
    run_dir, log = validate._new_run(tmp_path, "batch")
    try:
        code = validate.command_batch(_args(tmp_path, stage, stop_file), status, run_dir, log)
    finally:
        log.close()
    return code, status, run_dir


def _success_handlers(calls, root, *, stop_after_env: Path | None = None):
    def handler(command):
        def run(args, status, run_dir, log):
            calls.append((command, getattr(args, "ports", None), getattr(args, "axis", None)))
            if command == "env":
                common.atomic_json(root / "env.json", {"ok": True})
                if stop_after_env:
                    stop_after_env.write_text("graceful\n", encoding="utf-8")
            return 0
        return run
    return {name: handler(name) for name in ("env", "gates", "baseline", "matrix",
                                               "converge", "report")}


def test_batch_plan_order_stage_subsets_and_fresh_values():
    plan = validate.batch_plan()
    assert [(step["id"], step["argv"]) for step in plan] == EXPECTED
    assert [step["id"] for step in validate.batch_plan("baseline")] == [
        "baseline-p9", "baseline-p92"]
    assert [step["id"] for step in validate.batch_plan("matrix")] == [
        "matrix-b", "matrix-c", "matrix-d", "matrix-e"]
    assert [step["id"] for step in validate.batch_plan("env")] == ["env"]
    assert all(step["label"] and any("가" <= ch <= "힣" for ch in step["label"]) for step in plan)
    plan[0]["argv"].append("changed")
    assert validate.batch_plan()[0]["argv"] == ["env"]


def test_batch_runs_all_conditions_and_persists_step_evidence(tmp_path, monkeypatch):
    calls = []
    code, status, run_dir = _run_batch(
        tmp_path, monkeypatch, _success_handlers(calls, tmp_path))
    assert code == 0
    assert calls == [
        ("env", None, None), ("gates", None, None),
        ("baseline", "P9", None), ("baseline", "P92", None),
        ("matrix", "P9", "B"), ("matrix", "P92", "C"),
        ("matrix", "P92", "D"), ("matrix", "P20", "E"),
        ("converge", "P92", None), ("report", None, None),
    ]
    summary = json.loads((run_dir / "batch_summary.json").read_text(encoding="utf-8"))
    latest = json.loads((tmp_path / "batch_status.json").read_text(encoding="utf-8"))
    assert summary["exit_code"] == 0 and latest["exit_code"] == 0
    assert summary["batch"]["completed"] == 10
    assert all(step["state"] == "completed" for step in summary["batch"]["steps"])
    assert all(Path(step["run_dir"]).joinpath("run_summary.json").is_file()
               for step in summary["batch"]["steps"])
    assert status.value["batch"]["current_id"] is None


def test_batch_fails_fast_on_nonzero_step_and_names_failure(tmp_path, monkeypatch):
    calls = []
    handlers = _success_handlers(calls, tmp_path)
    handlers["gates"] = lambda *args: calls.append(("gates", None, None)) or 1
    code, _, run_dir = _run_batch(tmp_path, monkeypatch, handlers)
    assert code == 1
    assert calls == [("env", None, None), ("gates", None, None)]
    summary = json.loads((run_dir / "batch_summary.json").read_text(encoding="utf-8"))
    failed = summary["batch"]["steps"][1]
    assert failed["id"] == "gates" and failed["state"] == "failed" and failed["exit_code"] == 1
    assert failed["error"]
    assert summary["batch"]["steps"][2]["state"] == "pending"


def test_batch_captures_step_exception_and_stops_queue(tmp_path, monkeypatch):
    calls = []
    handlers = _success_handlers(calls, tmp_path)

    def broken(*args):
        raise RuntimeError("synthetic stage error")

    handlers["baseline"] = broken
    code, _, run_dir = _run_batch(tmp_path, monkeypatch, handlers, stage="baseline")
    assert code == 1
    summary = json.loads((run_dir / "batch_summary.json").read_text(encoding="utf-8"))
    assert summary["batch"]["steps"][0]["state"] == "failed"
    assert "synthetic stage error" in summary["batch"]["steps"][0]["error"]
    assert summary["batch"]["steps"][1]["state"] == "pending"


def test_batch_rejects_diagnostic_env_success(tmp_path, monkeypatch):
    def diagnostic(args, status, run_dir, log):
        common.atomic_json(tmp_path / "env.json", {"ok": False, "errors": ["not configured"]})
        return 0

    code, status, run_dir = _run_batch(tmp_path, monkeypatch, {"env": diagnostic}, stage="env")
    assert code == 1
    summary = json.loads((run_dir / "batch_summary.json").read_text(encoding="utf-8"))
    step = summary["batch"]["steps"][0]
    assert step["state"] == "failed" and "env.json" in step["error"]
    latest = json.loads((tmp_path / "batch_status.json").read_text(encoding="utf-8"))
    assert latest["running"] is False and "env.json" in latest["error"]
    assert status.value["error"] == latest["error"]


def test_batch_stop_between_stages_returns_130_without_launch(tmp_path, monkeypatch):
    calls = []
    stop = tmp_path / "stop"
    handlers = _success_handlers(calls, tmp_path, stop_after_env=stop)
    code, _, run_dir = _run_batch(tmp_path, monkeypatch, handlers, stop_file=stop)
    assert code == 130 and calls == [("env", None, None)]
    summary = json.loads((run_dir / "batch_summary.json").read_text(encoding="utf-8"))
    assert summary["batch"]["steps"][1]["state"] == "stopped"
    assert summary["batch"]["steps"][1]["exit_code"] == 130


def test_batch_rerun_delegates_again_instead_of_skipping_completed_status(tmp_path, monkeypatch):
    calls = []
    handlers = _success_handlers(calls, tmp_path)
    assert _run_batch(tmp_path, monkeypatch, handlers, stage="baseline")[0] == 0
    assert _run_batch(tmp_path, monkeypatch, handlers, stage="baseline")[0] == 0
    assert [call[:2] for call in calls] == [
        ("baseline", "P9"), ("baseline", "P92"),
        ("baseline", "P9"), ("baseline", "P92"),
    ]


def test_main_holds_one_lock_for_whole_batch(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def env(args, status, run_dir, log):
        entered.set()
        assert release.wait(5)
        common.atomic_json(tmp_path / "env.json", {"ok": True})
        return 0

    monkeypatch.setitem(validate.COMMANDS, "env", env)
    result = []
    thread = threading.Thread(target=lambda: result.append(validate.main([
        "batch", "--stage", "env", "--root", str(tmp_path)])))
    thread.start()
    assert entered.wait(5)
    with pytest.raises(RuntimeError, match="another validation run"):
        with common.RunLock(tmp_path, "competitor"):
            pass
    release.set()
    thread.join(10)
    assert result == [0] and not thread.is_alive()


def test_batch_resets_child_comparison_before_next_stage(tmp_path, monkeypatch):
    calls = []
    handlers = _success_handlers(calls, tmp_path)
    original = handlers["baseline"]

    def baseline(args, status, run_dir, log):
        status.update(comparison="passed")
        return original(args, status, run_dir, log)

    handlers["baseline"] = baseline
    code, _, run_dir = _run_batch(tmp_path, monkeypatch, handlers)
    assert code == 0
    summary = json.loads((run_dir / "batch_summary.json").read_text(encoding="utf-8"))
    matrix = next(step for step in summary["batch"]["steps"] if step["id"] == "matrix-b")
    child = json.loads(Path(matrix["run_dir"]).joinpath("run_summary.json").read_text(encoding="utf-8"))
    assert child["comparison"] is None


def test_batch_persists_child_setup_exception(tmp_path, monkeypatch):
    status = validate.Status(tmp_path, "batch")
    run_dir = tmp_path / "batch-run"; run_dir.mkdir()
    log = (run_dir / "log.txt").open("w", encoding="utf-8")
    monkeypatch.setattr(validate, "_new_run", lambda *a, **k: (_ for _ in ()).throw(
        OSError("synthetic setup error")))
    try:
        code = validate.command_batch(_args(tmp_path, "env"), status, run_dir, log)
    finally:
        log.close()
    assert code == 1
    summary = json.loads((run_dir / "batch_summary.json").read_text(encoding="utf-8"))
    step = summary["batch"]["steps"][0]
    assert step["state"] == "failed" and "synthetic setup error" in step["error"]
    assert Path(step["run_dir"]).joinpath("run_summary.json").is_file()


def test_batch_maps_child_keyboard_interrupt_to_stopped(tmp_path, monkeypatch):
    def interrupted(*args):
        raise KeyboardInterrupt("synthetic immediate stop")

    code, _, run_dir = _run_batch(tmp_path, monkeypatch, {"report": interrupted}, stage="report")
    assert code == 130
    summary = json.loads((run_dir / "batch_summary.json").read_text(encoding="utf-8"))
    step = summary["batch"]["steps"][0]
    assert step["state"] == "stopped" and "immediate" in step["error"]


def test_batch_honors_graceful_stop_requested_by_last_child(tmp_path, monkeypatch):
    stop = tmp_path / "stop"

    def report(*args):
        stop.write_text("graceful\n", encoding="utf-8")
        return 0

    code, _, run_dir = _run_batch(tmp_path, monkeypatch, {"report": report},
                                  stage="report", stop_file=stop)
    assert code == 130
    summary = json.loads((run_dir / "batch_summary.json").read_text(encoding="utf-8"))
    assert summary["batch"]["steps"][0]["state"] == "stopped"
