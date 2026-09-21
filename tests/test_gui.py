"""Launcher behavior without starting real experiments or requiring a desktop."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # Configures the same script import path as the packaged entry point.
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
    win.root, win.child, win.log_handle = tmp_path, None, None
    win.command, win.ports, win.axis = Value("matrix"), Value("P20"), Value("E")
    win.status = Value()
    win.save_settings = lambda: True
    win.show_plan = lambda stage, steps=None: None
    win._set_busy = lambda busy: None
    win.start_button = SimpleNamespace(configure=lambda **kwargs: None)
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
