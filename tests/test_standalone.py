"""Standalone orchestration uses the installed engine and publishes settings only after preparation."""
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/engine_studies/workstation"))
import common
import ws_validate as validate


def test_prepare_publishes_folder_config_and_failure_preserves_it(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    data = tmp_path / "data"; data.mkdir()
    monkeypatch.setattr(validate, "bundled_runtime", lambda: (runtime / "python.exe", runtime))
    monkeypatch.setattr(common, "bundled_runtime", lambda: (runtime / "python.exe", runtime))
    prepared = {"designs": {"board": {"spd": "board.spd", "reference": "board.npz", "family": "package"}},
                "port_sets": {key: {"board": ["PortA"]} for key in ("P9", "P20", "P92", "E4")}}
    monkeypatch.setattr(validate, "_call_worker", lambda *a, **kw: prepared)
    args = SimpleNamespace(root=tmp_path, data_dir=data, stop_file=None, stop_now=False)
    status = SimpleNamespace(update=lambda **kw: None)
    run = tmp_path / "run"; run.mkdir()
    assert validate.command_prepare(args, status, run, io.StringIO()) == 0
    config_path = tmp_path / "config.json"
    before = config_path.read_bytes()
    config = json.loads(before)
    assert config["engine_mode"] == "bundled" and config["engine_python"] is None
    assert config["configuration_mode"] == "folder"
    assert config["designs"] == prepared["designs"]
    assert validate.command_prepare(args, status, run, io.StringIO()) == 0
    assert config_path.read_bytes() == before
    def fail(*args, **kwargs):
        raise ValueError("ambiguous reference")
    monkeypatch.setattr(validate, "_call_worker", fail)
    with pytest.raises(ValueError, match="ambiguous"):
        validate.command_prepare(args, status, run, io.StringIO())
    assert config_path.read_bytes() == before
    assert json.loads((tmp_path / "preparation.json").read_text(encoding="utf-8"))["ok"] is False


def test_installed_paths_override_stale_machine_paths_in_bundled_mode(tmp_path, monkeypatch):
    expected = (tmp_path / "runtime/python.exe", tmp_path / "runtime")
    monkeypatch.setattr(common, "bundled_runtime", lambda: expected)
    assert common.engine_paths(tmp_path, {"engine_mode": "bundled", "engine_python": "old/python",
                                        "engine_root": "old/root"}) == expected
    assert common.engine_paths(tmp_path, {}) == expected


def test_folder_input_change_invalidates_gate_even_without_config_edit(tmp_path, monkeypatch):
    spd = tmp_path / "design.spd"; spd.write_bytes(b"original")
    ref = tmp_path / "ref.npz"; ref.write_bytes(b"reference")
    common.atomic_json(tmp_path / "config.json", {"data_dir": str(tmp_path)})
    settings = common.Settings(tmp_path, Path(sys.executable), tmp_path, tmp_path,
                               designs={"board": common.DesignSpec("board", spd, ref, "unknown")},
                               configuration_mode="folder")
    monkeypatch.setattr(validate, "_git_head", lambda root: {})
    original = validate._engine_fingerprint(settings, {})[0]
    spd.write_bytes(b"changed")
    assert validate._engine_fingerprint(settings, {})[0] != original


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing violation")
def test_atomic_status_retries_transient_windows_reader(tmp_path, monkeypatch):
    replace = common.os.replace
    calls = []
    def transient(source, target):
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError("reader still open")
        replace(source, target)
    monkeypatch.setattr(common.os, "replace", transient)
    common.atomic_json(tmp_path / "status.json", {"ok": True})
    assert len(calls) == 2
    assert json.loads((tmp_path / "status.json").read_text()) == {"ok": True}
