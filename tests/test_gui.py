"""Launcher behavior without starting real experiments or requiring a desktop."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # Configures the same script import path as the packaged entry point.
from common import atomic_json
import workstation_gui as gui


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = str(value)


@pytest.fixture
def window(tmp_path, monkeypatch):
    win = gui.Window.__new__(gui.Window)
    win.root, win.child, win.child_kind, win.log_handle = tmp_path, None, None, None
    win.pending_launch = None
    win.preparation_data = {"ok": True, "data_dir": str(tmp_path)}
    win.command, win.ports, win.axis = Value("matrix"), Value("P20"), Value("E")
    win.status = Value()
    win.preparation_status, win.runtime_status = Value(), Value()
    win.fields = {key: Value(str(tmp_path) if key == "data_dir" else "") for key in (
        "engine_python", "engine_root", "data_dir", "laptop_receipts", "laptop_freeze")}
    (tmp_path / "config.json").write_text(json.dumps({
        "data_dir": str(tmp_path), "configuration_mode": "manual"}), encoding="utf-8")
    (tmp_path / "preparation.json").write_text(
        json.dumps(win.preparation_data), encoding="utf-8")
    win.show_plan = lambda stage, steps=None: None
    win._set_busy = lambda busy: None
    win.start_button = SimpleNamespace(configure=lambda **kwargs: None)
    win.setting_widgets = []
    win.master = SimpleNamespace(after=lambda *args: None)
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args: errors.append(args))
    win.errors = errors
    return win


@pytest.mark.parametrize("stage", ["all", "matrix", "baseline", "converge"])
def test_batch_launch_uses_fixed_plan_not_manual_filters(window, monkeypatch, stage):
    captured = []
    monkeypatch.setattr(gui.subprocess, "Popen", lambda args, **kwargs:
                        captured.append(args) or SimpleNamespace(poll=lambda: None))
    stop = window.root / "gui-stop.request"
    stop.write_text("now")
    window.start_batch(stage)
    try:
        assert not window.errors
        args = captured[0]
        assert args[len(gui.runner_command()):len(gui.runner_command()) + 3] == ["batch", "--stage", stage]
        assert "--ports" not in args and "--axis" not in args
        assert args[args.index("--stop-file") + 1] == str(stop)
        assert not stop.exists()
        window.start_batch("all")
        assert len(captured) == 1, "A running batch must not be launched twice"
    finally:
        if window.log_handle:
            window.log_handle.close()


def test_manual_launch_keeps_selected_conditions(window, monkeypatch):
    captured = []
    monkeypatch.setattr(gui.subprocess, "Popen", lambda args, **kwargs:
                        captured.append(args) or SimpleNamespace(poll=lambda: None))
    window.start()
    try:
        args = captured[0]
        assert args[args.index("--ports") + 1] == "P20"
        assert args[args.index("--axis") + 1] == "E"
    finally:
        if window.log_handle:
            window.log_handle.close()


def test_null_optional_settings_stay_empty(window):
    window.fields = {key: Value() for key in (
        "engine_python", "engine_root", "data_dir", "laptop_receipts", "laptop_freeze")}
    config = {key: None for key in ("laptop_receipts", "laptop_freeze")}
    (window.root / "config.json").write_text(json.dumps(config))
    window.load_settings()
    assert window.fields["laptop_receipts"].get() == ""
    assert window.fields["laptop_freeze"].get() == ""


def test_save_settings_preserves_unknown_and_legacy_fields(window):
    original = {"data_dir": "old", "legacy_value": {"keep": True}, "designs": {"x": {}},
                "engine_mode": "bundled", "engine_python": None, "engine_root": None}
    (window.root / "config.json").write_text(json.dumps(original), encoding="utf-8")
    window.fields["data_dir"].set(str(window.root))
    assert window.save_settings()
    saved = json.loads((window.root / "config.json").read_text(encoding="utf-8"))
    assert saved["legacy_value"] == {"keep": True}
    assert saved["designs"] == {"x": {}}
    assert saved["data_dir"] == str(window.root)
    assert saved["engine_mode"] == "bundled"
    assert saved["engine_python"] is None and saved["engine_root"] is None


def test_save_settings_does_not_rewrite_unchanged_generated_config(window):
    generated = {"configuration_mode": "folder", "data_dir": str(window.root),
                 "engine_mode": "bundled", "engine_python": None, "engine_root": None,
                 "designs": {"board": {}}, "port_sets": {"P9": {}}}
    atomic_json(window.root / "config.json", generated)
    before = (window.root / "config.json").read_bytes()
    assert window.save_settings()
    assert (window.root / "config.json").read_bytes() == before


def test_advanced_external_engine_requires_pair_and_switches_mode(window):
    atomic_json(window.root / "config.json", {
        "configuration_mode": "folder", "data_dir": str(window.root),
        "engine_mode": "bundled", "engine_python": None, "engine_root": None})
    before = (window.root / "config.json").read_bytes()
    window.fields["engine_python"].set("C:/external/python.exe")
    assert window.save_settings() is False
    assert (window.root / "config.json").read_bytes() == before

    window.fields["engine_root"].set("C:/external/engine")
    assert window.save_settings() is True
    saved = json.loads((window.root / "config.json").read_text(encoding="utf-8"))
    assert saved["engine_mode"] == "external"
    assert saved["engine_python"] == "C:/external/python.exe"
    assert saved["engine_root"] == "C:/external/engine"


def test_preparation_summary_reports_discovered_files_and_ports():
    text = gui.Window._format_preparation({
        "ok": True,
        "designs": {"board": {"spd": "board.spd", "reference": "prepared.npz",
                                "ports": ["A", "B", "C"]}},
        "port_sets": {"P9": {"board": ["A", "B"]}, "P20": {"board": ["A", "B", "C"]}},
        "metadata": {"designs": {"board": {"source_touchstone": "board.s16p"}}},
    })
    assert "설계 1개" in text
    assert "발견 파일 2개" in text
    assert "포트 3개" in text
    assert "board.spd" in text


def test_unprepared_run_prepares_then_launches_selected_conditions(window, monkeypatch):
    spawned = []

    def spawn(args, label, kind):
        spawned.append((args, label, kind))
        window.child = SimpleNamespace(poll=lambda: None)
        window.child_kind = kind

    monkeypatch.setattr(window, "_spawn", spawn)
    window.preparation_data = {}
    (window.root / "config.json").write_text(json.dumps({
        "data_dir": str(window.root), "configuration_mode": "folder"}), encoding="utf-8")
    (window.root / "preparation.json").unlink()
    window.start()
    assert len(spawned) == 1
    assert spawned[0][2] == "prepare"
    prepare_args = spawned[0][0][len(gui.runner_command()):]
    assert prepare_args[:5] == ["prepare", "--root", str(window.root),
                                "--data-dir", str(window.root)]
    assert prepare_args[prepare_args.index("--stop-file") + 1] == str(
        window.root / "gui-stop.request")

    ready = {"ok": True, "data_dir": str(window.root), "designs": {"board": {}}, "ports": ["P1"]}
    (window.root / "preparation.json").write_text(json.dumps(ready), encoding="utf-8")
    window._finish_child(0)
    assert len(spawned) == 2
    run_args, _, kind = spawned[1]
    assert kind == "run"
    assert run_args[run_args.index("--ports") + 1] == "P20"
    assert run_args[run_args.index("--axis") + 1] == "E"


def test_changed_folder_prepares_even_for_explicit_manual_config(window, tmp_path, monkeypatch):
    selected = tmp_path / "new-data"
    selected.mkdir()
    window.fields["data_dir"].set(str(selected))
    spawned = []
    monkeypatch.setattr(window, "_spawn", lambda args, label, kind: spawned.append((args, kind)))
    window.start()
    assert [kind for _, kind in spawned] == ["prepare"]
    assert spawned[0][0][spawned[0][0].index("--data-dir") + 1] == str(selected)


def test_prepare_failure_does_not_launch_requested_run(window, monkeypatch):
    spawned = []

    def spawn(args, label, kind):
        spawned.append((args, label, kind))
        window.child = SimpleNamespace(poll=lambda: None)
        window.child_kind = kind

    monkeypatch.setattr(window, "_spawn", spawn)
    window.preparation_data = {}
    selected = window.root / "new-data"
    selected.mkdir()
    window.fields["data_dir"].set(str(selected))
    atomic_json(window.root / "config.json", {
        "data_dir": str(window.root), "configuration_mode": "manual",
        "designs": {"old": {"keep": True}}, "legacy": "preserve"})
    config_before = (window.root / "config.json").read_bytes()
    (window.root / "preparation.json").unlink()
    window.start_batch("all")
    assert [item[2] for item in spawned] == ["prepare"]
    window._finish_child(2)
    assert [item[2] for item in spawned] == ["prepare"]
    assert window.pending_launch is None
    assert "실패" in window.status.get()
    assert (window.root / "config.json").read_bytes() == config_before
    assert window.fields["data_dir"].get() == str(selected)


def test_prepare_stop_does_not_launch_requested_run(window, monkeypatch):
    spawned = []

    def spawn(args, label, kind):
        spawned.append((args, label, kind))
        window.child = SimpleNamespace(poll=lambda: None)
        window.child_kind = kind

    monkeypatch.setattr(window, "_spawn", spawn)
    (window.root / "config.json").write_text(json.dumps({
        "data_dir": str(window.root), "configuration_mode": "folder"}), encoding="utf-8")
    window.start()
    (window.root / "gui-stop.request").write_text("now", encoding="utf-8")
    ready = {"ok": True, "data_dir": str(window.root), "designs": {"board": {}}, "ports": ["P1"]}
    (window.root / "preparation.json").write_text(json.dumps(ready), encoding="utf-8")
    window._finish_child(0)
    assert [item[2] for item in spawned] == ["prepare"]
    assert window.pending_launch is None
    assert "중단" in window.status.get()


def test_launch_error_closes_log_and_restores_buttons(window, monkeypatch):
    busy = []
    window._set_busy = busy.append
    def fail(*args, **kwargs):
        raise OSError("cannot launch")
    monkeypatch.setattr(gui.subprocess, "Popen", fail)
    window.start_batch("all")
    assert window.errors
    assert window.log_handle is None
    assert window.child is None
    assert busy[-1] is False


def test_finished_batch_shows_full_progress_instead_of_empty_last_step(window):
    progress = []
    window.progress = SimpleNamespace(configure=lambda **values: progress.append(values))
    window.last_batch = None
    (window.root / "status.json").write_text(json.dumps({
        "running": False, "exit_code": 0, "completed": 0, "remaining": 0,
        "batch": {"stage": "all", "completed": 10, "total": 10, "steps": []},
    }), encoding="utf-8")
    window.refresh()
    assert progress[-1] == {"maximum": 10, "value": 10}


@pytest.mark.parametrize("now,expected", [(False, "graceful"), (True, "now")])
def test_batch_stop_reaches_shared_request_file(window, now, expected):
    window.child = SimpleNamespace(poll=lambda: None)
    window.stop(now)
    assert (window.root / "gui-stop.request").read_text() == expected
