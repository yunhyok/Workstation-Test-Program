from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "engine_studies" / "workstation"))
import result_bundle


def put(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def unpack(path: Path) -> tuple[set[str], dict]:
    with zipfile.ZipFile(path) as bundle:
        names = set(bundle.namelist())
        manifest = json.loads(bundle.read("manifest.json"))
    return names, manifest


def test_success_bundle_includes_diagnostics_and_excludes_raw_inputs(tmp_path):
    put(tmp_path, "config.json", "{}")
    put(tmp_path, "summary.json", '{"overall_status":"PASS"}')
    put(tmp_path, "W15_REPORT.md", "pass")
    put(tmp_path, "status.json", '{"subcommand":"batch","running":false,"exit_code":0}')
    put(tmp_path, "batch_status.json", '{"running":false,"exit_code":0,"batch":{"completed":2,"total":2,"steps":[{"id":"report","state":"completed"}]}}')
    put(tmp_path, "receipts/one.json", '{"case":1}')
    put(tmp_path, "comparisons/one.json", "{}")
    put(tmp_path, "figures/w15_wall_jobs_threads.png", "png")
    put(tmp_path, "launcher-logs/failed-launch.txt", "error")
    put(tmp_path, "archives/summary_older.json", "{}")
    put(tmp_path, "runs/one/run_summary.json", "{}")
    put(tmp_path, "runs/one/log.txt", "completed")
    put(tmp_path, "runs/one/worker/test.request.json", "{}")
    put(tmp_path, "runs/one/worker/test.output.json", "{}")
    put(tmp_path, "runs/one/comparison-inputs/case/left/one.json", "{}")
    put(tmp_path, "runs/one/comparison_failures/one.json", "{}")
    put(tmp_path, "runs/one/archives/matrix_plan.before.json", "{}")
    for name in ("source.spd", "source.s2p", "cache/output.npz", "prepared/data.json",
                 "runs/one/worker/raw.npz", "runs/one/comparison-inputs/case/left/raw.spd",
                 "secrets.json", "runs/one/secret.txt", "stop", "run.lock"):
        put(tmp_path, name, "secret")

    bundle = result_bundle.export_results(tmp_path)
    names, manifest = unpack(bundle)
    assert bundle.parent == tmp_path / "exports"
    assert manifest["snapshot_state"] == "final"
    assert manifest["overall_status"] == "PASS"
    assert "runs/one/worker/test.output.json" in names
    assert "runs/one/comparison-inputs/case/left/one.json" in names
    assert "receipts/one.json" in names
    assert "launcher-logs/failed-launch.txt" in names
    assert not any("secret" in name or name.endswith((".spd", ".s2p", ".npz")) for name in names)
    assert json.loads((tmp_path / "export.json").read_text(encoding="utf-8"))["path"] == str(bundle)

    second = result_bundle.export_results(tmp_path)
    assert second != bundle and bundle.exists()
    assert not any(name.startswith("exports/") for name in unpack(second)[0])


def test_failed_batch_and_running_state_are_not_reported_as_pass(tmp_path):
    put(tmp_path, "summary.json", '{"overall_status":"PASS"}')
    put(tmp_path, "W15_REPORT.md", "old report")
    put(tmp_path, "status.json", '{"subcommand":"batch","running":false,"exit_code":1}')
    put(tmp_path, "batch_status.json", '{"running":false,"exit_code":1,"batch":{"completed":1,"total":3}}')
    _, manifest = unpack(result_bundle.export_results(tmp_path))
    assert manifest["snapshot_state"] == "partial"
    assert manifest["overall_status"] == "FAIL"
    assert "config.json" in manifest["missing_files"]

    put(tmp_path, "status.json", '{"subcommand":"batch","running":true,"exit_code":null}')
    put(tmp_path, "batch_status.json", '{"running":true,"exit_code":null,"batch":{"completed":1,"total":3}}')
    _, manifest = unpack(result_bundle.export_results(tmp_path))
    assert manifest["snapshot_state"] == "running"
    assert manifest["overall_status"] == "INCOMPLETE"


def test_old_pass_report_is_not_claimed_for_new_nonreport_run(tmp_path):
    put(tmp_path, "summary.json", '{"overall_status":"PASS"}')
    put(tmp_path, "W15_REPORT.md", "old report")
    put(tmp_path, "status.json", '{"subcommand":"env","running":false,"exit_code":0}')
    _, manifest = unpack(result_bundle.export_results(tmp_path))
    assert manifest["snapshot_state"] == "partial"
    assert manifest["overall_status"] == "INCOMPLETE"


def test_unfinished_report_cannot_claim_pass(tmp_path):
    put(tmp_path, "summary.json", '{"overall_status":"PASS"}')
    put(tmp_path, "W15_REPORT.md", "old report")
    put(tmp_path, "status.json", '{"subcommand":"report","running":false,"exit_code":null}')
    _, manifest = unpack(result_bundle.export_results(tmp_path))
    assert manifest["snapshot_state"] == "partial"
    assert manifest["overall_status"] == "INCOMPLETE"


def test_malformed_records_yield_diagnostic_manifest(tmp_path):
    put(tmp_path, "summary.json", "[]")
    put(tmp_path, "W15_REPORT.md", "old report")
    put(tmp_path, "status.json", "[]")
    put(tmp_path, "batch_status.json", '{"running":false,"batch":[]}')
    _, manifest = unpack(result_bundle.export_results(tmp_path))
    assert manifest["snapshot_state"] == "inconsistent"
    assert manifest["overall_status"] == "INCOMPLETE"
    assert set(manifest["issues"]) == {
        "malformed status.json", "malformed batch_status.json", "malformed summary.json"}


def test_symlinks_are_not_followed(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    put(outside, "private.json", '{"token":"secret"}')
    try:
        (tmp_path / "receipts").symlink_to(outside, target_is_directory=True)
        (tmp_path / "status.json").symlink_to(outside / "private.json")
    except OSError:
        pytest.skip("symlink creation unavailable")
    names, manifest = unpack(result_bundle.export_results(tmp_path))
    assert "receipts/private.json" not in names
    assert "status.json" not in names
    assert "status.json" in manifest["missing_files"]


def test_changed_file_keeps_captured_bytes_and_marks_inconsistent(tmp_path, monkeypatch):
    put(tmp_path, "receipts/one.json", "old")
    original = result_bundle._stamp
    calls = 0

    def changing_stamp(path, root):
        nonlocal calls
        if path == tmp_path / "receipts" / "one.json":
            calls += 1
            if calls == 3:
                path.write_text("new and longer", encoding="utf-8")
        return original(path, root)

    monkeypatch.setattr(result_bundle, "_stamp", changing_stamp)
    bundle = result_bundle.export_results(tmp_path)
    names, manifest = unpack(bundle)
    assert "receipts/one.json" in names
    assert manifest["snapshot_state"] == "inconsistent"
    assert manifest["changed_files"] == ["receipts/one.json"]
    with zipfile.ZipFile(bundle) as archive:
        assert archive.read("receipts/one.json") == b"old"


def test_inaccessible_directory_aborts_instead_of_claiming_pass(tmp_path, monkeypatch):
    put(tmp_path, "receipts/one.json", "{}")
    original = Path.iterdir

    def inaccessible(path):
        if path == tmp_path / "receipts":
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", inaccessible)
    with pytest.raises(PermissionError):
        result_bundle.export_results(tmp_path)
    assert not list((tmp_path / "exports").glob("*.zip"))


def test_partial_zip_entry_is_in_inventory_and_marked_inconsistent(tmp_path, monkeypatch):
    put(tmp_path, "receipts/one.json", "partial")
    original = result_bundle.os.fdopen
    used = False

    class FailsAfterFirstRead:
        def __init__(self, source):
            self.source = source
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def fileno(self):
            return self.source.fileno()

        def read(self, size):
            self.reads += 1
            if self.reads > 1:
                raise OSError("read failed")
            return self.source.read(size)

    def fdopen(fd, mode, *args, **kwargs):
        nonlocal used
        source = original(fd, mode, *args, **kwargs)
        if not used:
            used = True
            return FailsAfterFirstRead(source)
        return source

    monkeypatch.setattr(result_bundle.os, "fdopen", fdopen)
    names, manifest = unpack(result_bundle.export_results(tmp_path))
    assert "receipts/one.json" in names
    assert "receipts/one.json" in manifest["included_files"]
    assert manifest["skipped_files"] == [
        {"path": "receipts/one.json", "reason": "partial ZIP entry: OSError"}]
    assert manifest["snapshot_state"] == "inconsistent"
    assert manifest["overall_status"] == "INCOMPLETE"
