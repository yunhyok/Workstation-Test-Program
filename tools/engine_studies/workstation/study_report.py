"""Strict receipt comparison and reporting for the W15 workstation study.

This module deliberately treats missing evidence as an incomplete result.  In
particular, files that cannot be parsed, receipts that cannot be paired, a
changed frequency grid, or a changed ``numerics_id`` can never produce PASS.
The module has no dependency on engine internals and is safe to import from
``ws_validate``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import numpy as np
except ImportError as exc:  # The engine itself requires NumPy.
    raise RuntimeError("study_report requires the engine's NumPy dependency") from exc


REPORT_SCHEMA_VERSION = 1
LEGACY_MAP_NAMES = {
    ".legacy_receipt_map.json",
    "legacy_receipt_map.json",
    "legacy_receipts.json",
}
NON_RECEIPT_NAMES = LEGACY_MAP_NAMES | {"summary.json", "status.json", "env.json"}

CPU_LIMITS = {"package": 1e-9, "pcb": 2e-9}
GPU_LIMITS = {"package": 1e-8, "pcb_full": 1e-6, "pcb_ge_1mhz": 1e-7}


class ReceiptError(ValueError):
    """A receipt lacks evidence needed for a numerical comparison."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot read JSON: {exc}") from exc


def _load_legacy_map(directory: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Load an explicit, content-addressed map for frozen research receipts.

    Old exp28/exp30 JSON files predate receipt v1 and do not contain a
    ``numerics_id``.  Accepting one by filename or by a guessed engine version
    would turn an unknown comparison into a false PASS.  A map must therefore
    name each file, assert ``verified: true``, give its complete SHA-256 and the
    numerics ID established by the reproduction test.

    Supported shape::

        {"schema_version": 1, "verified": true,
         "receipts": {"old.json": {"sha256": "...", "numerics_id": "...",
                                      "design": "design_a", "family": "package",
                                      "backend": "splu", "threads": 1}}}
    """
    errors: list[str] = []
    found = [directory / name for name in LEGACY_MAP_NAMES if (directory / name).is_file()]
    if not found:
        return {}, errors
    if len(found) > 1:
        return {}, ["multiple legacy receipt maps found; keep exactly one"]
    path = found[0]
    try:
        raw = _read_json(path)
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ReceiptError("legacy map schema_version must be 1")
        rows = raw.get("receipts")
        if not isinstance(rows, dict):
            raise ReceiptError("legacy map receipts must be an object")
        top_verified = raw.get("verified") is True
        result: dict[str, dict[str, Any]] = {}
        for name, value in rows.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                raise ReceiptError("legacy map entries must map filenames to objects")
            if not (top_verified or value.get("verified") is True):
                raise ReceiptError(f"legacy map entry {name!r} is not explicitly verified")
            expected_hash = value.get("sha256", value.get("receipt_sha256"))
            numerics_id = value.get("numerics_id")
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
                raise ReceiptError(f"legacy map entry {name!r} needs a complete receipt sha256")
            if not isinstance(numerics_id, str) or not numerics_id.strip():
                raise ReceiptError(f"legacy map entry {name!r} needs numerics_id")
            result[Path(name).as_posix()] = dict(value)
        return result, errors
    except ReceiptError as exc:
        return {}, [f"{path.name}: {exc}"]


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _study(rec: dict[str, Any]) -> dict[str, Any]:
    value = rec.get("study")
    return value if isinstance(value, dict) else {}


def _design(rec: dict[str, Any], mapped: dict[str, Any]) -> str | None:
    study = _study(rec)
    for value in (mapped.get("design"), study.get("design"), rec.get("design"), rec.get("tag")):
        if isinstance(value, dict):
            value = value.get("id", value.get("name"))
        text = _text(value)
        if text:
            return text
    return None


def _family(rec: dict[str, Any], mapped: dict[str, Any], design: str | None) -> str | None:
    study = _study(rec)
    for value in (mapped.get("family"), study.get("family"), rec.get("family"), rec.get("design_class")):
        text = _text(value)
        if text and text.casefold() in {"package", "pcb", "unknown"}:
            return text.casefold()
    return None


def _backend(rec: dict[str, Any], mapped: dict[str, Any]) -> str | None:
    study = _study(rec)
    actual = study.get("actual_solvers")
    if isinstance(actual, list):
        names = {str(item).casefold() for item in actual if _text(item)}
        if len(names) == 1:
            return _canonical_backend(next(iter(names)))
    basis = rec.get("decap_basis")
    if isinstance(basis, dict) and isinstance(basis.get("backend"), dict):
        requested = _text(basis["backend"].get("solver"))
        if requested in {"cudss", "auto"}:
            return "cudss" if basis["backend"].get("device") is not None else "splu"
        if requested:
            return _canonical_backend(requested)
    # The engine receipt records the backend that actually ran.  The study
    # block can say "auto", which is a request and may have fallen back.
    value = mapped.get("backend", rec.get("backend", study.get("backend")))
    if isinstance(value, dict):
        value = value.get("solver")
    text = _text(value)
    if not text:
        return None
    return _canonical_backend(text)


def _canonical_backend(text: str) -> str:
    folded = text.casefold()
    if folded in {"cpu", "scipy", "superlu"}:
        return "splu"
    if folded in {"gpu", "cuda"}:
        return "cudss"
    return folded


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def _threads(rec: dict[str, Any], mapped: dict[str, Any]) -> int | None:
    study = _study(rec)
    return _positive_int(mapped.get("threads", study.get("threads", rec.get("threads"))))


def _jobs(rec: dict[str, Any], mapped: dict[str, Any]) -> int | None:
    study = _study(rec)
    return _positive_int(mapped.get("jobs", study.get("jobs", rec.get("jobs"))))


def _as_float_array(rec: dict[str, Any], key: str) -> np.ndarray:
    value = rec.get(key)
    if not isinstance(value, list) or not value:
        raise ReceiptError(f"{key} must be a non-empty JSON array")
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReceiptError(f"{key} must contain numbers") from exc
    if array.ndim != 1 or not np.all(np.isfinite(array)):
        raise ReceiptError(f"{key} must be a finite one-dimensional array")
    return array


def _normalise_receipt(path: Path, base: Path, raw: Any, mapping: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReceiptError("receipt root must be an object")
    rel = path.relative_to(base).as_posix()
    mapped = mapping.get(rel, mapping.get(path.name, {}))
    is_legacy = "numerics_id" not in raw or "receipt_version" not in raw
    if is_legacy:
        if not mapped:
            raise ReceiptError("legacy receipt has no explicit verified mapping")
        if _sha256(path).casefold() != str(mapped.get("sha256", mapped.get("receipt_sha256"))).casefold():
            raise ReceiptError("legacy receipt SHA-256 does not match its verified mapping")
    numerics_id = _text(raw.get("numerics_id")) or _text(mapped.get("numerics_id"))
    if not numerics_id:
        raise ReceiptError("missing numerics_id")
    design = _design(raw, mapped)
    port = _text(mapped.get("port")) or _text(_study(raw).get("port")) or _text(raw.get("port"))
    family = _family(raw, mapped, design)
    if not design or not port:
        raise ReceiptError("missing design or port identity")
    freq = _as_float_array(raw, "freq")
    z_re = _as_float_array(raw, "Z_re")
    z_im = _as_float_array(raw, "Z_im")
    if not (freq.size == z_re.size == z_im.size):
        raise ReceiptError("freq, Z_re, and Z_im lengths differ")
    if np.any(np.diff(freq) <= 0):
        raise ReceiptError("freq must be strictly increasing")
    study = _study(raw)
    return {
        "path": str(path),
        "relative_path": rel,
        "raw": raw,
        "legacy": is_legacy,
        "numerics_id": numerics_id,
        "design": design,
        "port": port,
        "family": family,
        "backend": _backend(raw, mapped),
        "threads": _threads(raw, mapped),
        "jobs": _jobs(raw, mapped),
        "profile": _text(mapped.get("profile")) or _text(study.get("profile")),
        "axis": _text(study.get("axis")),
        "command": _text(study.get("command")),
        "repeat": int(study.get("repeat", 0)) if isinstance(study.get("repeat", 0), int) else None,
        "case_id": _text(study.get("case_id")),
        "input_identity": _text(study.get("input_identity")),
        "gate_fingerprint": _text(study.get("gate_fingerprint")),
        "source_spd_sha256": _text(study.get("source_spd_sha256")) or _text(raw.get("spd_sha256")),
        "source_ref_sha256": _text(study.get("source_ref_sha256")),
        "comparison_group": _text(study.get("comparison_group")),
        "comparison_role": _text(study.get("comparison_role")),
        "requested_backend": _canonical_backend(str(study.get("backend"))) if _text(study.get("backend")) else None,
        "freq": freq,
        "z": z_re + 1j * z_im,
    }


def _receipt_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise FileNotFoundError(f"receipt directory does not exist: {directory}")
    return sorted(
        path for path in directory.rglob("*.json")
        if path.name not in NON_RECEIPT_NAMES and "archives" not in path.parts
    )


def _load_receipts(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    mapping, map_errors = _load_legacy_map(directory)
    malformed = [{"path": str(directory), "error": error} for error in map_errors]
    receipts: list[dict[str, Any]] = []
    for path in _receipt_files(directory):
        try:
            receipts.append(_normalise_receipt(path, directory, _read_json(path), mapping))
        except ReceiptError as exc:
            malformed.append({"path": str(path), "error": str(exc)})
    return receipts, malformed


def _pair_receipts(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair conservatively; ambiguous rows remain unmatched."""
    left_remaining = list(left)
    right_remaining = list(right)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def consume(key) -> None:
        nonlocal left_remaining, right_remaining
        lg: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        rg: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for row in left_remaining:
            value = key(row)
            if value is not None:
                lg[value].append(row)
        for row in right_remaining:
            value = key(row)
            if value is not None:
                rg[value].append(row)
        used_l: set[int] = set()
        used_r: set[int] = set()
        for value in sorted(set(lg).intersection(rg), key=str):
            if len(lg[value]) == len(rg[value]) == 1:
                a, b = lg[value][0], rg[value][0]
                pairs.append((a, b)); used_l.add(id(a)); used_r.add(id(b))
        left_remaining = [row for row in left_remaining if id(row) not in used_l]
        right_remaining = [row for row in right_remaining if id(row) not in used_r]

    # Baseline has one frozen laptop receipt and several explicitly labelled
    # workstation observations (CPU, GPU, GPU repeat) for the same port.  The
    # roles make this fan-out unambiguous; without them, many-to-one data stays
    # unmatched rather than being paired by file order.
    lg: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    rg: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in left_remaining:
        lg[(row["design"], row["port"])].append(row)
    for row in right_remaining:
        rg[(row["design"], row["port"])].append(row)
    used_l: set[int] = set(); used_r: set[int] = set()
    for identity in sorted(set(lg).intersection(rg)):
        lgroup, rgroup = lg[identity], rg[identity]
        if len(lgroup) == 1 and len(rgroup) > 1 and all(row["comparison_role"] for row in rgroup):
            pairs.extend((lgroup[0], row) for row in rgroup)
            used_l.add(id(lgroup[0])); used_r.update(id(row) for row in rgroup)
        elif len(rgroup) == 1 and len(lgroup) > 1 and all(row["comparison_role"] for row in lgroup):
            pairs.extend((row, rgroup[0]) for row in lgroup)
            used_r.add(id(rgroup[0])); used_l.update(id(row) for row in lgroup)
    left_remaining = [row for row in left_remaining if id(row) not in used_l]
    right_remaining = [row for row in right_remaining if id(row) not in used_r]

    consume(lambda r: ("case", r["case_id"]) if r["case_id"] else None)
    consume(lambda r: ("input", r["input_identity"]) if r["input_identity"] else None)
    consume(lambda r: ("config", r["design"], r["port"], r["backend"], r["threads"], r["jobs"], r["profile"]))
    consume(lambda r: ("logical", r["design"], r["port"]))
    return pairs, left_remaining, right_remaining


def _bits_equal(a: np.ndarray, b: np.ndarray) -> bool:
    return a.dtype == b.dtype and a.shape == b.shape and bool(np.array_equal(a.view(np.uint64), b.view(np.uint64)))


def _relative_error(observed: np.ndarray, reference: np.ndarray) -> np.ndarray:
    delta = np.abs(observed - reference)
    denom = np.abs(reference)
    result = np.full(delta.shape, np.inf, dtype=np.float64)
    nonzero = denom > 0
    result[nonzero] = delta[nonzero] / denom[nonzero]
    result[(~nonzero) & (delta == 0)] = 0.0
    return result


def _compare_pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    identity_match = left["design"] == right["design"] and left["port"] == right["port"]
    if not identity_match:
        errors.append("design/port identity mismatch")
    for field, label in (("source_spd_sha256", "source SPD"), ("source_ref_sha256", "source reference")):
        if left[field] and right[field] and left[field] != right[field]:
            errors.append(f"{label} SHA-256 mismatch")
        elif not (left[field] and right[field]) and not (left["legacy"] or right["legacy"]):
            errors.append(f"{label} SHA-256 is missing")
    for side, row in (("left", left), ("right", right)):
        if row.get("requested_backend") in {"cudss", "auto"} and row["backend"] != "cudss":
            errors.append(f"{side} requested GPU backend but actual solver evidence is not cudss")
    frequency_identical = _bits_equal(left["freq"], right["freq"])
    if not frequency_identical:
        errors.append("frequency grids are not bit-identical")
    numerics_match = left["numerics_id"] == right["numerics_id"]
    if not numerics_match:
        errors.append("numerics_id mismatch")
    family = left["family"] if left["family"] == right["family"] else None
    if family not in {"package", "pcb", "unknown"}:
        errors.append("design family is missing or inconsistent")

    backend_values = {left["backend"], right["backend"]}
    gpu = bool(backend_values.intersection({"cudss", "auto"}))
    if None in backend_values:
        errors.append("backend is missing")
    roles = {str(left.get("comparison_role") or "").casefold(),
             str(right.get("comparison_role") or "").casefold()}
    basis = any("basis" in role for role in roles)
    category = "basis_gpu" if basis and gpu else ("basis_cpu" if basis else ("gpu" if gpu else "cpu"))
    same_threads = left["threads"] is not None and left["threads"] == right["threads"]
    if not gpu and (left["threads"] is None or right["threads"] is None):
        errors.append("CPU thread count is missing")
    bit_required = (not gpu and same_threads
                    and (left.get("axis") == "B" or right.get("axis") == "B"))
    bit_equal = _bits_equal(left["z"].real.copy(), right["z"].real.copy()) and _bits_equal(
        left["z"].imag.copy(), right["z"].imag.copy()
    ) if left["z"].shape == right["z"].shape else False

    metrics: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []
    if frequency_identical and left["z"].shape == right["z"].shape:
        rel = _relative_error(right["z"], left["z"])
        maximum = float(np.max(rel))
        metrics["max_relative_error"] = maximum
        if category in {"cpu", "basis_cpu"} and family in {*CPU_LIMITS, "unknown"}:
            limit = 1e-9 if category == "basis_cpu" else CPU_LIMITS.get(family, 1e-9)
            check_name = ("E4 CPU decap basis" if category == "basis_cpu" else
                          ("E3 CPU unclassified (strict)" if family == "unknown"
                           else f"E3 CPU {family}"))
            checks.append({"name": check_name, "value": maximum, "limit": limit,
                           "pass": bool(maximum <= limit)})
        elif category == "basis_gpu":
            limit = 1e-5
            checks.append({"name": "E4 GPU decap basis", "value": maximum, "limit": limit,
                           "pass": bool(maximum <= limit)})
        elif category == "gpu" and family == "package":
            limit = GPU_LIMITS["package"]
            checks.append({"name": "E3 GPU package", "value": maximum, "limit": limit,
                           "pass": bool(maximum <= limit)})
        elif category == "gpu" and family == "pcb":
            full_limit = GPU_LIMITS["pcb_full"]
            checks.append({"name": "E3 GPU PCB full", "value": maximum, "limit": full_limit,
                           "pass": bool(maximum <= full_limit)})
            mask = left["freq"] >= 1e6
            if not bool(np.any(mask)):
                errors.append("PCB grid has no frequency at or above 1 MHz")
            else:
                band = float(np.max(rel[mask]))
                metrics["max_relative_error_ge_1MHz"] = band
                band_limit = GPU_LIMITS["pcb_ge_1mhz"]
                checks.append({"name": "E3 GPU PCB >=1MHz", "value": band, "limit": band_limit,
                               "pass": bool(band <= band_limit)})
        elif category == "gpu" and family == "unknown":
            limit = GPU_LIMITS["package"]
            checks.append({"name": "E3 GPU unclassified (strict)", "value": maximum,
                           "limit": limit, "pass": bool(maximum <= limit)})
    else:
        errors.append("impedance arrays cannot be aligned")

    if bit_required:
        checks.append({"name": "same-thread CPU bit equality", "value": bit_equal,
                       "expected": True, "pass": bit_equal})
    passed = not errors and bool(checks) and all(check["pass"] for check in checks)
    return {
        "identity": {"design": left["design"], "port": left["port"]},
        "left": left["relative_path"],
        "right": right["relative_path"],
        "family": family,
        "category": category,
        "backend_left": left["backend"],
        "backend_right": right["backend"],
        "threads_left": left["threads"],
        "threads_right": right["threads"],
        "axis_left": left.get("axis"), "axis_right": right.get("axis"),
        "command_left": left.get("command"), "command_right": right.get("command"),
        "role_left": left.get("comparison_role"), "role_right": right.get("comparison_role"),
        "repeat_left": left.get("repeat"), "repeat_right": right.get("repeat"),
        "frequency_identical": frequency_identical,
        "identity_match": identity_match,
        "numerics_id_match": numerics_match,
        "same_thread_bit_equality_required": bit_required,
        "bit_equal": bit_equal,
        "metrics": metrics,
        "checks": checks,
        "errors": errors,
        "pass": passed,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def compare_directories(left: Path, right: Path, out: Path | None = None) -> dict[str, Any]:
    """Compare two receipt directories under the frozen E3 rules.

    ``left`` is the numerical reference side (normally laptop CPU receipts).
    The returned ``pass`` is false for zero pairs, every malformed file, and
    every unmatched receipt.  When ``out`` is supplied it must not already
    exist; comparison evidence is never overwritten.
    """
    left = Path(left); right = Path(right)
    lrows, lbad = _load_receipts(left)
    rrows, rbad = _load_receipts(right)
    paired, lunmatched, runmatched = _pair_receipts(lrows, rrows)
    results = [_compare_pair(a, b) for a, b in paired]
    repeats = []
    right_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rrows:
        right_groups[(row["design"], row["port"])].append(row)
    for group in right_groups.values():
        candidates = [row for row in group if row.get("comparison_role") == "gpu_candidate"]
        reruns = [row for row in group if row.get("comparison_role") == "gpu_repeat"]
        if len(candidates) == 1:
            repeats.extend(_compare_pair(candidates[0], rerun) for rerun in reruns)
    passed = (bool(results) and not lbad and not rbad and not lunmatched and not runmatched
              and all(row["pass"] for row in results) and all(row["pass"] for row in repeats))
    campaign_inputs: dict[str, set[str]] = defaultdict(set)
    for row in [*lrows, *rrows]:
        if row.get("gate_fingerprint") and row.get("input_identity"):
            campaign_inputs[row["gate_fingerprint"]].add(row["input_identity"])
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "workstation_receipt_comparison",
        "created_at": _utc_now(),
        "left": str(left.resolve()),
        "right": str(right.resolve()),
        "counts": {
            "left_valid": len(lrows), "right_valid": len(rrows), "paired": len(results),
            "repeat_pairs": len(repeats),
            "malformed": len(lbad) + len(rbad),
            "unmatched_left": len(lunmatched), "unmatched_right": len(runmatched),
        },
        "pairs": results,
        "repeat_comparisons": repeats,
        "input_identities_by_gate_fingerprint": {
            fingerprint: sorted(identities)
            for fingerprint, identities in sorted(campaign_inputs.items())
        },
        "malformed_left": lbad,
        "malformed_right": rbad,
        "unmatched_left": [row["relative_path"] for row in lunmatched],
        "unmatched_right": [row["relative_path"] for row in runmatched],
        "pass": passed,
        "overall_pass": passed,
    }
    if out is not None:
        output = Path(out)
        if output.exists():
            raise FileExistsError(f"refusing to overwrite comparison: {output}")
        _atomic_json(output, report)
    return report


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _measurement(rec: dict[str, Any], path: Path) -> dict[str, Any]:
    study = _study(rec)
    backend = _backend(rec, {})
    wall = _finite_number(study.get("wall_seconds", study.get("wall_s", rec.get("wall_seconds"))))
    rss = _finite_number(study.get("peak_rss_MB", rec.get("peak_rss_MB")))
    vram = _finite_number(study.get("vram_peak_MB"))
    samples = study.get("vram_samples_MB", study.get("vram_samples"))
    if vram is None and isinstance(samples, list):
        valid = [_finite_number(value) for value in samples]
        valid = [value for value in valid if value is not None]
        if valid:
            vram = max(valid)
    numerical_error = None
    freq = z = None
    try:
        freq = _as_float_array(rec, "freq")
        z_re = _as_float_array(rec, "Z_re"); z_im = _as_float_array(rec, "Z_im")
        if not (freq.size == z_re.size == z_im.size) or np.any(np.diff(freq) <= 0):
            raise ReceiptError("invalid numerical array lengths or frequency order")
        z = z_re + 1j * z_im
    except ReceiptError as exc:
        numerical_error = str(exc)
    return {
        "path": str(path),
        "case_id": _text(study.get("case_id")),
        "input_identity": _text(study.get("input_identity")),
        "axis": (_text(study.get("axis")) or "").upper(),
        "design": _design(rec, {}), "family": _family(rec, {}, _design(rec, {})),
        "port": _text(study.get("port")) or _text(rec.get("port")),
        "backend": backend, "threads": _threads(rec, {}), "jobs": _jobs(rec, {}),
        "profile": _text(study.get("profile")),
        "wall_seconds": wall, "peak_rss_MB": rss, "vram_peak_MB": vram,
        "unknowns": _positive_int(rec.get("unknowns")),
        "build_seconds": _finite_number(study.get("build_seconds", study.get("build_s"))),
        "factor_seconds": _finite_number(study.get("factor_seconds", study.get("factor_s"))),
        "explicit_outcome": study.get("outcome", study.get("pass")),
        "comparison_group": _text(study.get("comparison_group")),
        "comparison_role": _text(study.get("comparison_role")),
        "group_total_wall_seconds": _finite_number(study.get("group_total_wall_seconds")),
        "numerics_id": _text(rec.get("numerics_id")), "freq": freq, "z": z,
        "numerical_error": numerical_error,
        "source_spd_sha256": _text(study.get("source_spd_sha256")) or _text(rec.get("spd_sha256")),
        "source_ref_sha256": _text(study.get("source_ref_sha256")),
        "started_at": _text(study.get("started_at")), "finished_at": _text(study.get("finished_at")),
        "ram_budget_MB": _finite_number(study.get("ram_budget_MB")),
        "gate_fingerprint": _text(study.get("gate_fingerprint")),
        "convergence": rec.get("convergence"),
        "convergence_cost": rec.get("convergence_cost"),
    }


def _read_measurements(receipts_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows: list[dict[str, Any]] = []
    bad: list[dict[str, str]] = []
    if not receipts_dir.is_dir():
        return rows, bad
    for path in _receipt_files(receipts_dir):
        try:
            raw = _read_json(path)
            if not isinstance(raw, dict):
                raise ReceiptError("receipt root must be an object")
            # Reporting accepts partial performance receipts, but never calls
            # them validation evidence unless their required fields exist.
            rows.append(_measurement(raw, path))
        except ReceiptError as exc:
            bad.append({"path": str(path), "error": str(exc)})
    return rows, bad


def _linear_estimate(rows: Iterable[dict[str, Any]], y_key: str, *, jobs_one: bool = False) -> dict[str, Any] | None:
    points: list[tuple[float, float]] = []
    for row in rows:
        if jobs_one and row.get("jobs") != 1:
            continue
        x = _finite_number(row.get("unknowns")); y = _finite_number(row.get(y_key))
        if x is not None and y is not None and x > 0 and y >= 0:
            points.append((x, y))
    # Three observations and at least three independent sizes keep a straight
    # line from being advertised on two conveniently selected cases.
    if len(points) < 3 or len({x for x, _ in points}) < 3:
        return None
    xs = np.asarray([p[0] for p in points]); ys = np.asarray([p[1] for p in points])
    slope, intercept = np.polyfit(xs, ys, 1)
    predicted = slope * xs + intercept
    ss_res = float(np.sum((ys - predicted) ** 2)); ss_tot = float(np.sum((ys - np.mean(ys)) ** 2))
    r2 = None if ss_tot == 0 else float(1 - ss_res / ss_tot)
    return {"samples": len(points), "distinct_unknowns": len(set(xs.tolist())),
            "intercept_MB": float(intercept), "MB_per_unknown": float(slope), "r_squared": r2}


def _convergence_cases(measurements: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    cases: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in (item for item in measurements if item["axis"] == "F"):
        convergence = row.get("convergence")
        cost = row.get("convergence_cost")
        if not isinstance(convergence, dict) or not isinstance(cost, dict):
            errors.append(f"{row['path']}: convergence/convergence_cost evidence is missing")
            continue
        rms = _finite_number(convergence.get("frequency_rms_delta_db"))
        maximum = _finite_number(convergence.get("frequency_max_delta_db"))
        ref = cost.get("ref") if isinstance(cost.get("ref"), dict) else {}
        var = cost.get("var") if isinstance(cost.get("var"), dict) else {}
        ref_n = _positive_int(ref.get("unknowns")); var_n = _positive_int(var.get("unknowns"))
        ref_wall = _finite_number(ref.get("wall_seconds")); var_wall = _finite_number(var.get("wall_seconds"))
        if None in (rms, maximum, ref_n, var_n, ref_wall, var_wall):
            errors.append(f"{row['path']}: convergence metrics/cost contain missing or invalid values")
            continue
        cases.append({
            "design": row["design"], "port": row["port"],
            "pair": convergence.get("mesh_pair"),
            "rms_db": rms, "max_db": maximum,
            "engine_converged_observation": convergence.get("converged")
                if isinstance(convergence.get("converged"), bool) else None,
            "h_ref": _finite_number(convergence.get("h_ref")),
            "h_var": _finite_number(convergence.get("h_var")),
            "cost": {
                "ref": {"unknowns": ref_n, "wall_seconds": ref_wall},
                "var": {"unknowns": var_n, "wall_seconds": var_wall},
                "total_wall_seconds": ref_wall + var_wall,
            },
        })
    return cases, errors


def _comparison_files(root: Path) -> list[Path]:
    candidates: set[Path] = set()
    directory = root / "comparisons"
    if directory.is_dir():
        candidates.update(directory.glob("*.json"))
    candidates.update(root.glob("comparison*.json"))
    return sorted(candidates)


def _load_comparisons(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows: list[dict[str, Any]] = []
    bad: list[dict[str, str]] = []
    for path in _comparison_files(root):
        try:
            value = _read_json(path)
            if not isinstance(value, dict) or value.get("kind") != "workstation_receipt_comparison":
                raise ReceiptError("not a workstation receipt comparison")
            rows.append(value)
        except ReceiptError as exc:
            bad.append({"path": str(path), "error": str(exc)})
    return rows, bad


def _load_group_runs(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    directory = root / "group_runs"
    if not directory.is_dir():
        return [], []
    attempts: list[dict[str, Any]] = []
    bad: list[dict[str, str]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = _read_json(path)
            required = ("group_id", "total_wall_seconds", "case_count", "executed_case_count",
                        "resumed_case_count", "failed_case_count")
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ReceiptError("group run schema_version must be 1")
            if not _text(value.get("group_id")) or any(_finite_number(value.get(key)) is None for key in required[1:]):
                raise ReceiptError("group run identity/count/wall fields are missing or invalid")
            attempts.append(value)
        except ReceiptError as exc:
            bad.append({"path": str(path), "error": str(exc)})
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        grouped[str(attempt["group_id"])].append(attempt)
    rows = []
    for group_id, values in grouped.items():
        newest = values[-1]
        executed = [value for value in values if int(value.get("executed_case_count", 0)) > 0]
        wall = sum(float(value["total_wall_seconds"]) for value in executed)
        complete = any(
            not value.get("stopped") and int(value["failed_case_count"]) == 0
            and int(value["executed_case_count"]) + int(value["resumed_case_count"]) == int(value["case_count"])
            for value in values
        )
        fingerprints = {_text(value.get("gate_fingerprint")) for value in values}
        fingerprints.discard(None)
        case_identities = set()
        for value in values:
            raw_identities = value.get("case_identities")
            if isinstance(raw_identities, list):
                case_identities.update(identity for identity in raw_identities
                                       if isinstance(identity, str) and identity)
        rows.append({
            "group_id": group_id, "axis": newest.get("axis"), "profile": newest.get("profile"),
            "backend": newest.get("backend"), "threads": _positive_int(newest.get("threads")),
            "jobs": _positive_int(newest.get("jobs")), "affinity": newest.get("affinity"),
            "total_wall_seconds": wall if executed else None, "attempts": len(values),
            "case_count": int(newest["case_count"]), "complete": complete,
            "gate_fingerprint": next(iter(fingerprints)) if len(fingerprints) == 1 else None,
            "case_identities": sorted(case_identities),
        })
    return rows, bad


def _select_folder_campaign(
    measurements: list[dict[str, Any]], comparisons: list[dict[str, Any]],
    group_runs: list[dict[str, Any]], current_fingerprint: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int], list[str]]:
    """Select only evidence that proves membership in the current folder campaign."""
    counts = {"receipts": 0, "comparisons": 0, "group_runs": 0}
    errors: list[str] = []
    if not current_fingerprint:
        counts.update(receipts=len(measurements), comparisons=len(comparisons),
                      group_runs=len(group_runs))
        errors.append("folder campaign has no current gates.engine_fingerprint")
        return [], [], [], counts, errors

    selected_measurements = [
        row for row in measurements if row.get("gate_fingerprint") == current_fingerprint
    ]
    counts["receipts"] = len(measurements) - len(selected_measurements)
    eligible_identities = {
        row["input_identity"] for row in selected_measurements if row.get("input_identity")
    }

    selected_comparisons = []
    for comparison in comparisons:
        if comparison.get("gate_fingerprint") != current_fingerprint:
            counts["comparisons"] += 1
            continue
        mapping = comparison.get("input_identities_by_gate_fingerprint")
        identities = mapping.get(current_fingerprint) if isinstance(mapping, dict) else None
        if not isinstance(identities, list) or not identities or not all(
                isinstance(item, str) and item for item in identities):
            counts["comparisons"] += 1
            continue
        unknown = sorted(set(identities).difference(eligible_identities))
        if unknown:
            errors.append(f"current comparison references ineligible input identities: {unknown}")
            continue
        selected_comparisons.append(comparison)

    selected_groups = []
    for group in group_runs:
        if group.get("gate_fingerprint") != current_fingerprint:
            counts["group_runs"] += 1
            continue
        identities = group.get("case_identities")
        if not isinstance(identities, list) or not identities:
            counts["group_runs"] += 1
            continue
        unknown = sorted(set(identities).difference(eligible_identities))
        if unknown:
            errors.append(f"current group run references ineligible input identities: {unknown}")
            continue
        selected_groups.append(group)
    return (selected_measurements, selected_comparisons, selected_groups, counts, errors)


def _same_numerical_identity(a: dict[str, Any], b: dict[str, Any]) -> tuple[bool, str | None]:
    if not a["numerics_id"] or a["numerics_id"] != b["numerics_id"]:
        return False, "numerics_id mismatch"
    if a["freq"] is None or b["freq"] is None or not _bits_equal(a["freq"], b["freq"]):
        return False, "frequency identity mismatch"
    if a["z"] is None or b["z"] is None or a["z"].shape != b["z"].shape:
        return False, "impedance arrays unavailable"
    for field in ("source_spd_sha256", "source_ref_sha256"):
        if not a[field] or a[field] != b[field]:
            return False, f"{field} mismatch or missing"
    return True, None


def _axis_b(measurements: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any] | None:
    rows = [row for row in measurements if row["axis"] == "B"]
    if not rows:
        return None
    planned = plan.get("axes", {}).get("B", {}) if isinstance(plan, dict) else {}
    combinations = planned.get("combinations") if isinstance(planned, dict) else None
    if not isinstance(combinations, list) or not combinations:
        return {"status": "INCOMPLETE", "cases": len(rows), "source": "matrix_plan B is missing"}
    expected = {tuple(map(int, pair)) for pair in combinations if isinstance(pair, list) and len(pair) == 2}
    planned_ports = planned.get("expected_ports")
    if not isinstance(planned_ports, list) or not planned_ports:
        return {"status": "INCOMPLETE", "cases": len(rows), "source": "matrix_plan B expected_ports is missing"}
    identities = {(str(item.get("design")), str(item.get("port"))) for item in planned_ports if isinstance(item, dict)}
    missing = []
    failed = []
    checks = 0
    for identity in identities:
        group = [row for row in rows if (row["design"], row["port"]) == identity]
        observed = {(row["threads"], row["jobs"]) for row in group}
        absent = expected.difference(observed)
        if absent:
            missing.append(f"{identity}: missing {sorted(absent)}")
        by_thread: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in group:
            if row["threads"] is not None:
                by_thread[row["threads"]].append(row)
        for thread_rows in by_thread.values():
            reference = thread_rows[0]
            for candidate in thread_rows[1:]:
                valid, reason = _same_numerical_identity(reference, candidate); checks += 1
                if not valid or not _bits_equal(reference["z"].real.copy(), candidate["z"].real.copy()) or not _bits_equal(reference["z"].imag.copy(), candidate["z"].imag.copy()):
                    failed.append(reason or f"same-thread bit mismatch: {identity}, threads={reference['threads']}")
        representatives = [values[0] for values in by_thread.values()]
        if representatives:
            reference = representatives[0]
            for candidate in representatives[1:]:
                valid, reason = _same_numerical_identity(reference, candidate); checks += 1
                if not valid:
                    failed.append(reason or "identity mismatch")
                elif float(np.max(_relative_error(candidate["z"], reference["z"]))) > 1e-9:
                    failed.append(f"cross-thread error exceeds 1e-9: {identity}")
    status = "FAIL" if failed else ("INCOMPLETE" if missing or not checks else "PASS")
    return {"status": status, "cases": len(rows), "checks": checks,
            "source": "automatic same-thread bit and cross-thread E3 checks",
            "missing": missing, "failures": failed}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _axis_d(measurements: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any] | None:
    rows = [row for row in measurements if row["axis"] == "D"]
    if not rows:
        return None
    planned = plan.get("axes", {}).get("D") if isinstance(plan, dict) else None
    if not isinstance(planned, dict) or not planned:
        return {"status": "INCOMPLETE", "cases": len(rows), "source": "matrix_plan D is missing"}
    plans = planned.get("plans")
    planned_ports = planned.get("expected_ports")
    if not isinstance(plans, dict) or not plans or not isinstance(planned_ports, list) or not planned_ports:
        return {"status": "INCOMPLETE", "cases": len(rows), "source": "matrix_plan D plans/expected_ports are missing"}
    expected_profiles = {f"ram{key}" for key in plans}
    expected_identities = {(str(item.get("design")), str(item.get("port")))
                           for item in planned_ports if isinstance(item, dict)}
    observed_profiles = {row["profile"] for row in rows}
    missing = sorted(expected_profiles.difference(observed_profiles))
    failures = []
    identities_by_profile = {
        profile: {(r["design"], r["port"]) for r in rows if r["profile"] == profile}
        for profile in expected_profiles
    }
    nonempty_sets = [value for value in identities_by_profile.values() if value]
    if (not nonempty_sets or any(value != expected_identities for value in nonempty_sets)
            or len(nonempty_sets) != len(expected_profiles)):
        missing.append("RAM profiles do not contain the same port set")
    profile_results = {}
    for profile in expected_profiles.intersection(observed_profiles):
        group = [row for row in rows if row["profile"] == profile]
        events = []
        budget_values = {row["ram_budget_MB"] for row in group if row["ram_budget_MB"] is not None}
        if len(budget_values) != 1:
            missing.append(f"{profile}: one RAM budget is required"); continue
        for index, row in enumerate(group):
            start, finish, rss = _parse_time(row["started_at"]), _parse_time(row["finished_at"]), row["peak_rss_MB"]
            if start is None or finish is None or finish < start or rss is None:
                missing.append(f"{profile}: invalid overlap timing/RSS in {row['path']}"); continue
            events.append((start, 1, index, rss)); events.append((finish, -1, index, rss))
        active = {}; peak_sum = 0.0
        for _time, kind, index, rss in sorted(events, key=lambda event: (event[0], event[1])):
            if kind < 0: active.pop(index, None)
            else: active[index] = rss
            peak_sum = max(peak_sum, sum(active.values()))
        budget = next(iter(budget_values))
        passed = peak_sum <= budget
        profile_results[profile] = {"overlapping_peak_rss_sum_MB": peak_sum, "budget_MB": budget, "pass": passed}
        if not passed: failures.append(f"{profile}: overlapping peak RSS {peak_sum:.3g} > budget {budget:.3g} MB")
    status = "FAIL" if failures else ("INCOMPLETE" if missing or len(profile_results) != len(expected_profiles) else "PASS")
    return {"status": status, "cases": len(rows), "source": "automatic overlapping RSS budget checks",
            "profiles": profile_results, "missing": missing, "failures": failures}


def _axis_e(measurements: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any] | None:
    rows = [row for row in measurements if row["axis"] == "E"]
    if not rows:
        return None
    planned = plan.get("axes", {}).get("E") if isinstance(plan, dict) else None
    if not isinstance(planned, dict):
        return {"status": "INCOMPLETE", "cases": len(rows), "source": "matrix_plan E is missing"}
    planned_ports = planned.get("expected_ports")
    if not isinstance(planned_ports, list) or not planned_ports:
        return {"status": "INCOMPLETE", "cases": len(rows), "source": "matrix_plan E expected_ports is missing"}
    expected_identities = {(str(item.get("design")), str(item.get("port")))
                           for item in planned_ports if isinstance(item, dict)}
    basis_cpu = [row for row in rows if row["comparison_role"] == "basis_cpu"]
    basis_gpu = [row for row in rows if row["comparison_role"] == "basis_gpu"]
    gpu_rows = [row for row in rows if row["comparison_role"] == "gpu_candidate"]
    cpu_pool = [row for row in measurements if row["backend"] == "splu" and row["comparison_role"] not in {"basis_cpu", "basis_gpu"}]
    missing = []; failures = []; checks = 0
    if len(basis_cpu) != 1 or len(basis_gpu) != 1:
        missing.append("exactly one basis_cpu and basis_gpu receipt are required")
    else:
        valid, reason = _same_numerical_identity(basis_cpu[0], basis_gpu[0]); checks += 1
        if not valid or float(np.max(_relative_error(basis_gpu[0]["z"], basis_cpu[0]["z"]))) > 1e-5:
            failures.append(reason or "E4 basis GPU error exceeds 1e-5")
    expected_jobs = {1, 2, 4, 8}
    identities = {(row["design"], row["port"], row["comparison_group"]) for row in gpu_rows}
    observed_identities = {(design, port) for design, port, _group in identities}
    if observed_identities != expected_identities:
        missing.append(f"GPU port coverage mismatch: missing {sorted(expected_identities - observed_identities)}")
    for design, port, group_id in identities:
        variants = [row for row in gpu_rows if (row["design"], row["port"], row["comparison_group"]) == (design, port, group_id)]
        if {row["jobs"] for row in variants} != expected_jobs:
            missing.append(f"{design}/{port}: GPU jobs 1,2,4,8 are incomplete")
        references = [row for row in cpu_pool if row["design"] == design and row["port"] == port and row["comparison_group"] == group_id]
        if not references:
            missing.append(f"{design}/{port}: CPU reference is missing"); continue
        reference = references[0]
        for candidate in variants:
            valid, reason = _same_numerical_identity(reference, candidate); checks += 1
            if not valid:
                failures.append(reason or "identity mismatch"); continue
            rel = _relative_error(candidate["z"], reference["z"])
            family = candidate["family"]
            maximum = float(np.max(rel))
            if family in {"package", "unknown"}:
                passed = maximum <= 1e-8
            elif family == "pcb":
                passed = maximum <= 1e-6
            else:
                passed = False
            if family == "pcb":
                mask = candidate["freq"] >= 1e6
                passed = passed and bool(np.any(mask)) and float(np.max(rel[mask])) <= 1e-7
            if not passed:
                label = "strict unclassified" if family == "unknown" else str(family or "missing-family")
                failures.append(f"{design}/{port} jobs={candidate['jobs']}: E3 GPU {label} tolerance failed")
    if not gpu_rows:
        missing.append("GPU concurrency receipts are missing")
    status = "FAIL" if failures else ("INCOMPLETE" if missing or not checks else "PASS")
    return {"status": status, "cases": len(rows), "checks": checks,
            "source": "automatic GPU/CPU E3 and decap-basis E4 checks",
            "missing": missing, "failures": failures}


def _axis_outcomes(measurements: list[dict[str, Any]], comparisons: list[dict[str, Any]],
                   plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    for axis in "ABCDEF":
        evidence = [row for row in measurements if row.get("axis") == axis]
        planned = plan.get("axes", {}).get(axis, {}) if isinstance(plan, dict) else {}
        if (axis == "E" and not evidence and isinstance(planned, dict)
                and str(planned.get("status", "")).casefold() == "not_run"):
            reason = _text(planned.get("reason")) or "planned as not run"
            outcomes[axis] = {"status": "NOT_RUN", "cases": 0,
                              "source": f"planned NOT_RUN: {reason}"}
            continue
        explicit = [row.get("explicit_outcome") for row in evidence]
        explicit_bool = [value for value in explicit if isinstance(value, bool)]
        automatic = ({"B": _axis_b, "D": _axis_d, "E": _axis_e}.get(axis))
        if automatic:
            assessed = automatic(measurements, plan)
            if assessed is not None:
                outcomes[axis] = assessed
                continue
        if axis in {"A", "E"}:
            relevant = []
            relevant_documents = []
            repeat_relevant = []
            for comparison in comparisons:
                pairs = comparison.get("pairs", [])
                if not isinstance(pairs, list):
                    continue
                if axis == "A":
                    selected = [pair for pair in pairs if isinstance(pair, dict)
                                and (pair.get("axis_right") == "A" or pair.get("command_right") == "baseline")
                                and pair.get("category") in {"cpu", "gpu"}]
                    repeat_relevant.extend(
                        pair for pair in comparison.get("repeat_comparisons", [])
                        if isinstance(pair, dict)
                        and (pair.get("axis_right") == "A" or pair.get("command_right") == "baseline")
                    )
                else:
                    selected = [pair for pair in pairs if isinstance(pair, dict)
                                and (pair.get("axis_left") == "E" or pair.get("axis_right") == "E")
                                and pair.get("category") in {"gpu", "basis_gpu", "basis_cpu"}]
                if selected:
                    relevant.extend(selected)
                    relevant_documents.append(comparison)
            if relevant:
                failed = (not all(document.get("pass") is True for document in relevant_documents)
                          or not all(pair.get("pass") is True for pair in relevant)
                          or not all(pair.get("pass") is True for pair in repeat_relevant))
                missing = []
                if axis == "A":
                    by_identity: dict[tuple[str, str], set[str]] = defaultdict(set)
                    for pair in relevant:
                        identity = pair.get("identity", {})
                        by_identity[(identity.get("design"), identity.get("port"))].add(pair.get("role_right"))
                    required_roles = {"cpu_reference", "gpu_candidate", "gpu_repeat"}
                    for identity, roles in by_identity.items():
                        if not required_roles.issubset(roles):
                            missing.append(f"{identity}: missing baseline roles {sorted(required_roles.difference(roles))}")
                    if len(repeat_relevant) < len(by_identity):
                        missing.append("GPU candidate-to-repeat comparisons are incomplete")
                status = "FAIL" if failed else ("INCOMPLETE" if missing else "PASS")
                outcomes[axis] = {"status": status, "cases": len(relevant),
                                  "source": "strict scoped numerical comparisons", "missing": missing,
                                  "repeat_comparisons": len(repeat_relevant)}
                continue
        if explicit_bool:
            passed = len(explicit_bool) == len(evidence) and all(explicit_bool)
            outcomes[axis] = {"status": "PASS" if passed else "FAIL", "cases": len(evidence),
                              "source": "explicit receipt outcomes"}
        elif evidence:
            outcomes[axis] = {"status": "OBSERVED" if axis == "F" else "INCOMPLETE",
                              "cases": len(evidence), "source": "measurements without a recorded decision"}
        else:
            outcomes[axis] = {"status": "NOT_RUN", "cases": 0, "source": "no evidence"}
    return outcomes


def _archive_existing(path: Path, archive_dir: Path) -> Path | None:
    if not path.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = archive_dir / f"{path.stem}_{stamp}{path.suffix}"
    suffix = 2
    while target.exists():
        target = archive_dir / f"{path.stem}_{stamp}_{suffix}{path.suffix}"; suffix += 1
    shutil.move(str(path), str(target))
    return target


def _plot(root: Path, measurements: list[dict[str, Any]], group_runs: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except (ImportError, RuntimeError) as exc:
        return [], [f"matplotlib unavailable: {exc}"]
    figures = root / "figures"; archives = root / "archives" / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    made: list[str] = []; missing: list[str] = []

    def save(name: str, draw) -> None:
        path = figures / name
        _archive_existing(path, archives)
        fig, ax = plt.subplots(figsize=(7, 4.5))
        draw(ax)
        fig.tight_layout(); fig.savefig(path, dpi=160); plt.close(fig)
        made.append(str(path.relative_to(root)))

    group_totals = [r for r in group_runs if r["complete"] and r["total_wall_seconds"] is not None
                    and r["jobs"] is not None and r["threads"] is not None]
    raw_totals = group_totals or [r for r in measurements if r["group_total_wall_seconds"] is not None and r["jobs"] is not None and r["threads"] is not None]
    totals = []
    seen_totals = set()
    for row in raw_totals:
        identity = row.get("group_id") or row.get("comparison_group") or (row.get("design"), row["backend"], row["threads"], row["jobs"], row["profile"], row.get("group_total_wall_seconds"))
        if identity not in seen_totals:
            seen_totals.add(identity); totals.append(row)
    wall = totals or [r for r in measurements if r["wall_seconds"] is not None and r["jobs"] is not None and r["threads"] is not None]
    if len(wall) >= 2:
        def wall_draw(ax):
            for threads in sorted({r["threads"] for r in wall}):
                key = "total_wall_seconds" if group_totals else ("group_total_wall_seconds" if totals else "wall_seconds")
                points = [(r["jobs"], r[key]) for r in wall if r["threads"] == threads]
                ax.scatter([p[0] for p in points], [p[1] for p in points], label=f"threads={threads}")
            title = "Sweep wall time by jobs and threads" if totals else "Per-receipt wall time by jobs and threads"
            ax.set(xlabel="jobs", ylabel="wall time (s)", title=title)
            ax.legend()
        save("w15_wall_jobs_threads.png", wall_draw)
    else:
        _archive_existing(figures / "w15_wall_jobs_threads.png", archives)
        missing.append("wall/jobs/threads plot: at least two complete measurements required")

    rss = [r for r in measurements if r["peak_rss_MB"] is not None and r["unknowns"] is not None]
    if len(rss) >= 2:
        save("w15_rss_unknowns.png", lambda ax: (
            ax.scatter([r["unknowns"] for r in rss], [r["peak_rss_MB"] for r in rss]),
            ax.set(xlabel="unknowns", ylabel="peak RSS (MB)", title="Peak RSS by model size")))
    else:
        _archive_existing(figures / "w15_rss_unknowns.png", archives)
        missing.append("RSS/unknowns plot: at least two complete measurements required")

    vram = [r for r in measurements if r["vram_peak_MB"] is not None and r["jobs"] is not None]
    if len(vram) >= 2:
        save("w15_vram_jobs.png", lambda ax: (
            ax.scatter([r["jobs"] for r in vram], [r["vram_peak_MB"] for r in vram]),
            ax.set(xlabel="jobs", ylabel="peak VRAM (MB)", title="Peak VRAM by concurrent jobs")))
    else:
        _archive_existing(figures / "w15_vram_jobs.png", archives)
        missing.append("VRAM/jobs plot: at least two complete measurements required")
    return made, missing


_PREDICTIONS = {
    "A": "같은 CPU 스레드 수에서는 비트 동일하며 GPU 오차는 E3 계약 안일 것으로 예측했다.",
    "B": "jobs 15까지 처리량이 거의 선형으로 늘고 프로세스당 RSS는 jobs와 무관할 것으로 예측했다.",
    "C": "CPU affinity가 실제 병렬도와 플래너 선택에 반영되는지 확인하기로 했다.",
    "D": "모든 RAM 프로필에서 합산 peak RSS가 플래너 예산 이내일 것으로 예측했다.",
    "E": "GPU jobs 4까지 벽시계가 감소하며 동시 실행도 E3 계약을 지킬 것으로 예측했다.",
    "F": "선택 실행이며 기본 격자 변경 판정은 하지 않기로 했다.",
}


def _write_korean_report(path: Path, summary: dict[str, Any]) -> None:
    standalone = summary.get("study_mode") == "standalone_folder"
    lines = [
        ("# 독립 실행 엔진 및 입력 검증 보고서" if standalone
         else "# W15 워크스테이션 검증 보고서"),
        "",
        f"생성 시각: {summary['created_at']}",
        "",
        "이 보고서는 저장된 영수증만 집계한다. 측정값이나 비교 입력이 없으면 PASS로 채우지 않았다.",
        "",
    ]
    if standalone:
        lines.extend([
            "검증 범위는 설치 런타임, 입력 파일 구조, 합성 CPU/GPU 수치 점검이다. 과거 비공개 데이터 재현은 실행하지 않았다.",
            "모델 정확도는 baseline 영수증과 보고서 비교에서 별도로 판정한다.",
            "자동 발견 입력의 설계 family가 unknown이면 분류를 추측하지 않고 CPU 1e-9, GPU 1e-8의 보수적 비교 한계를 적용한다.",
            "",
        ])
    else:
        lines.extend([
            "검증 범위는 사전등록된 W15 과거 재현 게이트와 워크스테이션 측정 캠페인이다.",
            "",
        ])
    lines.extend([
        "## 환경과 게이트",
        "",
        f"- 환경 상태: {summary['environment']['status']}",
        f"- 엔진 게이트: {summary['gates']['status']}",
        f"- 게이트 종류: {summary['gates']['gate_kind']}",
        f"- 과거 재현 상태: {summary['gates']['historical_reproduction']}",
    ])
    if standalone:
        selection = summary["campaign_selection"]
        excluded = summary["excluded_history_counts"]
        lines.extend([
            f"- 현재 게이트 fingerprint: {selection.get('gate_fingerprint') or '없음'}",
            f"- 현재 캠페인 선택: 영수증 {selection['selected']['receipts']}개, "
            f"비교 {selection['selected']['comparisons']}개, 그룹 실행 {selection['selected']['group_runs']}개",
            f"- 제외된 이전 이력: 영수증 {excluded['receipts']}개, "
            f"비교 {excluded['comparisons']}개, 그룹 실행 {excluded['group_runs']}개",
        ])
    if summary["environment"].get("python"):
        lines.append(f"- Python: {summary['environment']['python']}")
    if summary["environment"].get("gpu"):
        lines.append(f"- 실제 탐지 GPU: {summary['environment']['gpu']}")
    lines.extend([
        "- `detected` 프로필은 실제 탐지값이며 `laptop`/`workstation`과 RAM·VRAM 변형 프로필은 플래너 에뮬레이션이다.",
        "- CPU affinity는 가장 낮은 번호의 논리 CPU `n`개를 선택한다. NUMA 노드나 CCD 한쪽에 집중되거나 SMT 형제 스레드가 함께 선택될 수 있으므로 C의 시간 측정은 토폴로지 중립 결과가 아니다.",
        "- 프로세스당 VRAM 추정은 배치 시작 전 유휴 표본 5개의 중앙값을 기준선으로 삼고 `(전체 GPU peak - 유휴 중앙값) / 동시 worker 수`로 계산한다. 다른 GPU 작업과 worker별 peak 차이 때문에 개별 프로세스 귀속에는 한계가 있다.",
        "",
        "## A–F 결과",
        "",
        "| 항목 | 상태 | 사례 수 | 근거 | 예측 대 결과 |",
        "|---|---:|---:|---|---|",
    ])
    for axis in "ABCDEF":
        outcome = summary["outcomes"][axis]
        observed = ("관측 자료가 충분하지 않다." if outcome["status"] in {"NOT_RUN", "INCOMPLETE"}
                    else f"기록된 상태는 {outcome['status']}이다.")
        lines.append(f"| {axis} | {outcome['status']} | {outcome['cases']} | {outcome['source']} | {_PREDICTIONS[axis]} {observed} |")
    lines.extend(["", "## F 격자 민감도 관측", ""])
    if summary["convergence_cases"]:
        lines.extend([
            "F는 관측 자료이며 기본 격자 변경 판정에 사용하지 않았다.",
            "",
            "| 설계 | 포트 | pair | RMS dB | max dB | 기준 미지수 / 초 | 변형 미지수 / 초 | 합계 초 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ])
        for case in summary["convergence_cases"]:
            ref = case["cost"]["ref"]; var = case["cost"]["var"]
            lines.append(
                f"| {case['design']} | {case['port']} | {case['pair']} | {case['rms_db']:.4g} | "
                f"{case['max_db']:.4g} | {ref['unknowns']} / {ref['wall_seconds']:.3g} | "
                f"{var['unknowns']} / {var['wall_seconds']:.3g} | {case['cost']['total_wall_seconds']:.3g} |"
            )
    else:
        lines.append("실행되어 검증 가능한 F 사례가 없다.")
    lines.extend(["", "## 플래너 상수 추정", ""])
    estimates = summary["sizing_constant_estimates"]
    if not estimates:
        lines.append("서로 다른 미지수 크기의 독립 표본이 3개 미만이어서 상수를 추정하지 않았다.")
    else:
        for name, value in estimates.items():
            lines.append(f"- {name}: 절편 {value['intercept_MB']:.3g} MB, 미지수당 {value['MB_per_unknown']:.6g} MB, 표본 {value['samples']}개")
    lines.extend(["", "## 산출물", ""])
    if summary["figures"]:
        lines.extend(f"- `{name}`" for name in summary["figures"])
    else:
        lines.append("- 생성할 수 있는 그림이 없다.")
    lines.extend(["", "## 엔진 요구사항과 누락 사례", ""])
    requirements = summary["requirements_and_missing_cases"]
    if requirements:
        lines.extend(f"- {item}" for item in requirements)
    else:
        lines.append("- 없음")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_report(root: Path) -> dict[str, Any]:
    """Build ``summary.json``, available figures, and Korean ``W15_REPORT.md``.

    Existing current artifacts are moved to ``root/archives`` before the new
    current version is published.  Raw receipts are never moved or modified.
    """
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    measurements, receipt_errors = _read_measurements(root / "receipts")
    comparisons, comparison_errors = _load_comparisons(root)
    group_runs, group_run_errors = _load_group_runs(root)
    metadata_errors: list[dict[str, str]] = []
    def optional_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = _read_json(path)
            if not isinstance(value, dict):
                raise ReceiptError("root must be an object")
            return value
        except ReceiptError as exc:
            metadata_errors.append({"path": str(path), "error": str(exc)})
            return {}
    config_path = root / "config.json"
    config_raw = optional_json(config_path)
    configured_folder = config_raw.get("configuration_mode") == "folder"
    env_raw = optional_json(root / "env.json")
    gates_raw = optional_json(root / "gates.json")
    env_probe = env_raw.get("probe", {}) if isinstance(env_raw, dict) else {}
    env_gpu_rows = env_probe.get("nvidia_smi", {}).get("gpus", []) if isinstance(env_probe, dict) else []
    environment = {
        "status": "PASS" if isinstance(env_raw, dict) and env_raw.get("ok") is True else
                  ("FAIL" if env_raw else "NOT_RUN"),
        "python": env_probe.get("python", {}).get("version") if isinstance(env_probe, dict) else None,
        "platform": env_probe.get("platform") if isinstance(env_probe, dict) else None,
        "engine_git": env_raw.get("engine_git") if isinstance(env_raw, dict) else None,
        "gpu": ", ".join(f"{row.get('name')} ({row.get('vram_total_MB')} MB)"
                         for row in env_gpu_rows if isinstance(row, dict)) or None,
        "profiles": env_probe.get("profiles", {}) if isinstance(env_probe, dict) else {},
        "version_checks": env_raw.get("version_checks", {}) if isinstance(env_raw, dict) else {},
    }
    gate_suites = gates_raw.get("suites", []) if isinstance(gates_raw, dict) else []
    standalone_raw = gates_raw.get("standalone", {}) if isinstance(gates_raw, dict) else {}
    gate_kind = gates_raw.get("gate_kind") if isinstance(gates_raw, dict) else None
    standalone_gate = gate_kind == "standalone_runtime_and_inputs"
    folder_mode = configured_folder or standalone_gate
    if standalone_gate:
        gate_pass = (
            gates_raw.get("ok") is True
            and isinstance(standalone_raw, dict)
            and standalone_raw.get("ok") is True
            and standalone_raw.get("gate_kind") == "standalone_runtime_and_inputs"
            and gates_raw.get("historical_reproduction") == "not_run"
            and standalone_raw.get("historical_reproduction") == "not_run"
        )
        historical_reproduction = "not_run"
        accuracy_verification = standalone_raw.get("accuracy_verification")
    else:
        gate_pass = gates_raw.get("ok") is True and len(gate_suites) == 3
        gate_kind = gate_kind or "preregistered_historical_reproduction"
        historical_reproduction = (gates_raw.get("historical_reproduction")
                                   or ("passed" if gate_pass else "not_established"))
        accuracy_verification = gates_raw.get("accuracy_verification")
    if configured_folder and not standalone_gate:
        gate_pass = False
    gates = {
        "status": "PASS" if gate_pass else ("FAIL" if gates_raw else "NOT_RUN"),
        "gate_kind": gate_kind or "not_run",
        "historical_reproduction": historical_reproduction,
        "accuracy_verification": accuracy_verification,
        "engine_fingerprint": gates_raw.get("engine_fingerprint") if isinstance(gates_raw, dict) else None,
        "suites": [{key: row.get(key) for key in ("name", "ok", "tests", "passed", "skipped", "wall_seconds")}
                   for row in gate_suites if isinstance(row, dict)],
    }
    current_fingerprint = gates.get("engine_fingerprint")
    excluded_history_counts = {"receipts": 0, "comparisons": 0, "group_runs": 0,
                               "receipt_errors": 0, "comparison_errors": 0,
                               "group_run_errors": 0}
    campaign_identity_errors: list[str] = []
    if folder_mode:
        selection_fingerprint = current_fingerprint
        if config_path.is_file():
            engine_evidence = gates_raw.get("engine_evidence")
            recorded_config_hash = (engine_evidence.get("config_sha256")
                                    if isinstance(engine_evidence, dict) else None)
            actual_config_hash = _sha256(config_path)
            if recorded_config_hash != actual_config_hash:
                campaign_identity_errors.append(
                    "current config SHA-256 differs from gates.engine_evidence.config_sha256")
                selection_fingerprint = None
        (measurements, comparisons, group_runs, excluded,
         selection_errors) = _select_folder_campaign(
            measurements, comparisons, group_runs, selection_fingerprint)
        campaign_identity_errors.extend(selection_errors)
        excluded_history_counts.update(excluded)
        excluded_history_counts.update(
            receipt_errors=len(receipt_errors), comparison_errors=len(comparison_errors),
            group_run_errors=len(group_run_errors))
        # Evidence without readable current-campaign proof is retained on disk as history. It must
        # neither support an outcome nor turn a new folder campaign into a false failure.
        receipt_errors = []
        comparison_errors = []
        group_run_errors = []
    campaign_selection = {
        "basis": "exact_gate_fingerprint_and_input_identity" if folder_mode else "all_manual_evidence",
        "gate_fingerprint": current_fingerprint,
        "selected": {"receipts": len(measurements), "comparisons": len(comparisons),
                     "group_runs": len(group_runs)},
    }
    plan = optional_json(root / "matrix_plan.json")
    outcomes = _axis_outcomes(measurements, comparisons, plan)
    convergence_cases, convergence_errors = _convergence_cases(measurements)
    f_measurements = [row for row in measurements if row["axis"] == "F"]
    if f_measurements:
        outcomes["F"] = {
            "status": "OBSERVED" if convergence_cases and not convergence_errors else "INCOMPLETE",
            "cases": len(convergence_cases),
            "source": "per-case mesh RMS/max dB and cost evidence",
        }
    if current_fingerprint and not folder_mode:
        for row in measurements:
            fingerprint = row.get("gate_fingerprint")
            if fingerprint and fingerprint != current_fingerprint:
                campaign_identity_errors.append(
                    f"{row['path']}: study gate_fingerprint differs from current gates.engine_fingerprint"
                )
    figures, plot_missing = _plot(root, measurements, group_runs)
    estimates: dict[str, Any] = {}
    rss_est = _linear_estimate(measurements, "peak_rss_MB")
    if rss_est is not None:
        estimates["rss"] = rss_est
    vram_est = _linear_estimate(measurements, "vram_peak_MB", jobs_one=True)
    if vram_est is not None:
        estimates["vram_per_process"] = vram_est

    missing: list[str] = []
    missing.extend(f"영수증 오류: {row['path']}: {row['error']}" for row in receipt_errors)
    missing.extend(f"비교 오류: {row['path']}: {row['error']}" for row in comparison_errors)
    missing.extend(f"그룹 실행 오류: {row['path']}: {row['error']}" for row in group_run_errors)
    missing.extend(f"메타데이터 오류: {row['path']}: {row['error']}" for row in metadata_errors)
    missing.extend(f"F 관측 오류: {error}" for error in convergence_errors)
    missing.extend(f"캠페인 식별 오류: {error}" for error in campaign_identity_errors)
    missing.extend(plot_missing)
    for axis, outcome in outcomes.items():
        if axis != "F" and outcome["status"] in {"NOT_RUN", "INCOMPLETE"}:
            missing.append(f"필수 항목 {axis}의 판정 근거가 부족하다.")
        if outcome["status"] == "FAIL":
            missing.append(f"항목 {axis}가 실패했다. 엔진 파라미터를 조정하지 말고 원인을 검토해야 한다.")
    if outcomes["C"]["status"] == "FAIL":
        missing.append("HardwareProfile.detect()가 실제 affinity 제한을 반영하지 않았다. 플래너에 affinity-aware 탐지가 필요하다.")
    if environment["status"] != "PASS":
        missing.append(f"환경 검증 상태가 {environment['status']}이다.")
    if gates["status"] != "PASS":
        missing.append(f"필수 엔진 게이트 상태가 {gates['status']}이다.")
    if not estimates:
        missing.append("사이징 상수 추정에 필요한 서로 다른 크기의 표본이 부족하다.")

    required = [outcomes[a]["status"] for a in "ABCDE"]
    hard_failure = ("FAIL" in required or environment["status"] == "FAIL" or gates["status"] == "FAIL"
                    or bool(receipt_errors) or bool(comparison_errors))
    hard_failure = (hard_failure or bool(metadata_errors) or bool(group_run_errors)
                    or bool(campaign_identity_errors))
    complete = (all(x == "PASS" for x in required) and environment["status"] == "PASS"
                and gates["status"] == "PASS" and not receipt_errors and not comparison_errors)
    complete = complete and not metadata_errors and not group_run_errors and not campaign_identity_errors
    overall = "FAIL" if hard_failure else ("PASS" if complete else "INCOMPLETE")
    summary = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "w15_workstation_summary",
        "created_at": _utc_now(),
        "root": str(root.resolve()),
        "study_mode": "standalone_folder" if folder_mode else "preregistered_legacy",
        "overall_status": overall,
        "environment": environment,
        "gates": gates,
        "campaign_selection": campaign_selection,
        "excluded_history_counts": excluded_history_counts,
        "receipt_count": len(measurements),
        "comparison_count": len(comparisons),
        "group_run_count": len(group_runs),
        "outcomes": outcomes,
        "convergence_cases": convergence_cases,
        "convergence_errors": convergence_errors,
        "campaign_identity_errors": campaign_identity_errors,
        "sizing_constant_estimates": estimates,
        "figures": figures,
        "requirements_and_missing_cases": missing,
        "receipt_errors": receipt_errors,
        "comparison_errors": comparison_errors,
        "group_run_errors": group_run_errors,
        "metadata_errors": metadata_errors,
    }
    archive_dir = root / "archives"
    summary_path = root / "summary.json"; report_path = root / "W15_REPORT.md"
    archived = [item for item in (_archive_existing(summary_path, archive_dir),
                                  _archive_existing(report_path, archive_dir)) if item is not None]
    if archived:
        summary["archived_previous"] = [str(path.relative_to(root)) for path in archived]
    _atomic_json(summary_path, summary)
    _write_korean_report(report_path, summary)
    summary["summary_path"] = str(summary_path)
    summary["report_path"] = str(report_path)
    return summary


__all__ = ["compare_directories", "make_report"]
