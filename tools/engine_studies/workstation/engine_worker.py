"""External-Python worker for every spd_pi_engine import and numerical operation.

The orchestrator is intentionally standard-library-only.  This file is shipped beside it and is
started with ``config.json:engine_python``, where the engine is installed.
"""
from __future__ import annotations

import dataclasses
import json
import os
import platform
import re
import subprocess
import sys
import time
import hashlib
from importlib import metadata
from pathlib import Path

_WORKER_DIR = str(Path(__file__).resolve().parent)
if _WORKER_DIR not in sys.path:
    sys.path.insert(0, _WORKER_DIR)


def _jsonable(value):
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def _set_affinity(count: int | None) -> None:
    if not count:
        return
    count = int(count)
    available = os.cpu_count() or 1
    if count > available:
        raise ValueError(f"requested affinity {count} exceeds available logical CPUs {available}")
    if sys.platform == "win32":
        import ctypes
        if count > 64:
            raise ValueError("Windows affinity validation supports one processor group (<=64 CPUs)")
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessAffinityMask.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        kernel32.SetProcessAffinityMask.restype = ctypes.c_int
        if not kernel32.SetProcessAffinityMask(kernel32.GetCurrentProcess(),
                                                ctypes.c_size_t((1 << count) - 1)):
            raise ctypes.WinError()
    elif hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, set(range(count)))


def _effective_affinity_count() -> int:
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetProcessAffinityMask.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                                    ctypes.POINTER(ctypes.c_size_t))
        process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
        if not kernel32.GetProcessAffinityMask(kernel32.GetCurrentProcess(),
                                                ctypes.byref(process_mask), ctypes.byref(system_mask)):
            raise ctypes.WinError()
        return int(process_mask.value).bit_count()
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def _packages() -> dict:
    names = ("numpy", "scipy", "matplotlib", "nvmath-python", "nvidia-cudss-cu12",
             "cuda-bindings", "cupy-cuda12x", "shapely", "pydantic", "spd-decap-pi-evaluator")
    out = {}
    for name in names:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    all_packages = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            all_packages[re.sub(r"[-_.]+", "-", name).lower()] = distribution.version
    return out, all_packages


