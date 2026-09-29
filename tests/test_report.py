from __future__ import annotations

import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[1]
WORKSTATION = REPO / "tools" / "engine_studies" / "workstation"
sys.path.insert(0, str(WORKSTATION))

from study_report import compare_directories, make_report  # noqa: E402


def receipt(
    *,
    design="design_a",
    family="package",
    port="PortA",
    backend="splu",
    threads=1,
    jobs=1,
    case_id="case-1",
    numerics="frozen-numerics",
    freq=None,
    z=None,
    axis="A",
    role=None,
):
    freq = [1e5, 1e6] if freq is None else list(freq)
    z = np.asarray([1 + 1j, 2 + 2j] if z is None else z, dtype=np.complex128)
    study = {
        "schema_version": 1, "case_id": case_id, "input_identity": "input-1",
        "design": design, "family": family, "port": port, "backend": backend,
        "threads": threads, "jobs": jobs, "profile": "test", "axis": axis,
        "wall_seconds": 1.0, "peak_rss_MB": 100.0,
        "source_spd_sha256": "a" * 64, "source_ref_sha256": "b" * 64,
    }
    if role:
        study["comparison_role"] = role
    return {
        "receipt_version": 1, "numerics_id": numerics, "design": design, "port": port,
        "backend": {"solver": backend}, "unknowns": 1000,
        "freq": freq, "Z_re": z.real.tolist(), "Z_im": z.imag.tolist(),
        "peak_rss_MB": 100.0, "wall_seconds": 1.0, "study": study,
    }


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class CompareDirectoriesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.left = self.root / "left"; self.right = self.root / "right"
        self.left.mkdir(); self.right.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def compare(self, left, right):
        write_json(self.left / "a.json", left)
        write_json(self.right / "b.json", right)
        return compare_directories(self.left, self.right)

    def test_same_thread_cpu_requires_and_records_bit_equality(self):
        result = self.compare(receipt(axis="B"), receipt(axis="B"))
        self.assertTrue(result["pass"])
        pair = result["pairs"][0]
        self.assertTrue(pair["same_thread_bit_equality_required"])
        self.assertTrue(pair["bit_equal"])

    def test_axis_a_bit_equality_is_recorded_but_e3_is_the_gate(self):
        left = receipt(axis="A", z=[1 + 0j, 1 + 0j])
        right = receipt(axis="A", z=[1 + 5e-10, 1 + 5e-10])
        result = self.compare(left, right)
        self.assertTrue(result["pass"])
        self.assertFalse(result["pairs"][0]["same_thread_bit_equality_required"])
        self.assertFalse(result["pairs"][0]["bit_equal"])

    def test_same_case_id_with_cross_identity_is_rejected(self):
        result = self.compare(receipt(), receipt(design="design_b", case_id="case-1"))
        self.assertFalse(result["pass"])
        self.assertFalse(result["pairs"][0]["identity_match"])
        self.assertIn("design/port identity mismatch", result["pairs"][0]["errors"])

    def test_numerics_and_frequency_identity_are_hard_gates(self):
        mismatch = self.compare(receipt(), receipt(numerics="other"))
        self.assertFalse(mismatch["pass"])
        self.assertFalse(mismatch["pairs"][0]["numerics_id_match"])
        (self.left / "a.json").unlink(); (self.right / "b.json").unlink()
        changed = self.compare(receipt(), receipt(freq=[1e5, 1e6 + 1]))
        self.assertFalse(changed["pass"])
        self.assertFalse(changed["pairs"][0]["frequency_identical"])

    def test_gpu_pcb_uses_full_and_one_mhz_limits(self):
        base = receipt(design="design_pcb", family="pcb", port="PortPCB", threads=1,
                       z=[1 + 0j, 1 + 0j])
        gpu = receipt(design="design_pcb", family="pcb", port="PortPCB", backend="cudss",
                      threads=2, z=[1 + 5e-7, 1 + 5e-8])
        result = self.compare(base, gpu)
        self.assertTrue(result["pass"])
        self.assertEqual([check["limit"] for check in result["pairs"][0]["checks"]], [1e-6, 1e-7])

    def test_unclassified_family_uses_strict_cpu_and_gpu_limits(self):
        cpu = self.compare(receipt(family="unknown", z=[1 + 0j, 1 + 0j]),
                           receipt(family="unknown", z=[1 + 5e-10, 1 + 5e-10]))
        self.assertTrue(cpu["pass"])
        self.assertEqual(cpu["pairs"][0]["checks"][0]["limit"], 1e-9)
        self.assertIn("unclassified (strict)", cpu["pairs"][0]["checks"][0]["name"])
        (self.left / "a.json").unlink(); (self.right / "b.json").unlink()
        gpu = self.compare(receipt(family="unknown", z=[1 + 0j, 1 + 0j]),
                           receipt(family="unknown", backend="cudss", threads=2,
                                   z=[1 + 5e-9, 1 + 5e-9]))
        self.assertTrue(gpu["pass"])
        self.assertEqual(gpu["pairs"][0]["checks"][0]["limit"], 1e-8)
        self.assertIn("unclassified (strict)", gpu["pairs"][0]["checks"][0]["name"])

    def test_e4_basis_has_distinct_gpu_tolerance(self):
        base = receipt(role="basis_cpu", z=[1 + 0j, 1 + 0j])
        gpu = receipt(backend="cudss", role="basis_gpu", z=[1 + 5e-6, 1 + 5e-6])
        result = self.compare(base, gpu)
        self.assertTrue(result["pass"])
        self.assertEqual(result["pairs"][0]["category"], "basis_gpu")
        self.assertEqual(result["pairs"][0]["checks"][0]["limit"], 1e-5)

    def test_requested_gpu_fallback_to_splu_cannot_pass_as_gpu(self):
        base = receipt()
        fallback = receipt(backend="auto", threads=2)
        fallback["study"]["actual_solvers"] = ["splu"]
        result = self.compare(base, fallback)
        self.assertFalse(result["pass"])
        self.assertTrue(any("requested GPU" in error for error in result["pairs"][0]["errors"]))

    def test_malformed_and_unmatched_receipts_cannot_pass(self):
        write_json(self.left / "a.json", receipt())
        write_json(self.right / "b.json", receipt())
        (self.right / "broken.json").write_text("{", encoding="utf-8")
        write_json(self.left / "extra.json", receipt(port="PortB", case_id="extra"))
        result = compare_directories(self.left, self.right)
        self.assertFalse(result["pass"])
        self.assertEqual(result["counts"]["malformed"], 1)
        self.assertEqual(result["counts"]["unmatched_left"], 1)

    def test_explicit_roles_pair_one_reference_to_cpu_gpu_and_repeat(self):
        write_json(self.left / "reference.json", receipt(case_id="laptop", role=None))
        write_json(self.right / "cpu.json", receipt(case_id="cpu", role="cpu_candidate"))
        write_json(self.right / "gpu.json", receipt(case_id="gpu", backend="cudss",
                                                     threads=2, role="gpu_candidate"))
        write_json(self.right / "repeat.json", receipt(case_id="repeat", backend="cudss",
                                                        threads=2, role="gpu_repeat"))
        result = compare_directories(self.left, self.right)
        self.assertTrue(result["pass"])
        self.assertEqual(result["counts"]["paired"], 3)
        self.assertEqual(result["counts"]["unmatched_right"], 0)

    def test_legacy_receipt_needs_content_addressed_verified_map(self):
        old = {
            "tag": "design_a", "port": "PortA", "freq": [1e5, 1e6],
            "Z_re": [1.0, 2.0], "Z_im": [1.0, 2.0],
        }
        old_path = self.left / "old.json"; write_json(old_path, old)
        write_json(self.right / "new.json", receipt(case_id=None))
        rejected = compare_directories(self.left, self.right)
        self.assertFalse(rejected["pass"])
        digest = hashlib.sha256(old_path.read_bytes()).hexdigest()
        mapping = {
            "schema_version": 1, "verified": True,
            "receipts": {"old.json": {"sha256": digest, "numerics_id": "frozen-numerics",
                                          "design": "design_a", "family": "package",
                                          "backend": "splu", "threads": 1, "jobs": 1,
                                          "profile": "test"}},
        }
        write_json(self.left / "legacy_receipt_map.json", mapping)
        accepted = compare_directories(self.left, self.right)
        self.assertTrue(accepted["pass"])

    def test_design_family_must_be_explicit_even_for_matching_designs(self):
        left = receipt()
        right = receipt()
        left["study"].pop("family")
        right["study"].pop("family")
        result = self.compare(left, right)
        self.assertFalse(result["pass"])
        self.assertIn("design family is missing or inconsistent", result["pairs"][0]["errors"])

    def test_output_is_never_overwritten(self):
        write_json(self.left / "a.json", receipt())
        write_json(self.right / "b.json", receipt())
        out = self.root / "comparison.json"
        compare_directories(self.left, self.right, out)
        with self.assertRaises(FileExistsError):
            compare_directories(self.left, self.right, out)

    def test_comparison_records_receipt_campaign_identities(self):
        left = receipt(); right = receipt()
        left["study"].update(gate_fingerprint="campaign-a", input_identity="left-input")
        right["study"].update(gate_fingerprint="campaign-a", input_identity="right-input")
        result = self.compare(left, right)
        self.assertEqual(result["input_identities_by_gate_fingerprint"], {
            "campaign-a": ["left-input", "right-input"]})


