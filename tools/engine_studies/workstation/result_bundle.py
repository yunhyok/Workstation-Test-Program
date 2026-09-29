"""Export a bounded, reviewable snapshot of workstation validation results."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from common import atomic_json


ROOT_FILES = (
    "config.json", "env.json", "gates.json", "preparation.json",
    "matrix_plan.json", "summary.json", "W15_REPORT.md",
    "program_validation_summary.json", "status.json", "batch_status.json",
)
JSON_DIRS = ("receipts", "comparisons", "failures", "group_runs")


def _safe_kind(path: Path, root: Path, directory: bool) -> bool:
    """Reject links, junctions, and entries outside the resolved result root."""
    try:
        if not path.resolve().is_relative_to(root):
            return False
        info = path.stat(follow_symlinks=False)
    except (FileNotFoundError, ValueError):
        return False
    if getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)


def _children(path: Path, root: Path, skipped: list[dict]) -> list[Path]:
    if not _safe_kind(path, root, True):
        if os.path.lexists(path):
            skipped.append({"path": path.relative_to(root).as_posix() + "/",
                            "reason": "unsafe directory"})
        return []
    return sorted(path.iterdir(), key=lambda p: p.name)


def _candidates(root: Path, skipped: list[dict]):
    def children(path: Path) -> list[Path]:
        return _children(path, root, skipped)

    for name in ROOT_FILES:
        yield root / name
    for dirname in JSON_DIRS:
        for path in children(root / dirname):
            if path.suffix.lower() == ".json":
                yield path
    for path in children(root / "figures"):
        if path.name.startswith("w15_") and path.suffix.lower() == ".png":
            yield path
    for path in children(root / "launcher-logs"):
        if path.suffix.lower() == ".txt":
            yield path
    archives = root / "archives"
    for path in children(archives):
        if ((path.name.startswith("summary_") and path.suffix.lower() == ".json") or
                (path.name.startswith("W15_REPORT_") and path.suffix.lower() == ".md")):
            yield path
    for path in children(archives / "figures"):
        if path.name.startswith("w15_") and path.suffix.lower() == ".png":
            yield path
    for run in children(root / "runs"):
        if not _safe_kind(run, root, True):
            skipped.append({"path": run.relative_to(root).as_posix() + "/",
                            "reason": "unsafe directory"})
            continue
        for name in ("run_summary.json", "batch_summary.json", "config.before.json", "log.txt"):
            yield run / name
        for path in children(run):
            if path.name.startswith("pytest-") and path.suffix.lower() == ".xml":
                yield path
        for path in children(run / "comparison_failures"):
            if path.suffix.lower() == ".json":
                yield path
        for path in children(run / "archives"):
            if path.name.startswith("matrix_plan.before") and path.suffix.lower() == ".json":
                yield path
        for path in children(run / "worker"):
            if path.name.endswith((".request.json", ".output.json")):
                yield path
        for comparison in children(run / "comparison-inputs"):
            if _safe_kind(comparison, root, True):
                for side in ("left", "right"):
                    for path in children(comparison / side):
                        if path.suffix.lower() == ".json":
                            yield path
            else:
                skipped.append({"path": comparison.relative_to(root).as_posix() + "/",
                                "reason": "unsafe directory"})
        for step in children(run / "failed-steps"):
            if _safe_kind(step, root, True):
                for name in ("run_summary.json", "log.txt"):
                    yield step / name
            else:
                skipped.append({"path": step.relative_to(root).as_posix() + "/",
                                "reason": "unsafe directory"})


def _stamp(path: Path, root: Path):
    if not _safe_kind(path, root, False):
        return None
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _state(captured: dict[str, bytes], changed: list[str], skipped: list[dict]) -> tuple[str, str, dict, list[str]]:
    records = {}
    issues = []
    for name in ("status.json", "batch_status.json"):
        if name not in captured:
            continue
        try:
            value = json.loads(captured[name])
            if not isinstance(value, dict) or not isinstance(value.get("running"), bool):
                raise ValueError("expected object with boolean running")
            records[name] = {key: value.get(key) for key in ("running", "exit_code", "error")}
            if name == "status.json":
                records[name]["subcommand"] = value.get("subcommand")
            if name == "batch_status.json":
                batch = value.get("batch")
                if not isinstance(batch, dict) or not isinstance(batch.get("steps", []), list):
                    raise ValueError("expected batch object with steps array")
                records[name].update(completed=batch.get("completed"), total=batch.get("total"))
                records[name]["report_completed"] = any(
                    step.get("id") == "report" and step.get("state") == "completed"
                    for step in batch.get("steps", []) if isinstance(step, dict))
        except (ValueError, TypeError, AttributeError):
            records.pop(name, None)
            issues.append(f"malformed {name}")
    report_current = (records.get("status.json", {}).get("subcommand") == "report" or
                      records.get("status.json", {}).get("subcommand") == "batch" and
                      records.get("batch_status.json", {}).get("report_completed") is True)
    try:
        summary = json.loads(captured["summary.json"])
        if not isinstance(summary, dict):
            raise ValueError("expected summary object")
        overall = summary.get("overall_status")
    except KeyError:
        overall = None
    except (ValueError, TypeError, AttributeError):
        overall = None
        issues.append("malformed summary.json")
    if not isinstance(overall, str) or overall not in {"PASS", "FAIL", "INCOMPLETE"}:
        overall = "INCOMPLETE"
    if any(record.get("exit_code") not in (None, 0) for record in records.values()):
        overall = "FAIL"
    elif (not report_current or "W15_REPORT.md" not in captured or issues or changed or skipped or
          any(record.get("exit_code") != 0 for record in records.values()) or
          any(record.get("running") is True for record in records.values()) or
          any(record.get("completed") is not None and
              record["completed"] != record.get("total") for record in records.values())):
        overall = "INCOMPLETE"
    if changed or skipped or issues:
        state = "inconsistent"
    elif any(record.get("running") is True for record in records.values()):
        state = "running"
    elif ("summary.json" not in captured or "W15_REPORT.md" not in captured or
          not report_current or any(record.get("exit_code") != 0 for record in records.values()) or
          any(record.get("completed") is not None and
              record["completed"] != record.get("total") for record in records.values())):
        state = "partial"
    else:
        state = "final"
    return state, overall, records, issues


def export_results(root: Path) -> Path:
    """Create an atomic timestamped ZIP; source files are read but never changed."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    exports = root / "exports"
    if exports.exists() and not _safe_kind(exports, root, True):
        raise ValueError("exports must be a real directory inside the result root")
    exports.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = exports / f"workstation-results-{stamp}-{uuid.uuid4().hex[:8]}.zip"
    fd, temporary = tempfile.mkstemp(prefix=".result-bundle-", suffix=".tmp", dir=exports)
    os.close(fd)
    included, missing, skipped, changed = [], [], [], []
    captured: dict[str, bytes] = {}
    stamps: dict[str, tuple] = {}
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in _candidates(root, skipped):
                name = path.relative_to(root).as_posix()
                before = _stamp(path, root)
                if before is None:
                    if path.parent == root:
                        missing.append(name)
                    elif os.path.lexists(path):
                        skipped.append({"path": name, "reason": "unsafe file"})
                    continue
                entry_started = False
                try:
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) |
                                 getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as source:
                        opened = os.fstat(source.fileno())
                        if (not _safe_kind(path, root, False) or
                                (opened.st_dev, opened.st_ino) != before[:2]):
                            changed.append(name)
                            continue
                        captured_bytes = bytearray() if name in (
                            "status.json", "batch_status.json", "summary.json", "W15_REPORT.md") else None
                        copied = 0
                        with bundle.open(name, "w", force_zip64=True) as output:
                            entry_started = True
                            for block in iter(lambda: source.read(1024 * 1024), b""):
                                output.write(block)
                                copied += len(block)
                                if captured_bytes is not None:
                                    captured_bytes.extend(block)
                except OSError as exc:
                    if entry_started:
                        included.append(name)
                    skipped.append({"path": name, "reason": (
                        "partial ZIP entry: " if entry_started else "") + type(exc).__name__})
                    continue
                after = _stamp(path, root)
                if after != before or copied != before[2]:
                    changed.append(name)
                stamps[name] = after
                included.append(name)
                if captured_bytes is not None:
                    captured[name] = bytes(captured_bytes)
            for name, previous in stamps.items():
                if _stamp(root / name, root) != previous and name not in changed:
                    changed.append(name)
            changed.sort()
            state, overall, records, issues = _state(captured, changed, skipped)
            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            manifest = {
                "schema_version": 1,
                "created_at": created_at,
                "snapshot_state": state,
                "overall_status": overall,
                "captured_status": records,
                "included_files": [*included, "manifest.json"],
                "missing_files": missing,
                "skipped_files": skipped,
                "changed_files": changed,
                "issues": issues,
            }
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, target)
        atomic_json(root / "export.json", {
            "schema_version": 1, "created_at": created_at, "path": str(target),
            "success": True, "error": None, "snapshot_state": state,
            "overall_status": overall,
        })
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
