from __future__ import annotations

import importlib.util
import json
import sys
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKSTATION = ROOT / "tools" / "engine_studies" / "workstation"
sys.path.insert(0, str(WORKSTATION))
import common
import ws_validate as validate


def test_common_options_work_after_subcommand(tmp_path):
    args = validate.build_parser().parse_args([
        "matrix", "--axis", "E", "--ports", "P20", "--root", str(tmp_path),
        "--stop-file", str(tmp_path / "stop"), "--stop-now",
    ])
    assert args.root == tmp_path
    assert args.stop_file == tmp_path / "stop"
    assert args.stop_now and args.axis == "E" and args.ports == "P20"


def test_custom_design_manifest_and_data_search(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    spd = data / "custom.spd"; spd.write_bytes(b"spd")
    ref = data / "analysis" / "custom.npz"; ref.parent.mkdir(); ref.write_bytes(b"npz")
    config = {"engine_python": sys.executable, "engine_root": str(ROOT), "data_dir": str(data),
              "designs": {"heldout": {"spd": "custom.spd", "reference": "custom.npz",
                                         "family": "pcb", "ports": ["P1"]}}}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    settings = common.load_settings(tmp_path)
    assert settings.designs["heldout"].spd == spd
    assert settings.designs["heldout"].reference == ref
    assert settings.designs["heldout"].ports == ("P1",)


def test_resume_requires_full_identity(tmp_path):
    receipts = tmp_path / "receipts"; receipts.mkdir()
    common.atomic_json(receipts / "one.json", {"numerics_id": "n", "freq": [1.0],
        "Z_re": [1.0], "Z_im": [0.0], "study": {"schema_version": 1,
        "input_identity": "a" * 64, "finished_at": common.utc_now(),
        "source_spd_sha256": "b" * 64, "command": "baseline"}})
    assert common.resumable_receipt(receipts, "a" * 64).name == "one.json"
    assert common.resumable_receipt(receipts, "b" * 64) is None


def test_windows_safe_process_liveness():
    assert common.process_alive(__import__("os").getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    assert not common.process_alive(child.pid)


def test_skip_only_or_missing_required_gate_never_passes():
    skipped = [{"classname": "x.test_reproduction", "name": "test_variant_p_cases[x]",
                "state": "skipped"}]
    ok, evidence = validate._required_gate_outcomes("default", skipped)
    assert not ok
    assert evidence["reproduction_skipped"] == 1


def test_default_gate_needs_named_data_reproductions():
    cases = [
        {"classname": "x.test_reproduction", "name": "test_variant_p_cases[a]", "state": "passed"},
        {"classname": "x.test_reproduction", "name": "test_variant_p_cases[b]", "state": "passed"},
        {"classname": "x.test_reproduction", "name": "test_pcb_s5m6585[a]", "state": "passed"},
        {"classname": "x.test_reproduction", "name": "test_attach_reference_port14", "state": "passed"},
    ]
    assert validate._required_gate_outcomes("default", cases)[0]


def test_missing_config_env_is_diagnostic_success(tmp_path):
    assert validate.main(["env", "--root", str(tmp_path)]) == 0
    record = json.loads((tmp_path / "env.json").read_text(encoding="utf-8"))
    assert record["configured_engine"] is False
    assert record["ok"] is False
    assert record["errors"]


def test_engine_import_is_confined_to_worker_source():
    for name in ("common.py", "ws_validate.py"):
        source = (WORKSTATION / name).read_text(encoding="utf-8")
        assert "import spd_pi_engine" not in source
        assert "from spd_pi_engine" not in source


def test_three_passing_gate_suites_finalize_engine_evidence(tmp_path, monkeypatch):
    data = tmp_path / "data"; data.mkdir()
    for item in common.DEFAULT_DESIGNS.values():
        (data / item["spd"]).write_bytes(b"spd")
        (data / item["reference"]).write_bytes(b"ref")
    (tmp_path / "config.json").write_text(json.dumps({
        "engine_python": sys.executable, "engine_root": str(ROOT), "data_dir": str(data),
    }), encoding="utf-8")
    packages = {k: v for k, v in validate.REQUIRED_VERSIONS.items() if k != "python"}
    probe = {"engine": {"module": str(ROOT / "src" / "spd_pi_engine" / "__init__.py"),
                         "source_hash": "f" * 64},
             "python": {"version": validate.REQUIRED_VERSIONS["python"]}, "packages": packages}
    monkeypatch.setattr(validate, "_call_worker", lambda *a, **k: probe)

    def fake_run(command, **kwargs):
        xml_arg = next(x for x in command if x.startswith("--junitxml="))
        path = Path(xml_arg.split("=", 1)[1])
        if "--gpu" in command:
            names = ["test_variant_p_cases_gpu[a]", "test_variant_p_cases_gpu[b]",
                     "test_pcb_s5m6585_gpu[a]"]
        elif "--slow" in command:
            names = ["test_legacy_port18_1mhz"] + [f"test_variant_p_cases[{i}]" for i in range(7)] \
                + ["test_pcb_s5m6585[a]", "test_pcb_s5m6585[b]"]
        else:
            names = ["test_variant_p_cases[a]", "test_variant_p_cases[b]",
                     "test_pcb_s5m6585[a]", "test_attach_reference_port14"]
        body = "".join(f'<testcase classname="x.test_reproduction" name="{name}" />' for name in names)
        path.write_text(f'<testsuite tests="{len(names)}">{body}</testsuite>', encoding="utf-8")
        return 0, None
    monkeypatch.setattr(validate, "_run_process", fake_run)
    status = SimpleNamespace(update=lambda **kw: None)
    args = SimpleNamespace(root=tmp_path, stop_file=None, stop_now=False)
    run_dir = tmp_path / "run"; run_dir.mkdir()
    with (run_dir / "log").open("w", encoding="utf-8") as log:
        assert validate.command_gates(args, status, run_dir, log) == 0
    gates = json.loads((tmp_path / "gates.json").read_text(encoding="utf-8"))
    assert gates["ok"] is True
    assert gates["engine_git"] == gates["engine_evidence"]["engine_git"]


def test_fingerprint_ignores_free_memory_but_tracks_driver(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    config = tmp_path / "config.json"; config.write_text("{}", encoding="utf-8")
    settings = common.Settings(tmp_path, Path(sys.executable), ROOT, data)
    probe = {"engine": {"source_hash": "a", "module": "m"},
             "python": {"version": "3.12.10"}, "packages": {},
             "profiles": {"detected": {"cpu_physical": 32, "cpu_logical": 64,
                 "ram_total_GB": 512, "ram_free_GB": 400,
                 "gpus": [{"index": 0, "name": "A6000", "vram_total_MB": 49140,
                            "vram_free_MB": 40000}]}},
             "nvidia_smi": {"gpus": [{"index": 0, "name": "A6000", "driver": "1",
                                        "vram_total_MB": 49140, "vram_free_MB": 40000}]}}
    first = validate._engine_fingerprint(settings, probe)[0]
    changed_free = json.loads(json.dumps(probe)); changed_free["profiles"]["detected"]["ram_free_GB"] = 1
    changed_free["profiles"]["detected"]["gpus"][0]["vram_free_MB"] = 2
    changed_free["nvidia_smi"]["gpus"][0]["vram_free_MB"] = 3
    assert validate._engine_fingerprint(settings, changed_free)[0] == first
    changed_driver = json.loads(json.dumps(probe)); changed_driver["nvidia_smi"]["gpus"][0]["driver"] = "2"
    assert validate._engine_fingerprint(settings, changed_driver)[0] != first


def test_matrix_plan_b_then_e_preserves_both_axes(tmp_path):
    run_b = tmp_path / "run-b"; run_b.mkdir()
    first = validate._publish_matrix_plan(
        tmp_path, run_b, {"schema_version": 1, "created_at": "first", "axes": {"B": {"jobs": [1, 4]}}})
    assert set(first["axes"]) == {"B"}
    run_e = tmp_path / "run-e"; run_e.mkdir()
    second = validate._publish_matrix_plan(
        tmp_path, run_e, {"schema_version": 1, "created_at": "second", "axes": {"E": {"jobs": [1, 2]}}})
    assert set(second["axes"]) == {"B", "E"}
    assert second["axes"]["B"] == {"jobs": [1, 4]}
    archived = list((run_e / "archives").glob("matrix_plan.before*.json"))
    assert len(archived) == 1
    assert set(json.loads(archived[0].read_text(encoding="utf-8"))["axes"]) == {"B"}