def _nvidia_smi() -> dict:
    command = ["nvidia-smi", "--query-gpu=index,name,driver_version,memory.total,memory.free",
               "--format=csv,noheader,nounits"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
        rows = []
        for line in proc.stdout.splitlines():
            fields = [x.strip() for x in line.split(",")]
            if len(fields) == 5:
                rows.append(dict(index=int(fields[0]), name=fields[1], driver=fields[2],
                                 vram_total_MB=float(fields[3]), vram_free_MB=float(fields[4])))
        return {"command": command, "returncode": proc.returncode, "gpus": rows,
                "stderr": proc.stderr.strip()}
    except Exception as exc:
        return {"command": command, "error": f"{type(exc).__name__}: {exc}", "gpus": []}


def _profile_from_dict(H, value: dict):
    return H.HardwareProfile(int(value["cpu_physical"]), int(value["cpu_logical"]),
                             float(value["ram_total_GB"]), float(value["ram_free_GB"]),
                             tuple(value.get("gpus", ())))


def op_probe(request: dict) -> dict:
    import spd_pi_engine
    from spd_pi_engine import hardware as H
    detected = H.HardwareProfile.detect()
    profiles = {"detected": detected, "laptop": H.LAPTOP, "workstation": H.WORKSTATION}
    plans = {}
    for label, profile in profiles.items():
        plans[label] = {
            solver: H.plan_sweep(profile, n_ports=92, solver=solver)
            for solver in ("splu", "auto", "cudss")
        }
    source_files = [Path(spd_pi_engine.__file__).with_name(name + ".py") for name in
                    ("geometry", "homogenise", "reference", "model", "solver", "receipt",
                     "hardware", "api")]
    digest = hashlib.sha256()
    for path in source_files:
        digest.update(path.name.encode()); digest.update(path.read_bytes())
    core_packages, all_packages = _packages()
    return {
        "ok": True,
        "python": {"version": platform.python_version(), "executable": sys.executable,
                   "implementation": platform.python_implementation()},
        "platform": platform.platform(),
        "packages": core_packages,
        "all_packages": all_packages,
        "engine": {"module": str(Path(spd_pi_engine.__file__).resolve()),
                   "version": getattr(spd_pi_engine, "__version__", None),
                   "source_hash": digest.hexdigest()},
        "nvidia_smi": _nvidia_smi(),
        "profiles": profiles,
        "plans": plans,
    }


def op_ports(request: dict) -> dict:
    from spd_pi_engine import Design
    design = Design.open(request["spd"])
    return {"ports": design.ports(), "spd_sha256": design.sha256}


def _reference(npz_path: str | None, port: str):
    if not npz_path:
        return None
    import numpy as np
    with np.load(npz_path, allow_pickle=True) as data:
        names = [str(x) for x in data["port_names"]]
        matches = [index for index, name in enumerate(names) if name.split("::", 1)[0] == port]
        if len(matches) != 1:
            raise KeyError(f"port {port!r} matched {len(matches)} columns in {npz_path}")
        freq = np.asarray(data["freq"], float)
        zdiag = np.asarray(data["Zdiag"], complex)
    if freq.ndim != 1 or not len(freq) or not np.all(np.isfinite(freq)) or np.any(np.diff(freq) <= 0):
        raise ValueError(f"invalid frequency grid in {npz_path}")
    if zdiag.ndim != 2 or zdiag.shape != (len(freq), len(names)) or not np.all(np.isfinite(zdiag)):
        raise ValueError(f"invalid Zdiag shape/values in {npz_path}: {zdiag.shape}")
    return freq, zdiag[:, matches[0]]


def op_solve(request: dict) -> dict:
    import numpy as np
    from spd_pi_engine import Backend, Design, FLAGS_P, ModelOptions, attach_reference, ladder_freqs

    t0 = time.perf_counter()
    reference = _reference(request.get("reference"), request["port"])
    freqs = ladder_freqs(reference[0] if reference else None)
    design = Design.open(request["spd"])
    rail = design.rail(request["port"], cache_dir=request["cache_dir"])
    options = ModelOptions(reference="powersi-compatible", flags=FLAGS_P, h=200.0, fh=50.0,
                           top_h=50.0, sub=(20, 10, 10), fringe=True)
    backend = Backend(solver=request["backend"], fast=True,
                      host_nthreads=max(1, int(request.get("threads", 1))))
    mesh = rail.mesh(options, backend)
    result = mesh.solve(np.asarray(freqs, float), verbose=False)
    receipt = result.receipt()
    if reference:
        attach_reference(receipt, reference[0], reference[1])
    receipt["worker"] = {"wall_seconds": time.perf_counter() - t0,
                         "mesh_summary": mesh.summary,
                         "affinity_requested": request.get("affinity"),
                         "affinity_effective_logical": _effective_affinity_count()}
    return receipt


def op_converge(request: dict) -> dict:
    import numpy as np
    from spd_pi_engine import (Backend, Design, FLAGS_P, ModelOptions, check_mesh_convergence,
                               ladder_freqs)
    reference = _reference(request.get("reference"), request["port"])
    freqs = ladder_freqs(reference[0] if reference else None)
    rail = Design.open(request["spd"]).rail(request["port"], cache_dir=request["cache_dir"])
    options = ModelOptions(reference="powersi-compatible", flags=FLAGS_P, h=200.0, fh=50.0,
                           top_h=50.0, sub=(20, 10, 10), fringe=True)
    backend = Backend(solver=request["backend"], fast=True,
                      host_nthreads=max(1, int(request.get("threads", 1))))
    result = check_mesh_convergence(rail, options, np.asarray(freqs, float), pair="fine",
                                    backend=backend)
    receipt = result.ref.receipt()
    if reference:
        from spd_pi_engine import attach_reference
        attach_reference(receipt, reference[0], reference[1])
    receipt["convergence"] = result.product_dict()
    receipt["convergence_cost"] = _jsonable(result.cost)
    receipt["worker"] = {"kind": "mesh_convergence", "pair": "fine"}
    return receipt


def op_plan(request: dict) -> dict:
    from spd_pi_engine import hardware as H
    profile = (_profile_from_dict(H, request["profile"]) if request.get("profile")
               else H.HardwareProfile.detect())
    sweep = H.plan_sweep(profile, n_ports=int(request.get("n_ports", 92)),
                         unknowns_estimate=int(request.get("unknowns", H.DEFAULT_UNKNOWNS)),
                         solver=request.get("backend", "splu"), max_jobs=request.get("max_jobs"))
    effective = _effective_affinity_count()
    return {"profile": profile, "sweep": sweep,
            "affinity_requested": request.get("affinity"),
            "affinity_effective_logical": effective,
            "detect_reflects_affinity": (not request.get("affinity") or profile.cpu_logical <= effective),
            "chunk_tiles": H.plan_chunk_tiles(profile),
            "basis": H.plan_basis(profile, int(request.get("unknowns", H.DEFAULT_UNKNOWNS)),
                                  int(request.get("n_decaps", 421)),
                                  int(request.get("n_freqs", 27)), request.get("backend", "splu")),
            "rss_per_process_MB": H.rss_per_process_MB(int(request.get("unknowns", H.DEFAULT_UNKNOWNS))),
            "vram_per_process_MB": H.vram_per_process_MB(
                int(request.get("unknowns", H.DEFAULT_UNKNOWNS)), H.plan_chunk_tiles(profile))}


def op_basis(request: dict) -> dict:
    import numpy as np
    from spd_pi_engine import Backend, Design, FLAGS_P, ModelOptions, ladder_freqs
    reference = _reference(request.get("reference"), request["port"])
    freqs = ladder_freqs(reference[0] if reference else None)
    rail = Design.open(request["spd"]).rail(request["port"], cache_dir=request["cache_dir"])
    options = ModelOptions(reference="powersi-compatible", flags=FLAGS_P, h=200.0, fh=50.0,
                           top_h=50.0, sub=(20, 10, 10), fringe=True)
    backend = Backend(solver=request["backend"], fast=True,
                      host_nthreads=max(1, int(request.get("threads", 1))))
    mesh = rail.mesh(options, backend)
    started = time.perf_counter()
    basis = mesh.decap_basis(np.asarray(freqs, float), verbose=False)
    receipt = basis.Z({}).receipt()
    receipt["worker"] = {"wall_seconds": time.perf_counter() - started,
                         "mesh_summary": mesh.summary, "kind": "decap_basis",
                         "actual_solver": "cudss" if getattr(basis, "device", None) else "splu"}
    return receipt


def op_reference_check(request: dict) -> dict:
    checked = {}
    for port in request["ports"]:
        freq, impedance = _reference(request["reference"], str(port))
        checked[str(port)] = {"frequencies": len(freq), "finite": bool(len(impedance) == len(freq))}
    return {"reference": request["reference"], "ports": checked}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _standalone_design_check(design: dict) -> dict:
    """Validate one user-supplied design and its reference without judging accuracy."""
    design_id = str(design.get("design_id", "")).strip() if isinstance(design, dict) else ""
    result = {"design_id": design_id or None, "status": "failed"}
    try:
        import numpy as np
        from spd_pi_engine import Design

        if not isinstance(design, dict):
            raise ValueError("each design must be an object")
        if not design_id:
            raise ValueError("design_id is required")
        spd = Path(design["spd"]).resolve()
        reference = Path(design["reference"]).resolve()
        if not spd.is_file():
            raise FileNotFoundError(f"SPD file not found: {spd}")
        if not reference.is_file():
            raise FileNotFoundError(f"reference file not found: {reference}")

        selected = design.get("ports")
        if not isinstance(selected, list) or not selected:
            raise ValueError("ports must be a non-empty list")
        selected = [str(port) for port in selected]
        if len(set(selected)) != len(selected):
            raise ValueError("selected ports contain duplicates")

        available = [str(port) for port in Design.open(spd).ports()]
        if not available or len(set(available)) != len(available):
            raise ValueError("SPD ports must be non-empty and unique")
        missing_spd = [port for port in selected if port not in available]
        if missing_spd:
            raise ValueError(f"selected ports absent from SPD: {missing_spd}")

        with np.load(reference, allow_pickle=False) as data:
            missing_arrays = [name for name in ("freq", "port_names", "Zdiag") if name not in data]
            if missing_arrays:
                raise ValueError(f"reference is missing arrays: {missing_arrays}")
            freq = np.asarray(data["freq"], dtype=float)
            raw_names = np.asarray(data["port_names"])
            zdiag = np.asarray(data["Zdiag"], dtype=complex)
        if freq.ndim != 1 or not len(freq) or not np.all(np.isfinite(freq)):
            raise ValueError("reference frequency grid must be a non-empty finite vector")
        if np.any(np.diff(freq) <= 0):
            raise ValueError("reference frequency grid must be strictly increasing")
        if raw_names.ndim != 1:
            raise ValueError("reference port_names must be a vector")
        names = [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in raw_names]
        short_names = [name.split("::", 1)[0] for name in names]
        duplicates = sorted({name for name in short_names if short_names.count(name) > 1})
        if duplicates:
            raise ValueError(f"reference contains duplicate short port names: {duplicates}")
        if zdiag.ndim != 2 or zdiag.shape != (len(freq), len(names)):
            raise ValueError(f"reference Zdiag has invalid shape {zdiag.shape}")
        if not np.all(np.isfinite(zdiag)):
            raise ValueError("reference Zdiag contains non-finite values")
        missing_reference = [port for port in selected if port not in short_names]
        if missing_reference:
            raise ValueError(f"selected ports absent from reference: {missing_reference}")

        result.update({
            "status": "passed",
            "spd": str(spd),
            "reference": str(reference),
            "spd_sha256": _sha256_file(spd),
            "reference_sha256": _sha256_file(reference),
            "spd_port_count": len(available),
            "reference_port_count": len(names),
            "selected_port_count": len(selected),
            "frequency_count": len(freq),
        })
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _captured_check(function, *, require_zero: bool = False) -> dict:
    import contextlib
    import io

    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            value = function()
        if require_zero and value != 0:
            raise RuntimeError(f"self-check returned {value!r}, expected 0")
        result = {"status": "passed", "output": output.getvalue()[-4000:]}
        if value is not None:
            result["details"] = _jsonable(value)
        return result
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                "output": output.getvalue()[-4000:]}


