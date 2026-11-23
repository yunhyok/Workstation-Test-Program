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


def test_op_basis_avoids_invalid_verbose_and_forwards_immediate_cancel(tmp_path, monkeypatch):
    spd = tmp_path / "board.spd"; spd.write_bytes(b"spd")
    reference = tmp_path / "board.npz"; _reference(reference)
    stop_file = tmp_path / "stop.request"; stop_file.write_text("now\n", encoding="utf-8")
    captured = {}

    def decap_basis(freqs, *, chunk=24, backend=None, workers=1, is_cancelled=None, on_step=None):
        captured["freqs"] = list(freqs)
        captured["cancelled"] = is_cancelled() if is_cancelled else False
        return SimpleNamespace(
            device="RTX-Test", build_seconds=1.25, basis_kind="mounted_woodbury",
            Z=lambda cfg: SimpleNamespace(
                receipt=lambda: {"freq": list(freqs), "Z_re": [1.0, 2.0, 3.0],
                                 "Z_im": [0.1, 0.2, 0.3], "numerics_id": "n"}))

    mesh = SimpleNamespace(
        summary={"unknowns": 16},
        model=SimpleNamespace(decap_basis=decap_basis),
        decap_basis=lambda freqs, **kw: decap_basis(freqs, **kw),
    )
    rail = SimpleNamespace(mesh=lambda options, backend: mesh)
    design = SimpleNamespace(rail=lambda port, cache_dir: rail)
    fake_engine = SimpleNamespace(
        Backend=lambda **kw: SimpleNamespace(**kw),
        Design=SimpleNamespace(open=lambda path: design),
        FLAGS_P={},
        ModelOptions=lambda **kw: SimpleNamespace(**kw),
        attach_reference=lambda rec, rf, rz: rec.update(err_1MHz=0.0) or rec,
        ladder_freqs=lambda rf=None: np.asarray([1e5, 1e6, 1e7]),
    )
    monkeypatch.setitem(sys.modules, "spd_pi_engine", fake_engine)
    receipt = worker.op_basis({
        "spd": str(spd), "reference": str(reference), "port": "PortA",
        "cache_dir": str(tmp_path / "cache"), "backend": "cudss", "threads": 2,
        "stop_file": str(stop_file), "stop_now": False,
    })
    assert captured["cancelled"] is True
    assert receipt["err_1MHz"] == 0.0
    assert receipt["worker"]["actual_solver"] == "cudss"
    assert receipt["worker"]["basis_kind"] == "mounted_woodbury"


def test_op_probe_hashes_all_engine_modules(tmp_path, monkeypatch):
    pkg = tmp_path / "spd_pi_engine"
    pkg.mkdir()
    for name in ("__init__", "api", "decaps", "convergence", "hardware", "model", "solver"):
        (pkg / f"{name}.py").write_bytes(f"# {name}\r\nX = 1\r\n".encode("utf-8"))
    hw = SimpleNamespace(
        HardwareProfile=SimpleNamespace(detect=lambda: {"cpu_physical": 4, "cpu_logical": 8}),
        LAPTOP={"cpu_physical": 4}, WORKSTATION={"cpu_physical": 32},
        plan_sweep=lambda profile, n_ports, solver: {"jobs": 1, "solver": solver},
    )
    mod = ModuleType("spd_pi_engine")
    mod.__file__ = str(pkg / "__init__.py")
    mod.__version__ = "0.1.0"
    mod.hardware = hw
    monkeypatch.setitem(sys.modules, "spd_pi_engine", mod)
    monkeypatch.setitem(sys.modules, "spd_pi_engine.hardware", hw)
    monkeypatch.setattr(worker, "_nvidia_smi", lambda: {"gpus": []})
    first = worker.op_probe({})["engine"]["source_hash"]
    (pkg / "decaps.py").write_bytes(b"# decaps\nX = 1\n")
    assert worker.op_probe({})["engine"]["source_hash"] == first, "CRLF vs LF must hash identically"
    (pkg / "decaps.py").write_bytes(b"# decaps\nX = 2\n")
    assert worker.op_probe({})["engine"]["source_hash"] != first