class ReportTests(unittest.TestCase):
    def test_folder_report_selects_only_current_campaign_and_records_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipts = root / "receipts"; receipts.mkdir()
            comparisons = root / "comparisons"; comparisons.mkdir()
            groups = root / "group_runs"; groups.mkdir()
            current = "current-folder-fingerprint"
            write_json(root / "env.json", {"ok": True, "probe": {}})
            write_json(root / "gates.json", {
                "ok": True, "engine_fingerprint": current,
                "gate_kind": "standalone_runtime_and_inputs",
                "historical_reproduction": "not_run",
                "standalone": {"ok": True, "gate_kind": "standalone_runtime_and_inputs",
                               "historical_reproduction": "not_run",
                               "accuracy_verification": "baseline_receipts_and_report"},
                "suites": [{"name": "standalone_runtime_and_inputs", "ok": True}],
            })
            current_row = receipt(axis="F", case_id="current")
            current_row["study"].update(gate_fingerprint=current, input_identity="current-input")
            old_row = receipt(axis="B", case_id="old")
            old_row["study"].update(gate_fingerprint="old-folder", input_identity="old-input")
            unknown_row = receipt(axis="B", case_id="unknown")
            unknown_row["study"].pop("gate_fingerprint", None)
            unknown_row["study"]["input_identity"] = "unknown-input"
            write_json(receipts / "current.json", current_row)
            write_json(receipts / "old.json", old_row)
            write_json(receipts / "unknown.json", unknown_row)
            comparison = {"kind": "workstation_receipt_comparison", "pass": True,
                          "gate_fingerprint": current, "pairs": [],
                          "input_identities_by_gate_fingerprint": {current: ["current-input"]}}
            write_json(comparisons / "current.json", comparison)
            write_json(comparisons / "old.json", {"kind": "workstation_receipt_comparison",
                                                    "pass": True, "pairs": []})
            base_group = {"schema_version": 1, "group_id": "group", "axis": "F",
                          "total_wall_seconds": 1.0, "case_count": 1,
                          "executed_case_count": 1, "resumed_case_count": 0,
                          "failed_case_count": 0, "stopped": False}
            write_json(groups / "current.json", {**base_group, "gate_fingerprint": current,
                                                   "case_identities": ["current-input"]})
            write_json(groups / "old.json", {**base_group, "group_id": "old-group",
                                               "gate_fingerprint": "old-folder",
                                               "case_identities": ["old-input"]})
            result = make_report(root)
            self.assertEqual(result["receipt_count"], 1)
            self.assertEqual(result["comparison_count"], 1)
            self.assertEqual(result["group_run_count"], 1)
            self.assertEqual(result["excluded_history_counts"]["receipts"], 2)
            self.assertEqual(result["excluded_history_counts"]["comparisons"], 1)
            self.assertEqual(result["excluded_history_counts"]["group_runs"], 1)
            self.assertEqual(result["campaign_selection"]["gate_fingerprint"], current)
            self.assertEqual(result["outcomes"]["B"]["status"], "NOT_RUN")
            self.assertFalse(result["campaign_identity_errors"])
            text = (root / "W15_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("현재 게이트 fingerprint", text)
            self.assertIn("제외된 이전 이력", text)

    def test_folder_current_campaign_with_unknown_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipts = root / "receipts"; receipts.mkdir()
            (root / "comparisons").mkdir()
            current = "current-folder-fingerprint"
            write_json(root / "env.json", {"ok": True, "probe": {}})
            write_json(root / "gates.json", {
                "ok": True, "engine_fingerprint": current,
                "gate_kind": "standalone_runtime_and_inputs",
                "historical_reproduction": "not_run",
                "standalone": {"ok": True, "gate_kind": "standalone_runtime_and_inputs",
                               "historical_reproduction": "not_run"},
                "suites": [{"name": "standalone_runtime_and_inputs", "ok": True}],
            })
            row = receipt(axis="F")
            row["study"].update(gate_fingerprint=current, input_identity="current-input")
            write_json(receipts / "current.json", row)
            write_json(root / "comparisons" / "bad.json", {
                "kind": "workstation_receipt_comparison", "pass": True,
                "gate_fingerprint": current, "pairs": [],
                "input_identities_by_gate_fingerprint": {current: ["not-a-current-receipt"]},
            })
            result = make_report(root)
            self.assertEqual(result["overall_status"], "FAIL")
            self.assertTrue(any("comparison" in error for error in result["campaign_identity_errors"]))

    def test_folder_config_changed_after_gates_excludes_stale_campaign_and_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipts = root / "receipts"; receipts.mkdir()
            current = "stale-gate-fingerprint"
            write_json(root / "config.json", {"configuration_mode": "folder", "data_dir": "new"})
            write_json(root / "env.json", {"ok": True, "probe": {}})
            write_json(root / "gates.json", {
                "ok": True, "engine_fingerprint": current,
                "engine_evidence": {"config_sha256": "0" * 64},
                "gate_kind": "standalone_runtime_and_inputs",
                "historical_reproduction": "not_run",
                "standalone": {"ok": True, "gate_kind": "standalone_runtime_and_inputs",
                               "historical_reproduction": "not_run"},
                "suites": [{"name": "standalone_runtime_and_inputs", "ok": True}],
            })
            row = receipt(axis="B")
            row["study"].update(gate_fingerprint=current, input_identity="stale-input")
            write_json(receipts / "stale.json", row)
            result = make_report(root)
            self.assertEqual(result["overall_status"], "FAIL")
            self.assertEqual(result["receipt_count"], 0)
            self.assertEqual(result["excluded_history_counts"]["receipts"], 1)
            self.assertTrue(any("config" in error for error in result["campaign_identity_errors"]))

    def test_standalone_gate_is_readiness_pass_without_historical_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "env.json", {"ok": True, "probe": {}})
            write_json(root / "gates.json", {
                "ok": True,
                "engine_fingerprint": "standalone-current",
                "gate_kind": "standalone_runtime_and_inputs",
                "historical_reproduction": "not_run",
                "standalone": {
                    "ok": True,
                    "gate_kind": "standalone_runtime_and_inputs",
                    "historical_reproduction": "not_run",
                    "accuracy_verification": "baseline_receipts_and_report",
                },
                "suites": [{"name": "standalone_runtime_and_inputs", "ok": True}],
            })
            result = make_report(root)
            self.assertEqual(result["gates"]["status"], "PASS")
            self.assertEqual(result["gates"]["gate_kind"], "standalone_runtime_and_inputs")
            self.assertEqual(result["gates"]["historical_reproduction"], "not_run")
            self.assertEqual(result["study_mode"], "standalone_folder")
            self.assertEqual(result["overall_status"], "INCOMPLETE")
            report_text = (root / "W15_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("독립 실행 엔진 및 입력 검증 보고서", report_text)
            self.assertIn("과거 비공개 데이터 재현은 실행하지 않았다", report_text)
            self.assertIn("baseline 영수증과 보고서 비교", report_text)
            malformed = json.loads((root / "gates.json").read_text(encoding="utf-8"))
            malformed["historical_reproduction"] = "passed"
            write_json(root / "gates.json", malformed)
            self.assertEqual(make_report(root)["gates"]["status"], "FAIL")

    def test_cpu_only_e_plan_is_not_run_and_overall_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "env.json", {"ok": True, "probe": {}})
            write_json(root / "gates.json", {
                "ok": True,
                "suites": [{"name": name, "ok": True} for name in ("default", "gpu", "slow")],
            })
            write_json(root / "matrix_plan.json", {
                "axes": {"E": {"status": "not_run", "reason": "No NVIDIA GPU detected"}},
            })
            result = make_report(root)
            self.assertEqual(result["gates"]["status"], "PASS")
            self.assertEqual(result["outcomes"]["E"]["status"], "NOT_RUN")
            self.assertIn("No NVIDIA GPU detected", result["outcomes"]["E"]["source"])
            self.assertEqual(result["overall_status"], "INCOMPLETE")

    def test_report_documents_affinity_and_vram_estimate_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_report(root)
            report_text = (root / "W15_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("가장 낮은 번호의 논리 CPU", report_text)
            self.assertIn("NUMA", report_text)
            self.assertIn("유휴 표본 5개의 중앙값", report_text)
            self.assertIn("개별 프로세스 귀속에는 한계", report_text)

    def test_f_convergence_cases_include_metrics_and_cost_without_new_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipts = root / "receipts"; receipts.mkdir()
            row = receipt(axis="F", case_id="fine")
            row["convergence"] = {
                "converged": False, "frequency_rms_delta_db": 0.31,
                "frequency_max_delta_db": 0.72, "mesh_pair": "fine",
                "h_ref": 200.0, "h_var": 100.0,
            }
            row["convergence_cost"] = {
                "ref": {"unknowns": 1000, "wall_seconds": 2.5},
                "var": {"unknowns": 2000, "wall_seconds": 5.5},
            }
            write_json(receipts / "fine.json", row)
            result = make_report(root)
            self.assertEqual(result["outcomes"]["F"]["status"], "OBSERVED")
            self.assertEqual(result["convergence_cases"][0]["cost"]["total_wall_seconds"], 8.0)
            report_text = (root / "W15_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("PortA", report_text)
            self.assertIn("0.31", report_text)

    def test_gate_fingerprint_mismatch_forces_overall_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipts = root / "receipts"; receipts.mkdir()
            row = receipt(axis="F")
            row["study"]["gate_fingerprint"] = "old-engine"
            write_json(receipts / "stale.json", row)
            write_json(root / "gates.json", {
                "ok": True, "engine_fingerprint": "current-engine",
                "suites": [{"ok": True}, {"ok": True}, {"ok": True}],
            })
            result = make_report(root)
            self.assertEqual(result["overall_status"], "FAIL")
            self.assertTrue(result["campaign_identity_errors"])

    def test_axis_a_uses_only_complete_baseline_scope_and_does_not_fill_e(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); left = root / "left"; right = root / "right"
            left.mkdir(); right.mkdir(); (root / "comparisons").mkdir()
            write_json(left / "reference.json", receipt(axis="A", case_id="laptop"))
            write_json(right / "cpu.json", receipt(axis="A", case_id="cpu", role="cpu_reference"))
            write_json(right / "gpu.json", receipt(axis="A", case_id="gpu", backend="cudss",
                                                     threads=2, role="gpu_candidate"))
            write_json(right / "repeat.json", receipt(axis="A", case_id="repeat", backend="cudss",
                                                        threads=2, role="gpu_repeat"))
            compare_directories(left, right, root / "comparisons" / "baseline.json")
            result = make_report(root)
            self.assertEqual(result["outcomes"]["A"]["status"], "PASS")
            self.assertEqual(result["outcomes"]["E"]["status"], "NOT_RUN")

    def test_absent_evidence_is_incomplete_and_previous_report_is_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_report(root)
            self.assertEqual(first["overall_status"], "INCOMPLETE")
            self.assertEqual(first["outcomes"]["A"]["status"], "NOT_RUN")
            second = make_report(root)
            self.assertTrue(second.get("archived_previous"))
            self.assertTrue((root / "summary.json").is_file())
            self.assertTrue((root / "W15_REPORT.md").is_file())

    def test_failed_comparison_document_cannot_yield_axis_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "receipts").mkdir(); (root / "comparisons").mkdir()
            comparison = {
                "kind": "workstation_receipt_comparison", "pass": False,
                "pairs": [{"category": "cpu", "pass": True, "axis_right": "A",
                           "role_right": "cpu_reference", "identity": {"design": "d", "port": "p"}}],
            }
            write_json(root / "comparisons" / "comparison.json", comparison)
            result = make_report(root)
            self.assertEqual(result["outcomes"]["A"]["status"], "FAIL")
            self.assertEqual(result["overall_status"], "FAIL")

    def test_malformed_receipt_forces_overall_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "receipts").mkdir()
            (root / "receipts" / "broken.json").write_text("{", encoding="utf-8")
            result = make_report(root)
            self.assertEqual(result["overall_status"], "FAIL")

    def test_axis_b_pass_requires_complete_plan_and_bit_equal_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipts = root / "receipts"; receipts.mkdir()
            write_json(root / "matrix_plan.json", {"axes": {"B": {"combinations": [[1, 1], [1, 2]],
                                                                            "expected_ports": [{"design": "design_a", "port": "PortA"}]}}})
            write_json(receipts / "j1.json", receipt(axis="B", jobs=1, case_id="j1"))
            write_json(receipts / "j2.json", receipt(axis="B", jobs=2, case_id="j2"))
            result = make_report(root)
            self.assertEqual(result["outcomes"]["B"]["status"], "PASS")
            (receipts / "j2.json").unlink()
            result = make_report(root)
            self.assertEqual(result["outcomes"]["B"]["status"], "INCOMPLETE")

    def test_axis_d_uses_overlapping_peak_rss_sum_and_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipts = root / "receipts"; receipts.mkdir()
            write_json(root / "matrix_plan.json", {"axes": {"D": {
                "plans": {str(v): {} for v in (64, 128, 256, 512)},
                "expected_ports": [{"design": "design_a", "port": "PortA"}]}}})
            for value in (64, 128, 256, 512):
                row = receipt(axis="D", case_id=f"ram{value}")
                row["study"].update(profile=f"ram{value}", ram_budget_MB=value * .9 * 1024,
                                    started_at="2026-09-21T00:00:00+00:00",
                                    finished_at="2026-09-21T00:01:00+00:00")
                write_json(receipts / f"ram{value}.json", row)
            result = make_report(root)
            self.assertEqual(result["outcomes"]["D"]["status"], "PASS")

    def test_axis_e_requires_all_jobs_cpu_reference_and_basis_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipts = root / "receipts"; receipts.mkdir()
            write_json(root / "matrix_plan.json", {"axes": {"E": {"synthetic_vram_plans": {},
                                                                         "expected_ports": [{"design": "design_a", "port": "PortA"}]}}})
            cpu = receipt(axis="A", role="cpu_reference", case_id="cpu")
            write_json(receipts / "cpu.json", cpu)
            for jobs in (1, 2, 4, 8):
                gpu = receipt(axis="E", backend="cudss", threads=2, jobs=jobs,
                              role="gpu_candidate", case_id=f"gpu{jobs}")
                write_json(receipts / f"gpu{jobs}.json", gpu)
            write_json(receipts / "basis_cpu.json", receipt(axis="E", role="basis_cpu", case_id="bc"))
            write_json(receipts / "basis_gpu.json", receipt(axis="E", backend="cudss", threads=2,
                                                              role="basis_gpu", case_id="bg"))
            result = make_report(root)
            self.assertEqual(result["outcomes"]["E"]["status"], "PASS")

    def test_axis_e_unclassified_family_uses_strict_gpu_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); receipts = root / "receipts"; receipts.mkdir()
            write_json(root / "matrix_plan.json", {"axes": {"E": {
                "expected_ports": [{"design": "design_a", "port": "PortA"}]}}})
            write_json(receipts / "cpu.json", receipt(axis="A", family="unknown",
                                                       role="cpu_reference", case_id="cpu",
                                                       z=[1 + 0j, 1 + 0j]))
            for jobs in (1, 2, 4, 8):
                write_json(receipts / f"gpu{jobs}.json",
                           receipt(axis="E", family="unknown", backend="cudss", threads=2,
                                   jobs=jobs, role="gpu_candidate", case_id=f"gpu{jobs}",
                                   z=[1 + 5e-8, 1 + 5e-8]))
            write_json(receipts / "basis_cpu.json",
                       receipt(axis="E", family="unknown", role="basis_cpu", case_id="bc"))
            write_json(receipts / "basis_gpu.json",
                       receipt(axis="E", family="unknown", backend="cudss", threads=2,
                               role="basis_gpu", case_id="bg"))
            result = make_report(root)
            self.assertEqual(result["outcomes"]["E"]["status"], "FAIL")
            self.assertTrue(any("strict unclassified" in failure
                                for failure in result["outcomes"]["E"]["failures"]))


class ConverterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import spd_decap_pi  # noqa: F401
        except ImportError:
            sibling = REPO.parent / "SPD Decap PI Evaluator" / "src"
            if sibling.is_dir():
                sys.path.insert(0, str(sibling))

    def test_synthetic_two_port_and_no_overwrite(self):
        try:
            module = importlib.import_module("touchstone_to_zdiag")
        except ImportError as exc:
            self.skipTest(f"product touchstone API is unavailable: {exc}")
        result = module.self_check()
        self.assertEqual(result["self_check"], "PASS")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "x.s2p"; output = root / "x.npz"
            source.write_text(
                "! Port[1] = 2nd_SITE0-A/0\n! Port[2] = 2nd_SITE1-B/1\n"
                "# Hz S RI R 50\n1e6 .1 0 0 0 0 0 .2 0\n", encoding="utf-8")
            module.convert_touchstone(source, output)
            with self.assertRaises(FileExistsError):
                module.convert_touchstone(source, output)

    def test_port_manifest_cli_preserves_exact_short_port_and_rail_names(self):
        try:
            module = importlib.import_module("touchstone_to_zdiag")
        except ImportError as exc:
            self.skipTest(f"product touchstone API is unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.s2p"
            output = root / "output.npz"
            manifest = root / "ports.txt"
            source.write_text(
                "! Port[1] = 2nd_SITE0-VDD_A/0\n! Port[2] = 2nd_SITE1-VDD_B/1\n"
                "# Hz S RI R 50\n1e6 .1 0 0 0 0 0 .2 0\n", encoding="utf-8",
            )
            manifest.write_text("PortA::VDD_A/0\nPortB::VDD_B/1\n", encoding="utf-8")
            code = module.main([str(source), str(output), "--port-manifest", str(manifest)])
            self.assertEqual(code, 0)
            with np.load(output, allow_pickle=False) as converted:
                self.assertEqual(converted["port_names"].tolist(),
                                 ["PortA::VDD_A/0", "PortB::VDD_B/1"])

    def test_legacy_bare_rail_header_requires_exact_external_evidence(self):
        try:
            module = importlib.import_module("touchstone_to_zdiag")
        except ImportError as exc:
            self.skipTest(f"product touchstone API is unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "pcb.s1p"; output = root / "pcb.npz"
            expected = root / "expected.npz"
            source.write_text("! Port[1] = VDD/0\n# Hz S RI R 50\n1e6 0 0\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                module.convert_touchstone(source, output)
            np.savez(expected, freq=np.asarray([1e6]),
                     port_names=np.asarray(["PortA::VDD/0"]),
                     Zdiag=np.asarray([[50 + 0j]]))
            result = module.convert_touchstone(source, output, against=expected)
            self.assertTrue(result["verified"])


if __name__ == "__main__":
    unittest.main()
