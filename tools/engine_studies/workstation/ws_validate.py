"""Workstation validation CLI.

This orchestrator uses only the Python standard library.  Engine imports and numerical calls are
confined to ``engine_worker.py`` running under ``config.json:engine_python``.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import shutil
import statistics
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

if getattr(sys, "frozen", False):
    PROGRAM_ROOT = Path(sys._MEIPASS)
    HERE = PROGRAM_ROOT / "tools" / "engine_studies" / "workstation"
else:
    HERE = Path(__file__).resolve().parent
    PROGRAM_ROOT = HERE.parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from common import (RunLock, Settings, atomic_json, input_identity, load_settings, read_json,
                    receipt_path, resumable_receipt, safe_name, sha256_file, unique_path, utc_now,
                    compare_receipt_vectors)


VERSION = "1.1.0"
HARD_VERSIONS = {"python": "3.12.10", "numpy": "2.4.4", "scipy": "1.18.0"}
REFERENCE_VERSIONS = {
    **HARD_VERSIONS, "matplotlib": "3.10.9",
    "nvmath-python": "1.0.0", "nvidia-cudss-cu12": "0.8.0.10", "cuda-bindings": "12.9.8",
    "cupy-cuda12x": "14.2.0", "shapely": "2.1.2", "pydantic": "2.13.3",
}
GPU_VERSION_NAMES = {"nvmath-python", "nvidia-cudss-cu12", "cupy-cuda12x", "cuda-bindings"}


def default_root() -> Path:
    if os.environ.get("SPD_PI_WORK_DIR"):
        return Path(os.environ["SPD_PI_WORK_DIR"]) / "w15"
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "WorkstationTestProgram" / "w15"
    return Path.cwd() / "w15"


class Status:
    def __init__(self, root: Path, command: str):
        self.path = root / "status.json"
        self.value = {
            "schema_version": 1, "version": VERSION, "running": True, "pid": os.getpid(),
            "subcommand": command, "started_at": utc_now(), "updated_at": utc_now(),
            "current_case": None, "completed": 0, "remaining": 0, "total": 0,
            "comparison": None, "exit_code": None, "stopped_at": None, "error": None,
        }
        self._last = 0.0
        self.write(force=True)

    def update(self, *, force=False, **values):
        self.value.update(values)
        self.write(force=force)

    def write(self, force=False):
        now = time.monotonic()
        if force or now - self._last >= 10.0:
            self.value["updated_at"] = utc_now()
            atomic_json(self.path, self.value)
            self._last = now

    def finish(self, code: int, error: str | None = None, stopped=False):
        self.value.update(running=False, exit_code=code, error=error,
                          stopped_at=utc_now() if stopped else self.value.get("stopped_at"),
                          finished_at=utc_now())
        self.write(force=True)


def _stop_mode(path: Path | None, stop_now: bool) -> str | None:
    if not path or not path.exists():
        return None
    if stop_now:
        return "now"
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        value = "graceful"
    return "now" if value in {"now", "immediate", "kill"} else "graceful"


def _popen_group(command, **kwargs):
    if sys.platform == "win32":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"could not kill process tree {process.pid}: {exc}") from exc
        if result.returncode != 0 and process.poll() is None:
            raise RuntimeError(f"taskkill failed for process tree {process.pid} with exit {result.returncode}")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError as exc:
            if process.poll() is None:
                raise RuntimeError(f"could not kill process group {process.pid}: {exc}") from exc
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"process tree {process.pid} did not exit after forced termination") from exc


def _sample_vram_once() -> float | None:
    command = ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=5)
        values = [float(value.strip()) for value in proc.stdout.splitlines() if value.strip()]
        return max(values) if proc.returncode == 0 and values else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _responsive_wait(seconds: float, stop_file: Path | None, stop_now: bool) -> str | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        mode = _stop_mode(stop_file, stop_now)
        if mode:
            return mode
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return _stop_mode(stop_file, stop_now)


def _collect_idle_vram(stop_file: Path | None, stop_now: bool) -> tuple[float | None, list[float], str | None]:
    samples: list[float] = []
    for index in range(5):
        mode = _stop_mode(stop_file, stop_now)
        if mode:
            return None, samples, mode
        sample = _sample_vram_once()
        if sample is None:
            return None, [], None
        samples.append(sample)
        if index < 4:
            mode = _responsive_wait(1.0, stop_file, stop_now)
            if mode:
                return None, samples, mode
    return float(statistics.median(samples)), samples, None


class VramSampler:
    def __init__(self):
        self.samples: list[dict[str, Any]] = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)

    def _run(self):
        command = ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
        while not self.stop_event.is_set():
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=5)
                values = [float(x.strip()) for x in proc.stdout.splitlines() if x.strip()]
                if proc.returncode == 0 and values:
                    self.samples.append({"at": utc_now(), "monotonic": time.perf_counter(),
                                         "memory_used_MB": values})
            except Exception:
                pass
            self.stop_event.wait(1.0)


def _run_process(command: list[str], *, cwd: Path, env: dict[str, str], log,
                 status: Status, stop_file: Path | None, stop_now: bool) -> tuple[int, str | None]:
    process = _popen_group(command, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                           text=True)
    try:
        while process.poll() is None:
            mode = _stop_mode(stop_file, stop_now)
            if mode == "now":
                _kill_tree(process)
                process.wait()
                return process.returncode, "now"
            status.write()
            time.sleep(0.5)
        return process.returncode, _stop_mode(stop_file, stop_now)
    except BaseException:
        _kill_tree(process)
        process.wait()
        raise


def _worker_paths(run_dir: Path, label: str) -> tuple[Path, Path]:
    stem = safe_name(label) + f"-{time.time_ns()}"
    folder = run_dir / "worker"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{stem}.request.json", folder / f"{stem}.output.json"


def _call_worker(settings: Settings, request: dict, run_dir: Path, label: str, log,
                 status: Status, stop_file: Path | None, stop_now: bool) -> dict:
    request = {**request, "engine_root": str(settings.engine_root)}
    request_path, output_path = _worker_paths(run_dir, label)
    atomic_json(request_path, request)
    env = os.environ.copy()
    env["SPD_PI_DATA_DIR"] = str(settings.data_dir)
    env["SPD_PI_WORK_DIR"] = str(settings.root)
    threads = str(max(1, int(request.get("threads", 1))))
    env.update({key: threads for key in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS")})
    code, stopped = _run_process([str(settings.engine_python), str(HERE / "engine_worker.py"),
                                  str(request_path), str(output_path)], cwd=settings.engine_root,
                                 env=env, log=log, status=status, stop_file=stop_file,
                                 stop_now=stop_now)
    result = read_json(output_path, {})
    if stopped == "now":
        raise KeyboardInterrupt("immediate stop requested")
    if code != 0 or not result.get("ok"):
        raise RuntimeError(result.get("error") or f"engine worker exited {code}")
    return result["result"]


def _new_run(root: Path, command: str):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = unique_path(root / "runs" / f"{stamp}-{safe_name(command)}")
    run_dir.mkdir(parents=True)
    return run_dir, (run_dir / "log.txt").open("a", encoding="utf-8", buffering=1)


def _git_head(root: Path) -> dict:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, timeout=20)
        return {"returncode": proc.returncode, "head": proc.stdout.strip(),
                "stderr": proc.stderr.strip()}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _engine_fingerprint(settings: Settings, probe: dict) -> tuple[str, dict]:
    detected = probe.get("profiles", {}).get("detected", {})
    hardware = {key: detected.get(key) for key in ("cpu_physical", "cpu_logical", "ram_total_GB")}
    hardware["gpus"] = [{key: gpu.get(key) for key in ("index", "name", "vram_total_MB")}
                         for gpu in detected.get("gpus", [])]
    driver = [{key: gpu.get(key) for key in ("index", "name", "driver", "vram_total_MB")}
              for gpu in probe.get("nvidia_smi", {}).get("gpus", [])]
    evidence = {"config_sha256": sha256_file(settings.root / "config.json"),
                "engine_git": _git_head(settings.engine_root),
                "engine_source_hash": probe.get("engine", {}).get("source_hash"),
                "engine_module": probe.get("engine", {}).get("module"),
                "python": probe.get("python"), "packages": probe.get("packages"),
                "gpu_driver": driver, "detected_hardware": hardware}
    return input_identity(evidence, {})[0], evidence


def _version_evidence(probe: dict) -> tuple[dict, dict, list[str]]:
    actual = {"python": probe.get("python", {}).get("version"), **probe.get("packages", {})}
    hard_checks = {name: {"required": expected, "actual": actual.get(name),
                          "pass": actual.get(name) == expected}
                   for name, expected in HARD_VERSIONS.items()}
    differences = {name: {"reference": expected, "actual": actual.get(name)}
                   for name, expected in REFERENCE_VERSIONS.items()
                   if actual.get(name) != expected}
    warnings = [f"GPU package differs from reference: {name} "
                f"{item['actual']!r} (reference {item['reference']!r})"
                for name, item in differences.items() if name in GPU_VERSION_NAMES]
    return hard_checks, differences, warnings


def command_env(args, status: Status, run_dir: Path, log) -> int:
    record: dict[str, Any] = {"schema_version": 1, "created_at": utc_now(), "ok": False,
                              "errors": [], "warnings": [], "hard_versions": HARD_VERSIONS,
                              "reference_versions": REFERENCE_VERSIONS}
    config_path = args.root / "config.json"
    if not config_path.is_file():
        record.update(configured_engine=False, host_python=platform.python_version(),
                      host_platform=platform.platform(),
                      errors=[f"engine unavailable: missing runtime settings {config_path}"])
        atomic_json(args.root / "env.json", record)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0
    try:
        settings = load_settings(args.root, require_paths=False)
        record["configured_engine"] = True
        record["settings"] = {"engine_python": str(settings.engine_python),
                              "engine_root": str(settings.engine_root), "data_dir": str(settings.data_dir)}
        path_errors = []
        if not settings.engine_python.is_file(): path_errors.append(f"engine_python missing: {settings.engine_python}")
        if not settings.engine_root.is_dir(): path_errors.append(f"engine_root missing: {settings.engine_root}")
        if not settings.data_dir.is_dir(): path_errors.append(f"data_dir missing: {settings.data_dir}")
        if path_errors:
            raise ValueError("; ".join(path_errors))
        probe = _call_worker(settings, {"operation": "probe"}, run_dir, "env", log, status,
                             args.stop_file, args.stop_now)
        record["probe"] = probe
        record["engine_git"] = _git_head(settings.engine_root)
        checks, differences, warnings = _version_evidence(probe)
        record.update(version_checks=checks, version_differences=differences, warnings=warnings)
        if settings.laptop_freeze:
            if settings.laptop_freeze.is_file():
                freeze = {}
                for line in settings.laptop_freeze.read_text(encoding="utf-8").splitlines():
                    if "==" in line and not line.lstrip().startswith("#"):
                        name, version = line.split("==", 1)
                        freeze[re.sub(r"[-_.]+", "-", name).lower()] = version.strip()
                record["laptop_freeze_diff"] = {
                    name: {"laptop": version, "workstation": probe["all_packages"].get(name)}
                    for name, version in freeze.items()
                    if probe["all_packages"].get(name) != version
                }
            else:
                record["errors"].append(f"laptop_freeze missing: {settings.laptop_freeze}")
        record["ok"] = all(item["pass"] for item in checks.values())
        if not record["ok"]:
            record["errors"].append("required Python/package versions do not match")
    except Exception as exc:
        record["errors"].append(f"{type(exc).__name__}: {exc}")
    atomic_json(args.root / "env.json", record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record["ok"] else 1


def _junit_cases(path: Path) -> list[dict]:
    tree = ET.parse(path)
    out = []
    for case in tree.iter("testcase"):
        state = "passed"
        if case.find("skipped") is not None: state = "skipped"
        elif case.find("failure") is not None: state = "failed"
        elif case.find("error") is not None: state = "error"
        out.append({"classname": case.attrib.get("classname", ""), "name": case.attrib.get("name", ""),
                    "state": state})
    return out


def _required_gate_outcomes(suite: str, cases: list[dict]) -> tuple[bool, dict]:
    reproduction = [c for c in cases if c["classname"].endswith("test_reproduction")]
    passed = [c for c in reproduction if c["state"] == "passed"]
    names = [c["name"] for c in passed]
    count = lambda pattern: sum(bool(re.match(pattern, name)) for name in names)
    if suite == "default":
        rules = {"package_cpu": count(r"^test_variant(?:_[a-z]+)?_cases\[") >= 2,
                 "pcb_cpu": count(r"^test_pcb_.+(?<!_gpu)\[") >= 1,
                 "reference": count(r"^test_attach_reference_") >= 1}
    elif suite == "gpu":
        rules = {"package_gpu": count(r"^test_variant(?:_[a-z]+)?_cases_gpu\[") >= 2,
                 "pcb_gpu": count(r"^test_pcb_.+_gpu\[") >= 1}
    else:
        rules = {"legacy": count(r"^test_legacy_") >= 1,
                 "package_cpu_all": count(r"^test_variant(?:_[a-z]+)?_cases\[") >= 7,
                 "pcb_cpu_all": count(r"^test_pcb_.+(?<!_gpu)\[") >= 2}
    return all(rules.values()), {"rules": rules, "reproduction_passed": len(passed),
                                "reproduction_skipped": sum(c["state"] == "skipped" for c in reproduction)}


def command_gates(args, status: Status, run_dir: Path, log) -> int:
    settings = load_settings(args.root)
    missing = []
    for design_id, spec in settings.designs.items():
        for kind, path in (("spd", spec.spd), ("reference", spec.reference)):
            if path is None or not path.is_file():
                missing.append(f"{design_id} {kind}: {path}")
    result = {"schema_version": 1, "created_at": utc_now(), "ok": False,
              "preflight": {"missing": missing}, "suites": []}
    if missing:
        atomic_json(args.root / "gates.json", result)
        raise RuntimeError("gate data preflight failed: " + "; ".join(missing))
    probe = _call_worker(settings, {"operation": "probe"}, run_dir, "gate-probe", log, status,
                         args.stop_file, args.stop_now)
    checks, differences, warnings = _version_evidence(probe)
    result.update(version_checks=checks, version_differences=differences, warnings=warnings)
    if not all(item["pass"] for item in checks.values()):
        result["preflight"]["version_mismatch"] = {
            name: item for name, item in checks.items() if not item["pass"]}
        atomic_json(args.root / "gates.json", result)
        return 1
    reference_checks = {}
    for design_id, ports in settings.port_sets["P9"].items():
        spec = settings.designs[design_id]
        reference_checks[design_id] = _call_worker(
            settings, {"operation": "reference_check", "reference": str(spec.reference),
                       "ports": list(ports)}, run_dir, f"reference-{design_id}", log, status,
            args.stop_file, args.stop_now)
    fingerprint, engine_evidence = _engine_fingerprint(settings, probe)
    result.update(engine_fingerprint=fingerprint, engine_evidence=engine_evidence,
                  reference_checks=reference_checks)
    suites = [("default", []), ("gpu", ["--gpu"]), ("slow", ["--slow"])]
    env = os.environ.copy()
    env.update(SPD_PI_DATA_DIR=str(settings.data_dir), SPD_PI_WORK_DIR=str(settings.root))
    for index, (name, flags) in enumerate(suites):
        if _stop_mode(args.stop_file, args.stop_now):
            result["stopped_at"] = utc_now()
            break
        xml_path = run_dir / f"pytest-{name}.xml"
        command = [str(settings.engine_python), "-m", "pytest", "tests/engine", "-q", *flags,
                   f"--junitxml={xml_path}"]
        status.update(current_case=f"gates:{name}", completed=index, remaining=len(suites)-index,
                      total=len(suites))
        started = time.perf_counter()
        code, stopped = _run_process(command, cwd=settings.engine_root, env=env, log=log, status=status,
                                     stop_file=args.stop_file, stop_now=args.stop_now)
        cases = _junit_cases(xml_path) if xml_path.is_file() else []
        outcomes_ok, outcome_evidence = _required_gate_outcomes(name, cases)
        suite_result = {"name": name, "command": command, "returncode": code,
                        "wall_seconds": time.perf_counter()-started, "junit": str(xml_path),
                        "tests": len(cases), "passed": sum(c["state"] == "passed" for c in cases),
                        "skipped": sum(c["state"] == "skipped" for c in cases),
                        "required_outcomes": outcome_evidence,
                        "ok": code == 0 and bool(cases) and outcomes_ok}
        result["suites"].append(suite_result)
        atomic_json(args.root / "gates.json", result)
        if stopped or not suite_result["ok"]:
            if stopped:
                result["stopped_at"] = utc_now()
            break
    result["ok"] = len(result["suites"]) == 3 and all(x["ok"] for x in result["suites"])
    result["engine_git"] = engine_evidence["engine_git"]
    atomic_json(args.root / "gates.json", result)
    return 130 if result.get("stopped_at") else (0 if result["ok"] else 1)


def _measurement_unlocked(settings: Settings, probe: dict) -> None:
    root = settings.root
    gates = read_json(root / "gates.json", {})
    if not gates.get("ok") or len(gates.get("suites", [])) != 3:
        raise RuntimeError(f"measurements are locked: three genuine engine gates have not passed in {root/'gates.json'}")
    current, _ = _engine_fingerprint(settings, probe)
    if gates.get("engine_fingerprint") != current:
        raise RuntimeError("measurements are locked: config, engine source, Python, or package versions changed after gates")
    checks = {name: probe.get("python", {}).get("version") if name == "python"
              else probe.get("packages", {}).get(name) for name in HARD_VERSIONS}
    wrong = {name: (checks[name], expected) for name, expected in HARD_VERSIONS.items()
             if checks[name] != expected}
    if wrong:
        raise RuntimeError(f"measurements are locked: required versions do not match: {wrong}")
    plan = PROGRAM_ROOT / "docs" / "engine" / "W15_PLAN_20260921.md"
    if not plan.is_file() or plan.stat().st_size < 100:
        raise RuntimeError(f"measurements are locked: preregistration is missing or empty: {plan}")


def _measurement_prepare(settings: Settings, args, status: Status, run_dir: Path, log) -> dict:
    probe = _call_worker(settings, {"operation": "probe"}, run_dir, "measurement-probe", log,
                         status, args.stop_file, args.stop_now)
    _measurement_unlocked(settings, probe)
    _call_worker(settings, {"operation": "plan", "backend": "splu", "n_ports": 1}, run_dir,
                 "measurement-plan-smoke", log, status, args.stop_file, args.stop_now)
    return probe


def _ports_for(settings: Settings, label: str, design_filter: str | None,
               run_dir: Path | None = None, log=None, status: Status | None = None,
               args=None) -> list[tuple[str, str]]:
    if label not in settings.port_sets:
        raise ValueError(f"unknown configured port set {label!r}")
    if design_filter and design_filter not in settings.designs:
        raise ValueError(f"unknown configured design {design_filter!r}")
    mapping = settings.port_sets[label]
    if design_filter:
        mapping = {design_filter: mapping[design_filter]} if design_filter in mapping else {}
    selected: list[tuple[str, str]] = []
    for design_id, configured in mapping.items():
        if configured == "all":
            spec = settings.designs[design_id]
            value = _call_worker(settings, {"operation": "ports", "spd": str(spec.spd)},
                                 run_dir or settings.root, f"ports-{design_id}", log, status,
                                 getattr(args, "stop_file", None), getattr(args, "stop_now", False))
            ports = value.get("ports", [])
        else:
            ports = configured
        selected.extend((design_id, port) for port in dict.fromkeys(ports))
    if not selected:
        raise ValueError(f"port set {label} has no cases for design {design_filter!r}")
    return selected


def _case(settings: Settings, command: str, design_id: str, port: str, backend: str, threads: int,
          jobs: int, profile: str, axis: str | None = None, repeat: int = 0,
          affinity: int | None = None, extra: dict | None = None,
          comparison_role: str | None = None, operation: str | None = None) -> dict:
    spec = settings.designs[design_id]
    worker_operation = operation or ("converge" if command == "converge" else "solve")
    request = {"operation": worker_operation, "spd": str(spec.spd),
               "reference": str(spec.reference) if spec.reference else None, "port": port,
               "cache_dir": str(settings.root / "cache"), "backend": backend, "threads": threads}
    if affinity:
        request["affinity"] = affinity
    comparison_group = input_identity({"design": design_id, "port": port,
                                        "spd": sha256_file(spec.spd),
                                        "reference": sha256_file(spec.reference) if spec.reference else None,
                                        "mesh": "p-powersi-h200-fh50-top50-sub20-10-10-fringe"}, {})[0]
    identity_request = {"schema_version": 1, "command": command, "design": design_id,
                        "family": spec.family, "port": port, "backend": backend,
                        "threads": threads, "jobs": jobs, "profile": profile, "axis": axis,
                        "repeat": repeat, "affinity": affinity, "request": request,
                        "gate_fingerprint": read_json(settings.root / "gates.json", {}).get("engine_fingerprint"),
                        "worker_sha256": sha256_file(HERE / "engine_worker.py"), "extra": extra or {}}
    identity_request.update(comparison_group=comparison_group,
                            comparison_role=comparison_role or ("cpu_candidate" if backend == "splu"
                                                                else "gpu_candidate"))
    identity, hashes = input_identity(identity_request, {"spd": spec.spd, "reference": spec.reference})
    label = f"{command}-{design_id}-{port}-{backend}-t{threads}-j{jobs}-{profile}"
    if repeat: label += f"-r{repeat}"
    return {"request": request, "identity_request": identity_request, "identity": identity,
            "hashes": hashes, "label": label, "extra": extra or {}}


def _run_cases(settings: Settings, cases: list[dict], jobs: int, status: Status, run_dir: Path, log,
               args, *, status_offset: int = 0, status_total: int | None = None) -> tuple[int, bool]:
    receipts_dir = settings.root / "receipts"
    failures_dir = settings.root / "failures"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    failures_dir.mkdir(parents=True, exist_ok=True)
    pending = deque()
    completed = 0
    resumed = 0
    for case in cases:
        existing = resumable_receipt(receipts_dir, case["identity"])
        if existing:
            completed += 1
            resumed += 1
        else:
            pending.append(case)
    local_total = len(cases)
    total = status_total if status_total is not None else local_total
    status.update(total=total, completed=status_offset + completed,
                  remaining=total - status_offset - completed)
    stopped = False
    failures = 0
    group_started_at = utc_now()
    group_started = time.perf_counter()
    executed_success: list[str] = []
    while pending:
        mode = _stop_mode(args.stop_file, args.stop_now)
        if mode:
            stopped = True
            break
        batch = [pending.popleft() for _ in range(min(max(1, jobs), len(pending)))]
        active = []
        idle_vram, idle_samples, idle_stop = _collect_idle_vram(args.stop_file, args.stop_now)
        if idle_stop:
            stopped = True
            break
        sampler = VramSampler()
        sampler.start()
        immediate = False
        stop_after_batch = False
        try:
            for case in batch:
                mode = _stop_mode(args.stop_file, args.stop_now)
                if mode:
                    stop_after_batch = True
                    immediate = mode == "now"
                    break
                request_path, output_path = _worker_paths(run_dir, case["label"])
                atomic_json(request_path, {**case["request"], "engine_root": str(settings.engine_root)})
                env = os.environ.copy()
                env.update(SPD_PI_DATA_DIR=str(settings.data_dir), SPD_PI_WORK_DIR=str(settings.root))
                thread_text = str(case["identity_request"]["threads"])
                env.update({key: thread_text for key in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS")})
                process = _popen_group([str(settings.engine_python), str(HERE / "engine_worker.py"),
                                        str(request_path), str(output_path)], cwd=str(settings.engine_root),
                                       env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
                active.append({"case": case, "process": process, "output": output_path,
                               "started": time.perf_counter(), "started_at": utc_now(),
                               "finished": None, "finished_at": None})
            if immediate:
                for item in active:
                    _kill_tree(item["process"])
            while any(item["process"].poll() is None for item in active):
                for item in active:
                    if item["finished"] is None and item["process"].poll() is not None:
                        item["finished"] = time.perf_counter()
                        item["finished_at"] = utc_now()
                mode = _stop_mode(args.stop_file, args.stop_now)
                if mode == "now":
                    immediate = True
                    for item in active: _kill_tree(item["process"])
                status.update(current_case=[item["case"]["label"] for item in active
                                            if item["process"].poll() is None],
                              completed=status_offset + completed,
                              remaining=total - status_offset - completed)
                time.sleep(0.25)
            for item in active:
                item["finished"] = item["finished"] or time.perf_counter()
                item["finished_at"] = item["finished_at"] or utc_now()
        except BaseException:
            for item in active:
                _kill_tree(item["process"])
                item["process"].wait()
            raise
        finally:
            sampler.stop()
        successful_receipts = [read_json(item["output"], {}).get("result", {}) for item in active
                               if item["process"].returncode == 0]
        batch_rss_sum = sum(float(rec.get("peak_rss_MB") or 0) for rec in successful_receipts)
        for item in active:
            process, case = item["process"], item["case"]
            process.wait()
            output = read_json(item["output"], {})
            if immediate:
                continue
            if process.returncode != 0 or not output.get("ok"):
                failures += 1
                atomic_json(unique_path(failures_dir / f"{safe_name(case['label'])}.json"), {
                    "created_at": utc_now(), "case": case["identity_request"],
                    "input_identity": case["identity"], "worker": output,
                    "returncode": process.returncode})
                continue
            receipt = output["result"]
            worker_wall = receipt.get("worker", {}).get("wall_seconds") if isinstance(receipt, dict) else None
            stats = receipt.get("stats") or []
            actual_solvers = sorted({str(row.get("solver")) for row in stats if row.get("solver")})
            worker_solver = (receipt.get("worker") or {}).get("actual_solver")
            if worker_solver and worker_solver not in actual_solvers:
                actual_solvers.append(worker_solver); actual_solvers.sort()
            basis_meta = receipt.get("decap_basis") or {}
            build_seconds = basis_meta.get("build_seconds", (receipt.get("build_info") or {}).get(
                "build_seconds", receipt.get("build_seconds")))
            factor_seconds = sum(float(row.get("factor_s") or row.get("solve_s") or 0) for row in stats)
            assemble_seconds = sum(float(row.get("assemble_s") or 0) for row in stats)
            samples = [s for s in sampler.samples
                       if item["started"] <= s["monotonic"] <= item["finished"]]
            peak = max((max(s["memory_used_MB"]) for s in samples), default=None)
            delta = max(0.0, peak - idle_vram) if peak is not None and idle_vram is not None else None
            per_process = delta / max(1, len(active)) if delta is not None else None
            study = {**case["identity_request"], "case_id": case["label"],
                     "input_identity": case["identity"], "started_at": item["started_at"],
                     "finished_at": item["finished_at"], "wall_seconds": item["finished"]-item["started"],
                     "worker_wall_seconds": worker_wall, "peak_rss_MB": receipt.get("peak_rss_MB"),
                     "actual_concurrency": len(active),
                     "build_seconds": build_seconds, "factor_seconds": factor_seconds,
                     "assemble_seconds": assemble_seconds, "actual_solvers": actual_solvers,
                     "vram_samples_MB": samples, "vram_peak_total_MB": peak,
                     "vram_idle_total_MB": idle_vram, "vram_idle_samples_MB": idle_samples,
                     "vram_delta_MB": delta,
                     "vram_peak_MB": per_process,
                     "vram_scope": "estimated per-process delta=(whole-GPU peak-idle)/concurrent workers",
                     "source_spd_sha256": case["hashes"]["spd"],
                     "source_ref_sha256": case["hashes"]["reference"], "resumed": False,
                     **case["extra"]}
            axis = case["identity_request"].get("axis")
            if axis == "C":
                study["outcome"] = bool(case["extra"].get("planner", {}).get("detect_reflects_affinity"))
            elif axis == "D":
                study["batch_peak_rss_sum_MB"] = batch_rss_sum
                study["outcome"] = batch_rss_sum <= float(case["extra"]["ram_budget_MB"])
            elif axis == "E":
                requested = case["identity_request"]["backend"]
                study["solver_match"] = requested == "splu" or (bool(actual_solvers) and
                                         all(name == "cudss" for name in actual_solvers))
            receipt["study"] = study
            atomic_json(receipt_path(receipts_dir, case["label"], case["identity"]), receipt)
            completed += 1
            executed_success.append(case["identity"])
        if immediate or stop_after_batch:
            stopped = True
            break
        if _stop_mode(args.stop_file, args.stop_now) == "graceful":
            stopped = True
            break
    status.update(current_case=None, completed=status_offset + completed,
                  remaining=total-status_offset-completed)
    if cases and len({(c["identity_request"].get("axis"), c["identity_request"].get("profile"),
                       c["identity_request"].get("backend"), c["identity_request"].get("threads"),
                       c["identity_request"].get("jobs"), c["identity_request"].get("affinity"))
                      for c in cases}) == 1:
        first = cases[0]["identity_request"]
        group_key = {key: first.get(key) for key in
                     ("axis", "profile", "backend", "threads", "jobs", "affinity")}
        group_id = input_identity({"group": group_key,
                                   "cases": sorted(c["identity"] for c in cases)}, {})[0]
        record = {"schema_version": 1, "group_id": group_id, **group_key,
                  "started_at": group_started_at, "finished_at": utc_now(),
                  "total_wall_seconds": time.perf_counter() - group_started,
                  "wall_scope": "this immutable execution attempt; aggregate attempts by group_id",
                  "case_count": len(cases), "resumed_case_count": resumed,
                  "executed_case_count": len(executed_success),
                  "maximum_actual_concurrency": min(int(first.get("jobs") or 1),
                                                     len(executed_success)),
                  "failed_case_count": failures, "stopped": stopped,
                  "case_identities": sorted(c["identity"] for c in cases),
                  "executed_case_identities": sorted(executed_success)}
        folder = settings.root / "group_runs"; folder.mkdir(parents=True, exist_ok=True)
        atomic_json(unique_path(folder / f"{group_id[:16]}.json"), record)
    return failures, stopped


def _receipt_by_identity(root: Path, identity: str) -> Path | None:
    return resumable_receipt(root / "receipts", identity)


def _gpu_cases_used_cudss(root: Path, cases: list[dict]) -> bool:
    for case in cases:
        if case["identity_request"]["backend"] not in {"auto", "cudss"}:
            continue
        path = _receipt_by_identity(root, case["identity"])
        receipt = read_json(path, {}) if path else {}
        solvers = receipt.get("study", {}).get("actual_solvers", [])
        if not solvers or any(solver != "cudss" for solver in solvers):
            return False
    return True


def _compare_case_sets(settings: Settings, left_cases: list[dict], right_cases: list[dict],
                       label: str, run_dir: Path, log, status: Status, args) -> dict:
    folder = run_dir / "comparison-inputs" / safe_name(label)
    left_dir, right_dir = folder / "left", folder / "right"
    left_dir.mkdir(parents=True, exist_ok=True); right_dir.mkdir(parents=True, exist_ok=True)
    for index, case in enumerate(left_cases):
        path = _receipt_by_identity(settings.root, case["identity"])
        if path: shutil.copy2(path, left_dir / f"{index:04d}-{path.name}")
    for index, case in enumerate(right_cases):
        path = _receipt_by_identity(settings.root, case["identity"])
        if path: shutil.copy2(path, right_dir / f"{index:04d}-{path.name}")
    out = unique_path(settings.root / "comparisons" / f"{safe_name(label)}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    return _call_worker(settings, {"operation": "compare", "left": str(left_dir),
                                    "right": str(right_dir), "out": str(out)}, run_dir,
                        f"compare-{label}", log, status, args.stop_file, args.stop_now)


def _compare_external_to_cases(settings: Settings, external: Path, cases: list[dict], label: str,
                               run_dir: Path, log, status: Status, args) -> dict:
    right_dir = run_dir / "comparison-inputs" / safe_name(label) / "right"
    right_dir.mkdir(parents=True, exist_ok=True)
    for index, case in enumerate(cases):
        path = _receipt_by_identity(settings.root, case["identity"])
        if path: shutil.copy2(path, right_dir / f"{index:04d}-{path.name}")
    out = unique_path(settings.root / "comparisons" / f"{safe_name(label)}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    return _call_worker(settings, {"operation": "compare", "left": str(external),
                                    "right": str(right_dir), "out": str(out)}, run_dir,
                        f"compare-{label}", log, status, args.stop_file, args.stop_now)


def _write_run_summary(run_dir: Path, value: dict) -> None:
    path = run_dir / "run_summary.json"
    if path.exists():
        raise RuntimeError(f"immutable run summary already exists: {path}")
    atomic_json(path, {"schema_version": 1, **value})


def _comparison_failure(run_dir: Path, name: str, detail: dict) -> str:
    folder = run_dir / "comparison_failures"
    folder.mkdir(parents=True, exist_ok=True)
    path = unique_path(folder / f"{safe_name(name)}.json")
    atomic_json(path, {"schema_version": 1, "name": name, "created_at": utc_now(),
                       "detail": detail})
    return str(path)


def command_baseline(args, status: Status, run_dir: Path, log) -> int:
    settings = load_settings(args.root)
    _measurement_prepare(settings, args, status, run_dir, log)
    selected = _ports_for(settings, args.ports, args.design, run_dir, log, status, args)
    backends = [args.backend] if args.backend else ["splu", "auto"]
    cases = []
    for backend in backends:
        repeats = 2 if backend in ("auto", "cudss") else 1
        for repeat in range(repeats):
            for design_id, port in selected:
                role = ("cpu_reference" if backend == "splu" else
                        ("gpu_candidate" if repeat == 0 else "gpu_repeat"))
                cases.append(_case(settings, "baseline", design_id, port, backend, args.threads,
                                   args.jobs, args.profile, axis="A", repeat=repeat,
                                   comparison_role=role))
    failures, stopped = _run_cases(settings, cases, args.jobs, status, run_dir, log, args)
    solver_ok = _gpu_cases_used_cudss(settings.root, cases)
    comparison_ok = True
    comparison_state = "not_configured"
    comparison_failures: list[dict] = []
    comparisons: list[dict] = []

    def record(name: str, result: dict, *, configured=False) -> None:
        nonlocal comparison_ok, comparison_state
        comparisons.append({"name": name, "pass": result.get("pass") is True})
        if result.get("pass") is True:
            if configured and comparison_state != "failed":
                comparison_state = "passed"
            return
        comparison_ok = False
        comparison_state = "failed"
        comparison_failures.append({"name": name,
                                    "log": _comparison_failure(run_dir, name, result)})

    def attempt(name: str, function, *, configured=False) -> None:
        try:
            result = function()
        except Exception as exc:
            result = {"pass": False, "failures": [f"{type(exc).__name__}: {exc}"]}
        record(name, result, configured=configured)

    if stopped:
        comparison_state = "stopped"
    elif settings.laptop_receipts is not None:
        if settings.laptop_receipts.is_dir():
            attempt("baseline-laptop-vs-workstation", lambda: _compare_external_to_cases(
                settings, settings.laptop_receipts, cases, "baseline", run_dir, log, status, args),
                configured=True)
        else:
            record("baseline-laptop-receipts-missing", {
                "pass": False, "path": str(settings.laptop_receipts),
                "failures": ["configured laptop_receipts directory is missing"]}, configured=True)
    if not stopped:
        cpu = [c for c in cases if c["identity_request"]["comparison_role"] == "cpu_reference"]
        gpu_first = [c for c in cases if c["identity_request"]["comparison_role"] == "gpu_candidate"]
        gpu_repeat = [c for c in cases if c["identity_request"]["comparison_role"] == "gpu_repeat"]
        if gpu_first:
            attempt("baseline-cpu-vs-gpu", lambda: _compare_case_sets(
                settings, cpu, gpu_first, "baseline-cpu-vs-gpu", run_dir, log, status, args))
        if gpu_repeat:
            attempt("baseline-gpu-repeat", lambda: _compare_case_sets(
                settings, gpu_first, gpu_repeat, "baseline-gpu-repeat", run_dir, log, status, args))
        if not solver_ok:
            record("baseline-gpu-solver-proof", {
                "pass": False, "failures": ["GPU cases did not prove the requested GPU solver"]})
    code = 130 if stopped else (1 if failures or not comparison_ok or not solver_ok else 0)
    status.update(comparison=comparison_state)
    _write_run_summary(run_dir, {"command": "baseline", "finished_at": utc_now(),
                                 "exit_code": code, "comparison": comparison_state,
                                 "comparison_failures": comparison_failures,
                                 "comparisons": comparisons,
                                 "worker_failure_count": failures, "solver_proof": solver_ok,
                                 "stopped": stopped})
    return code


def _plan(settings, profile, backend, run_dir, log, status, args, affinity=None):
    request = {"operation": "plan", "backend": backend, "n_ports": 92}
    if profile: request["profile"] = profile
    if affinity: request["affinity"] = affinity
    return _call_worker(settings, request, run_dir, f"plan-{backend}-{affinity or 'profile'}", log,
                        status, args.stop_file, args.stop_now)


def _publish_matrix_plan(root: Path, run_dir: Path, update: dict) -> dict:
    """Merge independently-run axes while preserving the plan replaced by this run."""
    path = root / "matrix_plan.json"
    previous = read_json(path, {})
    previous_axes = previous.get("axes", {}) if isinstance(previous, dict) else {}
    if not isinstance(previous_axes, dict):
        previous_axes = {}
    if path.is_file():
        archive = run_dir / "archives" / "matrix_plan.before.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(unique_path(archive), previous)
    merged = {"schema_version": 1,
              "created_at": previous.get("created_at", update.get("created_at", utc_now()))
              if isinstance(previous, dict) else update.get("created_at", utc_now()),
              "updated_at": utc_now(), "axes": {**previous_axes, **update.get("axes", {})}}
    atomic_json(path, merged)
    return merged


def command_matrix(args, status: Status, run_dir: Path, log) -> int:
    settings = load_settings(args.root)
    _measurement_prepare(settings, args, status, run_dir, log)
    selected = _ports_for(settings, args.ports, args.design, run_dir, log, status, args)
    axes = list("BCDE") if args.axis == "all" else [args.axis]
    all_cases = []
    planning = {"schema_version": 1, "created_at": utc_now(), "axes": {}}
    if "B" in axes:
        combos = [(t, j) for t in (1, 2, 4, 8, 16) for j in (1, 4, 8, 15, 30) if t*j <= 64]
        planning["axes"]["B"] = {"combinations": combos, "constraint": "jobs*threads <= 64",
                                   "expected_ports": [{"design": d, "port": p} for d, p in selected]}
        for threads, jobs in combos:
            for design_id, port in selected:
                all_cases.append(_case(settings, "matrix", design_id, port, "splu", threads, jobs,
                                       args.profile, axis="B",
                                       comparison_role="cpu_reference" if jobs == 1 else "cpu_candidate"))
    if "C" in axes:
        c_selected = _ports_for(settings, "P92", args.design, run_dir, log, status, args)
        plans = {}
        for affinity in (8, 16, 32, 64):
            if affinity > (os.cpu_count() or 1):
                plans[str(affinity)] = {"supported": False,
                                        "reason": f"host has only {os.cpu_count() or 1} logical CPUs"}
                continue
            plan = _plan(settings, None, "splu", run_dir, log, status, args, affinity)
            plans[str(affinity)] = plan
            jobs = int(plan["sweep"]["jobs"])
            threads = int(plan["sweep"]["threads"])
            for design_id, port in c_selected:
                all_cases.append(_case(settings, "matrix", design_id, port, "splu", threads, jobs,
                                       f"affinity{affinity}", axis="C", affinity=affinity,
                                       extra={"planner": plan}))
        planning["axes"]["C"] = {"plans": plans,
                                   "expected_ports": [{"design": d, "port": p} for d, p in c_selected]}
    if "D" in axes:
        d_selected = _ports_for(settings, "P92", args.design, run_dir, log, status, args)
        base = _plan(settings, None, "splu", run_dir, log, status, args)["profile"]
        plans = {}
        for ram in (64, 128, 256, 512):
            profile = {**base, "ram_total_GB": ram, "ram_free_GB": ram*0.9}
            plan = _plan(settings, profile, "splu", run_dir, log, status, args)
            plans[str(ram)] = plan
            jobs, threads = int(plan["sweep"]["jobs"]), int(plan["sweep"]["threads"])
            for design_id, port in d_selected:
                all_cases.append(_case(settings, "matrix", design_id, port, "splu", threads, jobs,
                                       f"ram{ram}", axis="D", extra={"planner": plan,
                                       "ram_budget_MB": ram*0.9*1024}))
        planning["axes"]["D"] = {"plans": plans,
                                   "expected_ports": [{"design": d, "port": p} for d, p in d_selected]}
    if "E" in axes:
        e_selected = _ports_for(settings, "P20", args.design, run_dir, log, status, args)
        basis_selected = _ports_for(settings, "E4", args.design, run_dir, log, status, args)
        base = _plan(settings, None, "cudss", run_dir, log, status, args)["profile"]
        plans = {}
        for vram in (8, 24, 48):
            gpu = dict((base.get("gpus") or [{"index": 0, "name": "synthetic"}])[0])
            gpu.update(vram_total_MB=vram*1024, vram_free_MB=vram*1024*0.9)
            profile = {**base, "gpus": [gpu]}
            plans[str(vram)] = _plan(settings, profile, "cudss", run_dir, log, status, args)
        planning["axes"]["E"] = {"synthetic_vram_plans": plans,
                                   "expected_ports": [{"design": d, "port": p} for d, p in e_selected]}
        for design_id, port in e_selected:
            all_cases.append(_case(settings, "matrix", design_id, port, "splu",
                                   max(1, args.threads), 1, "gpu-cpu-reference", axis="E",
                                   comparison_role="cpu_reference"))
        for jobs in (1, 2, 4, 8):
            threads = max(1, args.threads)
            for design_id, port in e_selected:
                all_cases.append(_case(settings, "matrix", design_id, port, "cudss", threads, jobs,
                                       f"gpu-jobs{jobs}", axis="E", comparison_role="gpu_candidate"))
        for backend, role in (("splu", "basis_cpu"), ("cudss", "basis_gpu")):
            for design_id, port in basis_selected:
                all_cases.append(_case(settings, "matrix", design_id, port, backend,
                                       max(1, args.threads), 1, "basis-e4", axis="E",
                                       comparison_role=role, operation="basis",
                                       extra={"evidence": "E4 decap basis closure"}))
    planning = _publish_matrix_plan(settings.root, run_dir, planning)
    # Each case records its intended concurrency.  Group by that value so the actual launcher honors it.
    failures = 0
    stopped = False
    status_offset = 0
    group_keys = sorted({(c["identity_request"]["axis"], c["identity_request"]["profile"],
                          c["identity_request"]["backend"], c["identity_request"]["threads"],
                          c["identity_request"]["jobs"], c["identity_request"]["affinity"] or 0)
                         for c in all_cases}, key=str)
    for key in group_keys:
        group = [c for c in all_cases if (c["identity_request"]["axis"],
                 c["identity_request"]["profile"], c["identity_request"]["backend"],
                 c["identity_request"]["threads"], c["identity_request"]["jobs"],
                 c["identity_request"]["affinity"] or 0) == key]
        jobs = key[4]
        nfail, stopped = _run_cases(settings, group, jobs, status, run_dir, log, args,
                                    status_offset=status_offset, status_total=len(all_cases))
        failures += nfail
        status_offset += len(group)
        if stopped: break
    comparison_failed = False
    if not stopped and "B" in axes:
        b_cases = [c for c in all_cases if c["identity_request"]["axis"] == "B"]
        for threads in sorted({c["identity_request"]["threads"] for c in b_cases}):
            reference = [c for c in b_cases if c["identity_request"]["threads"] == threads and
                         c["identity_request"]["jobs"] == 1]
            for jobs in sorted({c["identity_request"]["jobs"] for c in b_cases
                                if c["identity_request"]["threads"] == threads and
                                c["identity_request"]["jobs"] != 1}):
                candidate = [c for c in b_cases if c["identity_request"]["threads"] == threads and
                             c["identity_request"]["jobs"] == jobs]
                result = _compare_case_sets(settings, reference, candidate,
                                            f"matrix-B-t{threads}-j1-vs-j{jobs}", run_dir,
                                            log, status, args)
                comparison_failed |= result.get("pass") is not True
        thread_reference = [c for c in b_cases if c["identity_request"]["threads"] == 1 and
                            c["identity_request"]["jobs"] == 1]
        for threads in sorted({c["identity_request"]["threads"] for c in b_cases if
                               c["identity_request"]["threads"] != 1}):
            candidate = [c for c in b_cases if c["identity_request"]["threads"] == threads and
                         c["identity_request"]["jobs"] == 1]
            result = _compare_case_sets(settings, thread_reference, candidate,
                                        f"matrix-B-cross-thread-t1-vs-t{threads}", run_dir,
                                        log, status, args)
            comparison_failed |= result.get("pass") is not True
    if not stopped and "E" in axes:
        e_cases = [c for c in all_cases if c["identity_request"]["axis"] == "E"]
        comparison_failed |= not _gpu_cases_used_cudss(settings.root, e_cases)
        cpu = [c for c in e_cases if c["identity_request"]["profile"] == "gpu-cpu-reference"]
        for jobs in (1, 2, 4, 8):
            gpu = [c for c in e_cases if c["identity_request"]["profile"] == f"gpu-jobs{jobs}"]
            result = _compare_case_sets(settings, cpu, gpu, f"matrix-E-cpu-vs-gpu-j{jobs}",
                                        run_dir, log, status, args)
            comparison_failed |= result.get("pass") is not True
        basis_cpu = [c for c in e_cases if c["identity_request"]["comparison_role"] == "basis_cpu"]
        basis_gpu = [c for c in e_cases if c["identity_request"]["comparison_role"] == "basis_gpu"]
        result = _compare_case_sets(settings, basis_cpu, basis_gpu, "matrix-E4-basis",
                                    run_dir, log, status, args)
        comparison_failed |= result.get("pass") is not True
    return 130 if stopped else (1 if failures or comparison_failed else 0)


def command_converge(args, status: Status, run_dir: Path, log) -> int:
    settings = load_settings(args.root)
    _measurement_prepare(settings, args, status, run_dir, log)
    selected = _ports_for(settings, args.ports, args.design, run_dir, log, status, args)
    cases = [_case(settings, "converge", d, p, args.backend, args.threads, args.jobs,
                   args.profile, axis="F") for d, p in selected]
    failures, stopped = _run_cases(settings, cases, args.jobs, status, run_dir, log, args)
    return 130 if stopped else (1 if failures else 0)


def command_compare(args, status: Status, run_dir: Path, log) -> int:
    settings = load_settings(args.root)
    left, right = args.left.expanduser().resolve(), args.right.expanduser().resolve()
    out = args.out.expanduser().resolve() if args.out else unique_path(
        args.root / "comparisons" / "comparison.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    result = _call_worker(settings, {"operation": "compare", "left": str(left),
                                     "right": str(right), "out": str(out)}, run_dir,
                          "compare", log, status, args.stop_file, args.stop_now)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("pass") is True else 1


def command_report(args, status: Status, run_dir: Path, log) -> int:
    settings = load_settings(args.root)
    result = _call_worker(settings, {"operation": "report", "root": str(args.root)}, run_dir,
                          "report", log, status, args.stop_file, args.stop_now)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def batch_plan(stage: str = "all") -> list[dict[str, Any]]:
    """Return a fresh, public description of the sequential validation campaign."""
    plan = [
        {"id": "env", "label": "환경 확인", "argv": ["env"]},
        {"id": "gates", "label": "필수 게이트", "argv": ["gates"]},
        {"id": "baseline-p9", "label": "기준선 P9 · CPU / GPU 2회", "argv": ["baseline", "--ports", "P9"]},
        {"id": "baseline-p92", "label": "기준선 P92 · CPU / GPU 2회", "argv": ["baseline", "--ports", "P92"]},
        {"id": "matrix-b", "label": "B · 스레드 × 프로세스 · P9", "argv": ["matrix", "--axis", "B", "--ports", "P9"]},
        {"id": "matrix-c", "label": "C · 논리 CPU 8/16/32/64 · P92", "argv": ["matrix", "--axis", "C", "--ports", "P92"]},
        {"id": "matrix-d", "label": "D · RAM 64/128/256/512 GB 플래너 · P92", "argv": ["matrix", "--axis", "D", "--ports", "P92"]},
        {"id": "matrix-e", "label": "E · GPU 동시성 / VRAM / 기저 · P20", "argv": ["matrix", "--axis", "E", "--ports", "P20"]},
        {"id": "converge", "label": "F · 수렴성 fine · P92", "argv": ["converge", "--ports", "P92"]},
        {"id": "report", "label": "보고서 생성", "argv": ["report"]},
    ]
    choices = {"all", "env", "gates", "baseline", "matrix", "converge", "report"}
    if stage not in choices:
        raise ValueError(f"unknown batch stage {stage!r}")
    if stage == "all":
        return plan
    return [step for step in plan if step["argv"][0] == stage]


def command_batch(args, status: Status, run_dir: Path, log) -> int:
    plan = batch_plan(args.stage)
    steps = [{**step, "argv": list(step["argv"]), "state": "pending", "exit_code": None,
              "run_dir": None, "error": None} for step in plan]
    batch = {"stage": args.stage, "completed": 0, "total": len(steps),
             "current_id": None, "steps": steps}
    started_at = utc_now()

    def persist(*, running: bool, exit_code: int | None) -> None:
        batch_error = next((step["error"] for step in steps
                            if step["state"] in {"failed", "stopped"} and step["error"]), None)
        status.update(force=True, batch=batch, error=batch_error)
        atomic_json(args.root / "batch_status.json", {
            "schema_version": 1, "version": VERSION, "updated_at": utc_now(),
            "running": running, "exit_code": exit_code, "error": batch_error,
            "batch_run_dir": str(run_dir), "batch": batch,
        })

    persist(running=True, exit_code=None)
    final_code = 0
    for index, step in enumerate(steps):
        mode = _stop_mode(args.stop_file, args.stop_now)
        if mode:
            final_code = 130
            step.update(state="stopped", exit_code=130,
                        error=f"{mode} stop requested before step launch")
            batch["current_id"] = step["id"]
            persist(running=True, exit_code=None)
            break

        step.update(state="running", error=None)
        batch["current_id"] = step["id"]
        status.value.update(batch=batch, current_case=None, completed=0, remaining=0,
                            total=0, comparison=None, error=None)
        persist(running=True, exit_code=None)
        print(f"batch step {step['id']} started: {step['label']}", file=log)
        child_args = None
        child_run_dir = None
        child_log = None
        code = 1
        error = None
        traceback_text = None
        try:
            child_args = build_parser().parse_args(step["argv"])
            child_args.root = args.root
            child_args.stop_file = args.stop_file
            child_args.stop_now = args.stop_now
            child_run_dir, child_log = _new_run(args.root, f"batch-{step['id']}")
            step["run_dir"] = str(child_run_dir)
            persist(running=True, exit_code=None)
            code = COMMANDS[child_args.command](child_args, status, child_run_dir, child_log)
            if child_args.command == "env" and code == 0:
                env_record = read_json(args.root / "env.json", {})
                if env_record.get("ok") is not True:
                    code = 1
                    error = "env.json did not report ok=true"
            if code == 0 and index == len(steps) - 1:
                mode = _stop_mode(args.stop_file, args.stop_now)
                if mode:
                    code = 130
                    error = f"{mode} stop requested before batch completion"
        except KeyboardInterrupt as exc:
            code = 130
            error = str(exc) or "interrupted"
        except Exception as exc:
            code = 1
            error = f"{type(exc).__name__}: {exc}"
            traceback_text = traceback.format_exc()
        finally:
            if error is None and code != 0:
                error = f"step returned exit code {code}"
            if child_run_dir is None:
                child_run_dir = unique_path(run_dir / "failed-steps" / safe_name(step["id"]))
                child_run_dir.mkdir(parents=True)
                step["run_dir"] = str(child_run_dir)
            if child_log is None:
                child_log = (child_run_dir / "log.txt").open("a", encoding="utf-8", buffering=1)
            if traceback_text:
                child_log.write(traceback_text)
            if not (child_run_dir / "run_summary.json").exists():
                _write_run_summary(child_run_dir, {
                    "command": child_args.command if child_args else step["argv"][0],
                    "batch_step_id": step["id"],
                    "finished_at": utc_now(), "exit_code": code,
                    "comparison": status.value.get("comparison"), "error": error,
                    "stopped": code == 130,
                })
            child_log.close()

        step["exit_code"] = code
        step["error"] = error
        status.value.update(current_case=None, completed=0, remaining=0, total=0)
        if code == 0:
            step["state"] = "completed"
            batch["completed"] += 1
            batch["current_id"] = None
            persist(running=True, exit_code=None)
            continue
        step["state"] = "stopped" if code == 130 else "failed"
        final_code = 130 if code == 130 else 1
        persist(running=True, exit_code=None)
        print(f"batch step {step['id']} {step['state']}: {error}", file=log)
        break

    batch["current_id"] = None
    finished_at = utc_now()
    summary_path = run_dir / "batch_summary.json"
    if summary_path.exists():
        raise RuntimeError(f"immutable batch summary already exists: {summary_path}")
    atomic_json(summary_path, {"schema_version": 1, "version": VERSION,
                               "started_at": started_at, "finished_at": finished_at,
                               "exit_code": final_code,
                               "error": next((step["error"] for step in steps
                                              if step["state"] in {"failed", "stopped"}), None),
                               "batch": batch})
    persist(running=False, exit_code=final_code)
    return final_code


def self_check() -> int:
    import tempfile
    checks = {}
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "input.spd"; source.write_bytes(b"synthetic")
        request = {"design": "synthetic", "port": "P1", "threads": 2}
        identity, hashes = input_identity(request, {"spd": source})
        checks["identity_stable"] = identity == input_identity(request, {"spd": source})[0]
        receipts = root / "receipts"; receipts.mkdir()
        path = receipt_path(receipts, "synthetic/P1", identity)
        atomic_json(path, {"numerics_id": "self-check", "freq": [1.0], "Z_re": [1.0],
                           "Z_im": [0.0], "study": {"schema_version": 1,
                           "input_identity": identity, "finished_at": utc_now(),
                           "source_spd_sha256": hashes["spd"], "command": "baseline"}})
        checks["resume_exact"] = resumable_receipt(receipts, identity) == path
        checks["resume_rejects_other"] = resumable_receipt(receipts, "0"*64) is None
        stop = root / "stop"; stop.write_text("now\n", encoding="utf-8")
        checks["stop_now"] = _stop_mode(stop, False) == "now"
        stop.write_text("graceful\n", encoding="utf-8")
        checks["stop_graceful"] = _stop_mode(stop, False) == "graceful"
        checks["hash_recorded"] = hashes["spd"] == sha256_file(source)
        vector = {"numerics_id": "n", "freq": [1.0], "Z_re": [2.0], "Z_im": [1.0]}
        checks["comparison_equal"] = compare_receipt_vectors(vector, dict(vector))["bit_equal"]
        changed = {**vector, "Z_re": [2.25]}
        checks["comparison_detects_delta"] = compare_receipt_vectors(vector, changed)[
            "max_relative_error"] > 0
        with RunLock(root, "self-check"):
            checks["exclusive_lock"] = True
            try:
                with RunLock(root, "collision"):
                    pass
            except RuntimeError:
                checks["exclusive_lock"] = True
            else:
                checks["exclusive_lock"] = False
    result = {"ok": all(checks.values()), "checks": checks}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def _common(parser, *, defaults=True):
    kw = {} if defaults else {"default": argparse.SUPPRESS}
    parser.add_argument("--root", type=Path, **({"default": default_root()} if defaults else kw))
    parser.add_argument("--stop-file", type=Path,
                        help="Stop request file; graceful stops prevent new launches and wait for in-flight jobs",
                        **({"default": None} if defaults else kw))
    parser.add_argument("--stop-now", action="store_true",
                        help="Treat a stop-file request as immediate process-tree termination",
                        **({"default": False} if defaults else kw))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _common(parser)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command")
    for name in ("env", "gates", "report"):
        child = sub.add_parser(name); _common(child, defaults=False)
    batch = sub.add_parser("batch"); _common(batch, defaults=False)
    batch.add_argument("--stage", choices=("all", "env", "gates", "baseline", "matrix",
                                            "converge", "report"), default="all")
    def measurement(name):
        child = sub.add_parser(name); _common(child, defaults=False)
        child.add_argument("--ports", choices=("P9", "P20", "P92"), default="P9" if name == "baseline" else "P20")
        child.add_argument("--backend", choices=("splu", "auto", "cudss"), default=None if name == "baseline" else "splu")
        child.add_argument("--jobs", type=int, default=1)
        child.add_argument("--threads", type=int, default=4)
        child.add_argument("--profile", default="manual")
        child.add_argument("--design")
        return child
    measurement("baseline")
    matrix = measurement("matrix"); matrix.add_argument("--axis", choices=("B", "C", "D", "E", "all"), default="all")
    converge = measurement("converge"); converge.set_defaults(ports="P92", backend="auto")
    compare = sub.add_parser("compare"); _common(compare, defaults=False)
    compare.add_argument("left", type=Path); compare.add_argument("right", type=Path)
    compare.add_argument("--out", type=Path)
    return parser


COMMANDS = {"env": command_env, "gates": command_gates, "baseline": command_baseline,
            "matrix": command_matrix, "converge": command_converge,
            "compare": command_compare, "report": command_report, "batch": command_batch}


def main(argv=None) -> int:
    if getattr(sys, "frozen", False) and os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    args = build_parser().parse_args(argv)
    if args.self_check:
        return self_check()
    if not args.command:
        build_parser().print_help()
        return 2
    args.root = args.root.expanduser().resolve()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.stop_file is not None:
        args.stop_file = args.stop_file.expanduser().resolve()
    code = 1
    error = None
    stopped = False
    try:
        with RunLock(args.root, args.command):
            run_dir, log = _new_run(args.root, args.command)
            status = Status(args.root, args.command)
            try:
                code = COMMANDS[args.command](args, status, run_dir, log)
                stopped = code == 130
                if code and args.command == "batch":
                    error = status.value.get("error")
            except KeyboardInterrupt as exc:
                code, stopped, error = 130, True, str(exc) or "interrupted"
            except Exception as exc:
                code, error = 1, f"{type(exc).__name__}: {exc}"
                traceback.print_exc(file=log)
                print(error, file=sys.stderr)
            finally:
                if not (run_dir / "run_summary.json").exists():
                    _write_run_summary(run_dir, {"command": args.command, "finished_at": utc_now(),
                                                 "exit_code": code,
                                                 "comparison": status.value.get("comparison"),
                                                 "error": error, "stopped": stopped})
                status.finish(code, error=error, stopped=stopped)
                log.close()
    except Exception as exc:
        code, error = 1, f"{type(exc).__name__}: {exc}"
        print(error, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
