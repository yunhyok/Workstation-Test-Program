from __future__ import annotations

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


def _write_config(root: Path) -> Path:
    data = root / "data"
    data.mkdir(exist_ok=True)
    (data / "board.spd").write_bytes(b"spd")
    (data / "board.npz").write_bytes(b"npz")
    config = {
        "engine_python": sys.executable,
        "engine_root": str(ROOT),
        "data_dir": str(data),
        "designs": {
            "design_a": {
                "spd": "board.spd", "reference": "board.npz",
                "family": "pcb", "ports": ["PortA", "PortB"],
            }
        },
        "port_sets": {
            "P9": {"design_a": ["PortA"]},
            "P20": {"design_a": ["PortA", "PortB"]},
            "P92": {"design_a": "all"},
            "E4": {"design_a": ["PortB"]},
        },
    }
    path = root / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_common_options_work_after_subcommand(tmp_path):
    args = validate.build_parser().parse_args([
        "matrix", "--axis", "E", "--ports", "P20", "--root", str(tmp_path),
        "--stop-file", str(tmp_path / "stop"), "--stop-now",
    ])
    assert args.root == tmp_path
    assert args.stop_file == tmp_path / "stop"
    assert args.stop_now and args.axis == "E" and args.ports == "P20"
    subparsers = next(action for action in validate.build_parser()._actions
                      if hasattr(action, "choices") and action.choices)
    assert "in-flight jobs" in subparsers.choices["baseline"].format_help()


def test_custom_design_manifest_and_data_search(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    spd = data / "custom.spd"; spd.write_bytes(b"spd")
    ref = data / "analysis" / "custom.npz"; ref.parent.mkdir(); ref.write_bytes(b"npz")
    config = {"engine_python": sys.executable, "engine_root": str(ROOT), "data_dir": str(data),
              "designs": {"heldout": {"spd": "custom.spd", "reference": "custom.npz",
                                         "family": "pcb", "ports": ["P1"]}},
              "port_sets": {"P9": {"heldout": ["P1"]}, "P20": {"heldout": ["P1"]},
                            "P92": {"heldout": "all"}, "E4": {"heldout": ["P1"]}}}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    settings = common.load_settings(tmp_path)
    assert settings.designs["heldout"].spd == spd
    assert settings.designs["heldout"].reference == ref
    assert settings.designs["heldout"].ports == ("P1",)
    assert settings.port_sets["P92"]["heldout"] == "all"


def test_config_requires_explicit_designs_and_port_sets(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "engine_python": sys.executable, "engine_root": str(ROOT), "data_dir": str(tmp_path),
    }), encoding="utf-8")
    with pytest.raises(common.ConfigError, match="designs"):
        common.load_settings(tmp_path)


def test_config_example_parses_with_optional_design_ports_omitted(tmp_path):
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    (tmp_path / "config.json").write_text(json.dumps(example), encoding="utf-8")
    settings = common.load_settings(tmp_path, require_paths=False)
    assert settings.designs["design_a"].ports == ()


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
    skipped = [{"classname": "x.test_reproduction", "name": "test_variant_cases[x]",
                "state": "skipped"}]
    ok, evidence = validate._required_gate_outcomes("default", skipped)
    assert not ok
    assert evidence["reproduction_skipped"] == 1


