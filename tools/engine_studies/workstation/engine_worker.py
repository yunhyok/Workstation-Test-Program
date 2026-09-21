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


def op_report(request: dict) -> dict:
    from study_report import make_report
    return make_report(Path(request["root"]))


def op_compare(request: dict) -> dict:
    from study_report import compare_directories
    out = Path(request["out"]) if request.get("out") else None
    return compare_directories(Path(request["left"]), Path(request["right"]), out)


OPS = {"probe": op_probe, "ports": op_ports, "solve": op_solve, "converge": op_converge,
       "plan": op_plan, "basis": op_basis, "reference_check": op_reference_check,
       "report": op_report, "compare": op_compare}


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
