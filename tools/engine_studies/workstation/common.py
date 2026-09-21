"""Small, dependency-free helpers shared by the workstation validation tools."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
    except BaseException:
        try:
            os.unlink(raw)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def safe_name(value: str, limit: int = 80) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-.") or "case"
    return value[:limit]


def unique_path(path: Path) -> Path:
    """Return a non-existing path without overwriting any previous evidence."""
    path = Path(path)
    if not path.exists():
        return path
    for index in range(1, 100_000):
        candidate = path.with_name(f"{path.stem}-{index:03d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate a unique path beside {path}")


@dataclass(frozen=True)
class DesignSpec:
    design_id: str
    spd: Path
    reference: Path | None
    family: str
    ports: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    root: Path
    engine_python: Path
    engine_root: Path
    data_dir: Path
    laptop_receipts: Path | None = None
    laptop_freeze: Path | None = None
    designs: dict[str, DesignSpec] = field(default_factory=dict)


DEFAULT_DESIGNS = {
    "260729": {
        "spd": "S4LB002-2Para_260729_1_injected.spd",
        "reference": "S4LB002_260729_Zdiag.npz",
        "family": "package",
    },
    "260804": {
        "spd": "S4LB002-2Para_260804_1_injected.spd",
        "reference": "S4LB002_260804_Zdiag.npz",
        "family": "package",
    },
    "s5m6585": {
        "spd": "s5m6585_32p_260414_length3_1.spd",
        "reference": "s5m6585_Zdiag.npz",
        "family": "pcb",
    },
}


def _resolved(base: Path, raw: str | os.PathLike[str] | None) -> Path | None:
    if raw in (None, ""):
        return None
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def find_data_file(data_dir: Path, raw: str | os.PathLike[str] | None) -> Path | None:
    if raw in (None, ""):
        return None
    path = Path(raw)
    candidates = [path] if path.is_absolute() else [data_dir / path, data_dir / "analysis" / path]
    return next((p.resolve() for p in candidates if p.exists()), candidates[0].resolve())


def load_settings(root: Path, *, require_paths: bool = True) -> Settings:
    root = Path(root).expanduser().resolve()
    config_path = root / "config.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"missing runtime settings: {config_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid runtime settings {config_path}: {exc}") from exc

    required = [key for key in ("engine_python", "engine_root", "data_dir") if not raw.get(key)]
    if required:
        raise ValueError(f"{config_path}: missing required keys: {', '.join(required)}")
    engine_python = _resolved(root, raw["engine_python"])
    engine_root = _resolved(root, raw["engine_root"])
    data_dir = _resolved(root, raw["data_dir"])
    assert engine_python and engine_root and data_dir
    if require_paths:
        errors = []
        if not engine_python.is_file():
            errors.append(f"engine_python is not a file: {engine_python}")
        if not engine_root.is_dir():
            errors.append(f"engine_root is not a directory: {engine_root}")
        if not data_dir.is_dir():
            errors.append(f"data_dir is not a directory: {data_dir}")
        if errors:
            raise ValueError("; ".join(errors))

    merged = dict(DEFAULT_DESIGNS)
    custom = raw.get("designs", {})
    if custom is not None and not isinstance(custom, dict):
        raise ValueError(f"{config_path}: designs must be an object")
    merged.update(custom or {})
    designs: dict[str, DesignSpec] = {}
    for design_id, item in merged.items():
        if not isinstance(item, dict) or not item.get("spd"):
            raise ValueError(f"{config_path}: design {design_id!r} needs an spd path")
        family = item.get("family", "package")
        if family not in ("package", "pcb"):
            raise ValueError(f"{config_path}: design {design_id!r} family must be package or pcb")
        designs[str(design_id)] = DesignSpec(
            str(design_id),
            find_data_file(data_dir, item["spd"]),
            find_data_file(data_dir, item.get("reference")),
            family,
            tuple(str(p) for p in item.get("ports", ())),
        )
    return Settings(
        root=root,
        engine_python=engine_python,
        engine_root=engine_root,
        data_dir=data_dir,
        laptop_receipts=_resolved(root, raw.get("laptop_receipts")),
        laptop_freeze=_resolved(root, raw.get("laptop_freeze")),
        designs=designs,
    )


def input_identity(request: dict[str, Any], files: dict[str, Path | None]) -> tuple[str, dict[str, str | None]]:
    hashes = {name: sha256_file(path) if path and path.is_file() else None for name, path in files.items()}
    return sha256_bytes(canonical_json({"request": request, "files": hashes})), hashes


def resumable_receipt(receipts_dir: Path, identity: str) -> Path | None:
    for path in sorted(Path(receipts_dir).glob("*.json")):
        try:
            value = read_json(path, {})
            if value.get("study", {}).get("input_identity") == identity and _complete_receipt(value):
                return path
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return None


def _complete_receipt(value: dict) -> bool:
    study = value.get("study")
    if not isinstance(study, dict) or study.get("schema_version") != SCHEMA_VERSION:
        return False
    if not study.get("finished_at") or not study.get("source_spd_sha256"):
        return False
    if study.get("command") == "converge":
        convergence = value.get("convergence", {})
        if not (convergence.get("mesh_pair") == "fine" and
                isinstance(convergence.get("converged"), bool)
                and all(isinstance(convergence.get(key), (int, float)) and math.isfinite(convergence[key])
                        for key in ("frequency_rms_delta_db", "frequency_max_delta_db"))):
            return False
    freq, real, imag = value.get("freq"), value.get("Z_re"), value.get("Z_im")
    if not value.get("numerics_id") or not all(isinstance(v, list) for v in (freq, real, imag)):
        return False
    if not freq or not (len(freq) == len(real) == len(imag)):
        return False
    try:
        return (all(math.isfinite(float(x)) for values in (freq, real, imag) for x in values)
                and all(float(a) < float(b) for a, b in zip(freq, freq[1:])))
    except (TypeError, ValueError, OverflowError):
        return False


def receipt_path(receipts_dir: Path, case_label: str, identity: str) -> Path:
    return unique_path(Path(receipts_dir) / f"{safe_name(case_label)}-{identity[:12]}.json")


def compare_receipt_vectors(left: dict, right: dict) -> dict:
    """Dependency-free strict comparison used by the packaged self-check."""
    if left.get("numerics_id") != right.get("numerics_id"):
        raise ValueError("numerics_id mismatch")
    if left.get("freq") != right.get("freq"):
        raise ValueError("frequency grid mismatch")
    keys = ("Z_re", "Z_im")
    if any(len(left.get(k, ())) != len(right.get(k, ())) for k in keys):
        raise ValueError("impedance vector length mismatch")
    maximum = 0.0
    bit_equal = True
    for ar, ai, br, bi in zip(left.get("Z_re", ()), left.get("Z_im", ()),
                              right.get("Z_re", ()), right.get("Z_im", ())):
        a, b = complex(ar, ai), complex(br, bi)
        bit_equal &= ar == br and ai == bi
        denominator = abs(a)
        if denominator == 0:
            relative = 0.0 if a == b else float("inf")
        else:
            relative = abs(b - a) / denominator
        maximum = max(maximum, relative)
    return {"bit_equal": bit_equal, "max_relative_error": maximum}


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(process, ctypes.byref(code))) \
                and code.value == 259
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class RunLock:
    """One writer for status.json across GUI, agent, and direct CLI starts."""

    def __init__(self, root: Path, command: str):
        self.path = Path(root) / "run.lock"
        self.command = command
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                if handle.read(1) == b"":
                    handle.seek(0); handle.write(b"0"); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError(f"another validation run owns {self.path}") from exc
        record = canonical_json({"pid": os.getpid(), "command": self.command,
                                 "started_at": utc_now()}) + b"\n"
        handle.seek(0); handle.truncate(); handle.write(record); handle.flush(); os.fsync(handle.fileno())
        handle.seek(0)
        self.handle = handle
        return self

    def __exit__(self, exc_type, exc, tb):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