def test_default_gate_needs_named_data_reproductions():
    cases = [
        {"classname": "x.test_reproduction", "name": "test_variant_cases[a]", "state": "passed"},
        {"classname": "x.test_reproduction", "name": "test_variant_cases[b]", "state": "passed"},
        {"classname": "x.test_reproduction", "name": "test_pcb_case[a]", "state": "passed"},
        {"classname": "x.test_reproduction", "name": "test_attach_reference_case", "state": "passed"},
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
    _write_config(tmp_path)
    packages = {k: v for k, v in validate.REFERENCE_VERSIONS.items() if k != "python"}
    packages["pydantic"] = "soft-difference"
    probe = {"engine": {"module": str(ROOT / "src" / "spd_pi_engine" / "__init__.py"),
                         "source_hash": "f" * 64},
             "python": {"version": validate.HARD_VERSIONS["python"]}, "packages": packages}
    monkeypatch.setattr(validate, "_call_worker", lambda settings, request, *a, **k:
                        probe if request["operation"] == "probe" else {"ok": True})

    def fake_run(command, **kwargs):
        xml_arg = next(x for x in command if x.startswith("--junitxml="))
        path = Path(xml_arg.split("=", 1)[1])
        if "--gpu" in command:
            names = ["test_variant_cases_gpu[a]", "test_variant_cases_gpu[b]",
                     "test_pcb_case_gpu[a]"]
        elif "--slow" in command:
            names = ["test_legacy_case"] + [f"test_variant_cases[{i}]" for i in range(7)] \
                + ["test_pcb_case[a]", "test_pcb_case[b]"]
        else:
            names = ["test_variant_cases[a]", "test_variant_cases[b]",
                     "test_pcb_case[a]", "test_attach_reference_case"]
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
    assert gates["version_differences"]["pydantic"]["actual"] == "soft-difference"


def test_env_records_soft_version_difference_without_failing(tmp_path, monkeypatch):
    _write_config(tmp_path)
    packages = {k: v for k, v in validate.REFERENCE_VERSIONS.items() if k != "python"}
    packages["pydantic"] = "soft-difference"
    probe = {"engine": {"module": "m", "source_hash": "f" * 64},
             "python": {"version": validate.HARD_VERSIONS["python"]}, "packages": packages,
             "all_packages": {}}
    monkeypatch.setattr(validate, "_call_worker", lambda *a, **k: probe)
    status = SimpleNamespace(update=lambda **kw: None)
    args = SimpleNamespace(root=tmp_path, stop_file=None, stop_now=False)
    run_dir = tmp_path / "run"; run_dir.mkdir()
    with (run_dir / "log").open("w", encoding="utf-8") as log:
        assert validate.command_env(args, status, run_dir, log) == 0
    record = json.loads((tmp_path / "env.json").read_text(encoding="utf-8"))
    assert record["ok"] is True
    assert record["version_differences"]["pydantic"]["actual"] == "soft-difference"


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


def test_only_runtime_core_versions_are_hard_gates():
    assert validate.HARD_VERSIONS == {
        "python": "3.12.10", "numpy": "2.4.4", "scipy": "1.18.0",
    }
    assert len(validate.REFERENCE_VERSIONS) == 10
    assert "pydantic" in validate.REFERENCE_VERSIONS


def test_soft_version_difference_unlocks_only_when_gate_fingerprint_matches(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    settings = common.Settings(tmp_path, Path(sys.executable), ROOT, tmp_path)
    probe = {"engine": {"source_hash": "a", "module": "m"},
             "python": {"version": validate.HARD_VERSIONS["python"]},
             "packages": {"numpy": validate.HARD_VERSIONS["numpy"],
                          "scipy": validate.HARD_VERSIONS["scipy"], "pydantic": "soft-difference"}}
    fingerprint = validate._engine_fingerprint(settings, probe)[0]
    common.atomic_json(tmp_path / "gates.json", {
        "ok": True, "suites": [{"ok": True}] * 3, "engine_fingerprint": fingerprint,
    })
    validate._measurement_unlocked(settings, probe)

    changed_after_gate = json.loads(json.dumps(probe))
    changed_after_gate["packages"]["pydantic"] = "changed-again"
    with pytest.raises(RuntimeError, match="changed after gates"):
        validate._measurement_unlocked(settings, changed_after_gate)

    hard_mismatch = json.loads(json.dumps(probe))
    hard_mismatch["packages"]["numpy"] = "wrong"
    hard_fingerprint = validate._engine_fingerprint(settings, hard_mismatch)[0]
    common.atomic_json(tmp_path / "gates.json", {
        "ok": True, "suites": [{"ok": True}] * 3, "engine_fingerprint": hard_fingerprint,
    })
    with pytest.raises(RuntimeError, match="required versions"):
        validate._measurement_unlocked(settings, hard_mismatch)


def test_configured_port_sets_drive_selection(tmp_path, monkeypatch):
    _write_config(tmp_path)
    settings = common.load_settings(tmp_path)
    assert validate._ports_for(settings, "P20", "design_a") == [("design_a", "PortA"),
                                                                  ("design_a", "PortB")]
    monkeypatch.setattr(validate, "_call_worker", lambda *a, **k: {"ports": ["PortA", "PortB"]})
    assert validate._ports_for(settings, "P92", "design_a") == [("design_a", "PortA"),
                                                                  ("design_a", "PortB")]


def test_owned_sources_do_not_define_default_designs_or_largest_port():
    for name in ("common.py", "ws_validate.py", "engine_worker.py"):
        source = (WORKSTATION / name).read_text(encoding="utf-8")
        assert "DEFAULT_" + "DESIGNS" not in source
        assert "LARGEST_" not in source


class _Status:
    def __init__(self):
        self.values = {}

    def update(self, **values):
        self.values.update(values)


def _baseline_args(tmp_path):
    return SimpleNamespace(root=tmp_path, stop_file=None, stop_now=False, design=None,
                           ports="P9", backend=None, jobs=1, threads=1, profile=None)


def _patch_baseline(monkeypatch, tmp_path, *, stopped=False, comparison=None):
    settings = SimpleNamespace(root=tmp_path, laptop_receipts=None)
    monkeypatch.setattr(validate, "load_settings", lambda root: settings)
    monkeypatch.setattr(validate, "_measurement_prepare", lambda *a, **k: (settings, {}, "fingerprint"))
    monkeypatch.setattr(validate, "_ports_for", lambda *a, **k: [("design_a", "PortA")])
    monkeypatch.setattr(validate, "_case", lambda *a, **k: {"identity_request": {
        "design": "design_a", "port": "PortA",
        "comparison_role": k.get("comparison_role", "cpu_reference")}})
    monkeypatch.setattr(validate, "_run_cases", lambda *a, **k: (0, stopped))
    monkeypatch.setattr(validate, "_gpu_cases_used_cudss", lambda *a, **k: True)
    monkeypatch.setattr(validate, "_compare_case_sets", comparison or
                        (lambda *a, **k: {"pass": True, "failures": []}))


def test_baseline_without_laptop_receipts_succeeds_and_records_not_configured(tmp_path, monkeypatch):
    _patch_baseline(monkeypatch, tmp_path)
    status = _Status()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    with (run_dir / "log").open("w", encoding="utf-8") as log:
        code = validate.command_baseline(_baseline_args(tmp_path), status, run_dir, log)
    assert code == 0
    assert status.values["comparison"] == "not_configured"
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["comparison"] == "not_configured" and summary["exit_code"] == 0
    assert [item["name"] for item in summary["comparisons"]] == [
        "baseline-cpu-vs-gpu", "baseline-gpu-repeat"]


def test_baseline_comparison_failure_is_named_and_fails(tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        return ({"pass": False, "failures": ["frequency_mismatch"]}
                if args[3] == "baseline-cpu-vs-gpu" else {"pass": True, "failures": []})
    _patch_baseline(monkeypatch, tmp_path, comparison=failed)
    args = _baseline_args(tmp_path)
    status = _Status()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    with (run_dir / "log").open("w", encoding="utf-8") as log:
        code = validate.command_baseline(args, status, run_dir, log)
    assert code == 1
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["comparison"] == "failed"
    assert [item["name"] for item in summary["comparison_failures"]] == ["baseline-cpu-vs-gpu"]
    logs = list((run_dir / "comparison_failures").glob("*.json"))
    assert len(logs) == 1 and json.loads(logs[0].read_text(encoding="utf-8"))["name"] == \
        "baseline-cpu-vs-gpu"


def test_baseline_stop_writes_summary_and_returns_130(tmp_path, monkeypatch):
    _patch_baseline(monkeypatch, tmp_path, stopped=True)
    status = _Status()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    with (run_dir / "log").open("w", encoding="utf-8") as log:
        code = validate.command_baseline(_baseline_args(tmp_path), status, run_dir, log)
    assert code == 130
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["comparison"] == "stopped"


def test_stop_is_rechecked_immediately_before_worker_launch(tmp_path, monkeypatch):
    stop_modes = iter((None, "graceful"))
    monkeypatch.setattr(validate, "_stop_mode", lambda *a, **k: next(stop_modes, "graceful"))
    monkeypatch.setattr(validate, "_collect_idle_vram", lambda *a, **k: (None, [], None))
    monkeypatch.setattr(validate, "_popen_group", lambda *a, **k: pytest.fail("worker launched after stop"))
    settings = SimpleNamespace(root=tmp_path, engine_python=Path(sys.executable), engine_root=ROOT)
    case = {"label": "case", "identity": "a" * 64, "request": {},
            "identity_request": {"axis": "B", "profile": "p", "backend": "splu",
                                 "threads": 1, "jobs": 1, "affinity": None}}
    args = SimpleNamespace(stop_file=tmp_path / "stop", stop_now=False)
    with (tmp_path / "log").open("w", encoding="utf-8") as log:
        failures, stopped = validate._run_cases(settings, [case], jobs=1, status=_Status(),
                                                run_dir=tmp_path, log=log, args=args)
    assert failures == 0 and stopped


def test_idle_vram_uses_five_sample_median_and_missing_gpu_is_fast(tmp_path, monkeypatch):
    values = iter((1, 5, 3, 7, 2))
    monkeypatch.setattr(validate, "_sample_vram_once", lambda: next(values))
    monkeypatch.setattr(validate, "_responsive_wait", lambda *a, **k: None)
    baseline, samples, stopped = validate._collect_idle_vram(None, False)
    assert baseline == 3 and samples == [1, 5, 3, 7, 2] and stopped is None

    calls = []
    monkeypatch.setattr(validate, "_sample_vram_once", lambda: calls.append(1) or None)
    baseline, samples, stopped = validate._collect_idle_vram(None, False)
    assert baseline is None and samples == [] and stopped is None and len(calls) == 1

    calls.clear()
    monkeypatch.setattr(validate, "_sample_vram_once", lambda: calls.append(1) or 10)
    monkeypatch.setattr(validate, "_responsive_wait", lambda *a, **k: "graceful")
    baseline, samples, stopped = validate._collect_idle_vram(tmp_path / "stop", False)
    assert baseline is None and samples == [10] and stopped == "graceful" and len(calls) == 1


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