def _synthetic_cpu_check() -> dict:
    """Exercise the engine's sparse pattern and SciPy CPU factorization on synthetic data."""
    import numpy as np
    from spd_pi_engine.solver import YPattern

    n = 128
    diagonal = np.arange(n)
    rows = np.concatenate((diagonal, diagonal[1:], diagonal[:-1]))
    cols = np.concatenate((diagonal, diagonal[:-1], diagonal[1:]))
    values = np.concatenate((np.full(n, 4.0 + 0.25j),
                             np.full(n - 1, -1.0 + 0.05j),
                             np.full(n - 1, -1.0 - 0.05j)))
    matrix = YPattern(rows, cols, n).csc(values)
    rhs = np.zeros(n, dtype=complex)
    rhs[n // 3] = 1.0

    from scipy.sparse.linalg import splu
    solution = splu(matrix, permc_spec="COLAMD").solve(rhs)
    residual = float(np.max(np.abs(matrix @ solution - rhs)))
    if not np.all(np.isfinite(solution)) or not np.isfinite(residual) or residual > 1e-11:
        raise AssertionError(f"synthetic CPU solve residual {residual:.3e}")
    return {"matrix_size": n, "nnz": int(matrix.nnz), "max_abs_residual": residual,
            "solver": "scipy.sparse.linalg.splu"}


def _standalone_datafree_checks() -> dict:
    from spd_pi_engine import geometry, hardware, homogenise, receipt, solver, spd_source

    def hardware_check():
        profile = hardware.HardwareProfile.detect()
        if profile.cpu_physical < 1 or profile.cpu_logical < profile.cpu_physical:
            raise AssertionError("invalid detected CPU topology")
        if profile.ram_total_GB <= 0:
            raise AssertionError("invalid detected total RAM")
        plan = hardware.plan_sweep(profile, n_ports=1, solver="splu")
        if plan.jobs < 1 or plan.threads < 1:
            raise AssertionError("invalid CPU sweep plan")
        return plan

    checks = {
        "parser_api": _captured_check(spd_source.check_parser_api),
        "homogenise": _captured_check(homogenise.demo, require_zero=True),
        "solver_pattern": _captured_check(solver.demo),
        "geometry": _captured_check(geometry.demo),
        "receipt": _captured_check(receipt.demo),
        "hardware": _captured_check(hardware_check),
        "synthetic_cpu": _captured_check(_synthetic_cpu_check),
    }
    return checks


def _standalone_gpu_check() -> dict:
    from spd_pi_engine import solver

    result = _captured_check(solver.demo_cudss)
    result["comparison"] = "cudss_vs_scipy_splu"
    return result


def op_standalone_gate(request: dict) -> dict:
    checks = _standalone_datafree_checks()
    requested_designs = request.get("designs")
    if not isinstance(requested_designs, list) or not requested_designs:
        designs = [{"design_id": None, "status": "failed",
                    "error": "designs must be a non-empty list"}]
    else:
        designs = [_standalone_design_check(design) for design in requested_designs]

    gpu_requested = bool(request.get("gpu", False))
    gpu = (_standalone_gpu_check() if gpu_requested else
           {"status": "not_requested", "requested": False})
    if gpu_requested:
        gpu["requested"] = True
    required = list(checks.values()) + designs + ([gpu] if gpu_requested else [])
    return {
        "ok": bool(required) and all(item.get("status") == "passed" for item in required),
        "gate_kind": "standalone_runtime_and_inputs",
        "historical_reproduction": "not_run",
        "accuracy_verification": "baseline_receipts_and_report",
        "checks": checks,
        "designs": designs,
        "gpu": gpu,
    }


def op_prepare(request: dict) -> dict:
    from data_setup import prepare_data
    return prepare_data(data_dir=Path(request["data_dir"]), root=Path(request["root"]))


def op_report(request: dict) -> dict:
    from study_report import make_report
    return make_report(Path(request["root"]))


def op_compare(request: dict) -> dict:
    from study_report import compare_directories
    out = Path(request["out"]) if request.get("out") else None
    return compare_directories(Path(request["left"]), Path(request["right"]), out)


OPS = {"probe": op_probe, "ports": op_ports, "solve": op_solve, "converge": op_converge,
       "plan": op_plan, "basis": op_basis, "reference_check": op_reference_check,
       "report": op_report, "compare": op_compare, "prepare": op_prepare,
       "standalone_gate": op_standalone_gate}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: engine_worker.py REQUEST.json OUTPUT.json", file=sys.stderr)
        return 2
    request_path, output_path = map(Path, argv)
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        _set_affinity(request.get("affinity"))
        operation = request.get("operation")
        if operation not in OPS:
            raise ValueError(f"unknown worker operation: {operation!r}")
        if operation not in {"report", "compare"}:
            import spd_pi_engine
            configured = Path(request["engine_root"]).resolve()
            imported = Path(spd_pi_engine.__file__).resolve()
            if configured not in imported.parents:
                raise RuntimeError(f"imported engine {imported} is outside configured engine_root {configured}")
        result = OPS[operation](request)
        _write(output_path, {"ok": True, "operation": operation, "result": result})
        return 0
    except BaseException as exc:
        import traceback
        _write(output_path, {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc()})
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
