from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
from scipy import sparse


ROOT = Path(__file__).resolve().parents[1]
WORKSTATION = ROOT / "tools" / "engine_studies" / "workstation"
sys.path.insert(0, str(WORKSTATION))
import engine_worker as worker


def _reference(path: Path, *, names=("PortA::rail-a", "PortB::rail-b")) -> None:
    np.savez(path, freq=np.asarray([1e5, 1e6, 1e7]), port_names=np.asarray(names),
             Zdiag=np.ones((3, len(names)), dtype=np.complex128))


def test_standalone_design_check_validates_spd_ports_and_reference(tmp_path, monkeypatch):
    spd = tmp_path / "board.spd"; spd.write_bytes(b"spd")
    reference = tmp_path / "board.npz"; _reference(reference)

    class Design:
        @staticmethod
        def open(path):
            assert Path(path) == spd
            return SimpleNamespace(ports=lambda: ["PortA", "PortB"])

    monkeypatch.setitem(sys.modules, "spd_pi_engine", SimpleNamespace(Design=Design))
    result = worker._standalone_design_check({
        "design_id": "design_a", "spd": str(spd), "reference": str(reference),
        "ports": ["PortA", "PortB"],
    })
    assert result["status"] == "passed"
    assert result["spd_port_count"] == 2 and result["frequency_count"] == 3
    assert len(result["spd_sha256"]) == len(result["reference_sha256"]) == 64


def test_standalone_design_check_rejects_unmatched_and_malformed_reference(tmp_path, monkeypatch):
    spd = tmp_path / "board.spd"; spd.write_bytes(b"spd")

    class Design:
        @staticmethod
        def open(path):
            return SimpleNamespace(ports=lambda: ["PortA"])

    monkeypatch.setitem(sys.modules, "spd_pi_engine", SimpleNamespace(Design=Design))
    reference = tmp_path / "bad.npz"
    np.savez(reference, freq=np.asarray([1e6, 1e5]),
             port_names=np.asarray(["PortA::one", "PortA::two"]),
             Zdiag=np.ones((2, 2), dtype=np.complex128))
    result = worker._standalone_design_check({
        "design_id": "design_a", "spd": str(spd), "reference": str(reference),
        "ports": ["PortA"],
    })
    assert result["status"] == "failed"
    assert "frequency" in result["error"] or "duplicate" in result["error"]


def test_standalone_gate_labels_scope_and_requires_all_checks(monkeypatch):
    monkeypatch.setattr(worker, "_standalone_datafree_checks", lambda: {
        "engine_demos": {"status": "passed"}, "synthetic_cpu": {"status": "passed"}})
    monkeypatch.setattr(worker, "_standalone_design_check", lambda design: {
        "status": "passed", "design_id": design["design_id"]})
    monkeypatch.setattr(worker, "_standalone_gpu_check", lambda: {
        "status": "passed", "comparison": "cudss_vs_scipy_splu"})
    result = worker.op_standalone_gate({
        "designs": [{"design_id": "design_a", "spd": "a", "reference": "b",
                     "ports": ["PortA"]}], "gpu": True,
    })
    assert result["ok"] is True
    assert result["gate_kind"] == "standalone_runtime_and_inputs"
    assert result["historical_reproduction"] == "not_run"
    assert result["accuracy_verification"] == "baseline_receipts_and_report"
    assert result["gpu"]["status"] == "passed"

    monkeypatch.setattr(worker, "_standalone_design_check", lambda design: {
        "status": "failed", "design_id": design["design_id"], "error": "bad reference"})
    failed = worker.op_standalone_gate({
        "designs": [{"design_id": "design_a", "spd": "a", "reference": "b",
                     "ports": ["PortA"]}], "gpu": False,
    })
    assert failed["ok"] is False
    assert failed["gpu"]["status"] == "not_requested"


def test_standalone_gate_rejects_empty_designs(monkeypatch):
    monkeypatch.setattr(worker, "_standalone_datafree_checks", lambda: {
        "engine_demos": {"status": "passed"}, "synthetic_cpu": {"status": "passed"}})
    result = worker.op_standalone_gate({"designs": [], "gpu": False})
    assert result["ok"] is False
    assert result["designs"][0]["status"] == "failed"


def test_prepare_operation_calls_data_setup_with_paths(tmp_path, monkeypatch):
    module = ModuleType("data_setup")
    calls = []

    def prepare_data(*, data_dir, root):
        calls.append((data_dir, root))
        return {"prepared": True}

    module.prepare_data = prepare_data
    monkeypatch.setitem(sys.modules, "data_setup", module)
    result = worker.op_prepare({"data_dir": str(tmp_path / "data"), "root": str(tmp_path)})
    assert result == {"prepared": True}
    assert calls == [(tmp_path / "data", tmp_path)]
    assert worker.OPS["prepare"] is worker.op_prepare
    assert worker.OPS["standalone_gate"] is worker.op_standalone_gate


def test_worker_script_directory_is_explicitly_importable():
    assert str(Path(worker.__file__).resolve().parent) in sys.path


def test_synthetic_cpu_check_performs_sparse_solve(monkeypatch):
    solver = ModuleType("spd_pi_engine.solver")

    class YPattern:
        def __init__(self, rows, cols, n):
            self.rows, self.cols, self.n = rows, cols, n

        def csc(self, values):
            return sparse.coo_matrix((values, (self.rows, self.cols)),
                                     shape=(self.n, self.n)).tocsc()

    solver.YPattern = YPattern
    package = ModuleType("spd_pi_engine")
    package.__path__ = []
    package.solver = solver
    monkeypatch.setitem(sys.modules, "spd_pi_engine", package)
    monkeypatch.setitem(sys.modules, "spd_pi_engine.solver", solver)
    result = worker._synthetic_cpu_check()
    assert result["solver"] == "scipy.sparse.linalg.splu"
    assert result["max_abs_residual"] <= 1e-11


def test_prepare_main_uses_configured_engine_root(tmp_path, monkeypatch):
    module = ModuleType("data_setup")
    module.prepare_data = lambda *, data_dir, root: {"data_dir": str(data_dir), "root": str(root)}
    monkeypatch.setitem(sys.modules, "data_setup", module)
    engine_root = tmp_path / "engine-runtime"
    engine_module = engine_root / "Lib" / "site-packages" / "spd_pi_engine" / "__init__.py"
    engine_module.parent.mkdir(parents=True)
    engine_module.write_text("", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "spd_pi_engine",
                        SimpleNamespace(__file__=str(engine_module)))
    request = tmp_path / "request.json"
    output = tmp_path / "output.json"
    request.write_text(json.dumps({"operation": "prepare", "data_dir": str(tmp_path / "data"),
                                   "root": str(tmp_path), "engine_root": str(engine_root)}),
                       encoding="utf-8")
    assert worker.main([str(request), str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is True and payload["result"]["root"] == str(tmp_path)
