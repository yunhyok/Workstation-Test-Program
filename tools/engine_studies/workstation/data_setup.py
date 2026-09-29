"""Discover and prepare a folder of SPD and PowerSI reference data.

The selected source folder is treated as read-only.  Converted references and
their manifests are published below ``root/prepared-data`` under a content
identity, so a later import cannot overwrite evidence from an earlier one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_SCHEMA_VERSION = 1
_TOUCHSTONE_SUFFIX = re.compile(r"\.s(\d+)p$", re.IGNORECASE)
_PORT_COMMENT = re.compile(r"^\s*!\s*Port\[(\d+)]\s*=\s*(.*?)\s*$", re.IGNORECASE)
@dataclass(frozen=True)
class _Spd:
    path: Path
    sha256: str
    ports: tuple[str, ...]
    rails: tuple[str, ...]
    family: str
    stat: tuple[int, int]


@dataclass(frozen=True)
class _Touchstone:
    path: Path
    sha256: str
    port_count: int
    labels: tuple[str, ...]
    stat: tuple[int, int]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stat(path: Path) -> tuple[int, int]:
    value = path.stat()
    return value.st_size, value.st_mtime_ns


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_immutable_json(path: Path, value: object) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise FileExistsError(f"prepared evidence differs and will not be overwritten: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != encoded:
                raise FileExistsError(f"prepared evidence appeared with different content: {path}") from None
        temporary.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def _spd_port_rails(path: Path) -> dict[str, str]:
    # This is the engine's authoritative .Port parser.  Keeping the import
    # lazy lets the public controller import this module outside the bundled
    # numerical runtime and dispatch setup to that runtime.
    from spd_pi_engine.multiport import port_rails

    return {str(port): str(rail) for port, rail in port_rails(path).items()}


def _read_spd(path: Path) -> _Spd:
    before = _stat(path)
    mapping = _spd_port_rails(path)
    if not mapping or any(not port or not rail for port, rail in mapping.items()):
        raise ValueError(f"{path}: SPD .Port section did not yield non-empty port/rail names")
    if len(set(mapping)) != len(mapping):
        raise ValueError(f"{path}: SPD contains duplicate short port names")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError(
            f"{path}: multiple SPD ports share a rail; the full diagonal Touchstone manifest is ambiguous"
        )
    # SPD/PowerSI geometry does not carry an authoritative package-vs-PCB
    # semantic field.  Size and naming heuristics would silently select the
    # wrong numerical tolerance for some valid boards, so folder setup keeps
    # the family explicitly unknown.
    digest = _sha256(path)
    after = _stat(path)
    if before != after:
        raise ValueError(f"source changed while reading SPD metadata: {path}")
    return _Spd(path, digest, tuple(mapping), tuple(mapping.values()), "unknown", after)


def _touchstone_header(path: Path) -> tuple[int, tuple[str, ...]]:
    suffix = _TOUCHSTONE_SUFFIX.search(path.name)
    declared = int(suffix.group(1)) if suffix else None
    mapping: dict[int, str] = {}
    saw_option = False
    with path.open("r", encoding="utf-8", errors="strict", newline=None) as stream:
        for raw in stream:
            stripped = raw.strip()
            matched = _PORT_COMMENT.match(raw)
            if matched:
                number, label = int(matched.group(1)), matched.group(2).strip()
                if number in mapping or not label:
                    raise ValueError(f"{path}: duplicate or empty ! Port[{number}] label")
                mapping[number] = label
            if stripped.startswith("#"):
                saw_option = True
            elif stripped.startswith("["):
                raise ValueError(f"{path}: Touchstone v2 keyword blocks are unsupported by the product parser")
            elif saw_option and stripped and not stripped.startswith("!"):
                break
    if declared is None:
        if path.suffix.casefold() != ".ts":
            raise ValueError(f"{path}: expected a .sNp or .ts Touchstone suffix")
        if not mapping:
            raise ValueError(f"{path}: .ts input needs a complete ! Port[n] header to infer its port count")
        declared = max(mapping)
    expected = set(range(1, declared + 1))
    if set(mapping) != expected:
        missing = sorted(expected.difference(mapping))
        extra = sorted(set(mapping).difference(expected))
        raise ValueError(f"{path}: Touchstone header is incomplete (missing={missing}, extra={extra})")
    return declared, tuple(mapping[index] for index in range(1, declared + 1))


def _read_touchstone(path: Path) -> _Touchstone:
    before = _stat(path)
    count, labels = _touchstone_header(path)
    digest = _sha256(path)
    after = _stat(path)
    if before != after:
        raise ValueError(f"source changed while reading Touchstone metadata: {path}")
    return _Touchstone(path, digest, count, labels, after)


def _assert_source_unchanged(path: Path, expected_stat: tuple[int, int], expected_sha256: str) -> None:
    if _stat(path) != expected_stat or _sha256(path) != expected_sha256:
        raise ValueError(f"source changed during data preparation: {path}")


def _product_rail(label: str) -> str | None:
    from spd_decap_pi._core.io.touchstone import TouchstoneError, powersi_rail_from_header_label

    try:
        return str(powersi_rail_from_header_label(label))
    except TouchstoneError:
        return None


def _matches(spd: _Spd, touchstone: _Touchstone) -> bool:
    if len(spd.ports) != touchstone.port_count:
        return False
    decoded = [_product_rail(label) or label for label in touchstone.labels]
    return len(set(decoded)) == len(decoded) and set(decoded) == set(spd.rails)


def _ordered_manifest(spd: _Spd, touchstone: _Touchstone) -> list[str]:
    rail_to_port = dict(zip(spd.rails, spd.ports))
    result = []
    for index, label in enumerate(touchstone.labels, 1):
        rail = _product_rail(label) or label
        port = rail_to_port.get(rail)
        if port is None:
            raise ValueError(f"{touchstone.path}: port {index} rail {rail!r} is absent from {spd.path}")
        result.append(f"{port}::{rail}")
    return result


def _basename_affinity(spd: Path, touchstone: Path) -> bool:
    left, right = spd.stem.casefold(), touchstone.stem.casefold()
    if left == right:
        return True
    separators = "_- ."
    return ((right.startswith(left) and right[len(left):len(left) + 1] in separators) or
            (left.startswith(right) and left[len(right):len(right) + 1] in separators))


def _pair(spds: list[_Spd], touchstones: list[_Touchstone]) -> list[tuple[_Spd, _Touchstone]]:
    candidates = {spd: [item for item in touchstones if _matches(spd, item)] for spd in spds}
    pairs: list[tuple[_Spd, _Touchstone]] = []
    remaining = set(touchstones)
    pending = set(spds)
    while pending:
        choices: dict[_Spd, list[_Touchstone]] = {}
        for spd in pending:
            compatible = [item for item in candidates[spd] if item in remaining]
            if len(compatible) > 1:
                named = [item for item in compatible if _basename_affinity(spd.path, item.path)]
                if len(named) == 1:
                    compatible = named
            choices[spd] = compatible
        selectable = sorted(
            (spd for spd, compatible in choices.items() if len(compatible) == 1),
            key=lambda item: str(item.path).casefold(),
        )
        if not selectable:
            spd = min(pending, key=lambda item: str(item.path).casefold())
            compatible = choices[spd]
            listed = ", ".join(str(item.path) for item in compatible) or "none"
            raise ValueError(
                f"{spd.path}: expected one Touchstone file with the exact ordered port/rail manifest; "
                f"found {len(compatible)} ({listed})"
            )
        spd = selectable[0]
        chosen = choices[spd][0]
        remaining.remove(chosen)
        pending.remove(spd)
        pairs.append((spd, chosen))
    if remaining:
        raise ValueError("unmatched Touchstone files: " + ", ".join(str(item.path) for item in sorted(remaining, key=lambda x: str(x.path))))
    return sorted(pairs, key=lambda pair: str(pair[0].path).casefold())


def _existing_against(npz_paths: Iterable[Path], names: list[str], spd: Path, touchstone: Path) -> Path | None:
    import numpy as np

    matches: list[Path] = []
    for path in npz_paths:
        try:
            with np.load(path, allow_pickle=False) as data:
                required = {"freq", "port_names", "Zdiag"}
                if not required.issubset(data.files):
                    continue
                actual = [str(item) for item in data["port_names"]]
                freq = data["freq"]
                zdiag = data["Zdiag"]
                if actual == names and freq.ndim == 1 and zdiag.shape == (len(freq), len(names)):
                    matches.append(path)
        except (OSError, ValueError):
            continue
    affine = [path for path in matches if _basename_affinity(spd, path) or _basename_affinity(touchstone, path)]
    return affine[0] if len(affine) == 1 else None


def _source_for_converter(source: Path, port_count: int, directory: Path, source_hash: str) -> Path:
    if _TOUCHSTONE_SUFFIX.search(source.name):
        return source
    alias = directory / f"touchstone-{source_hash[:12]}.s{port_count}p"
    if alias.exists():
        if _sha256(alias) != source_hash:
            raise FileExistsError(f"prepared Touchstone alias differs and will not be overwritten: {alias}")
        return alias
    try:
        os.link(source, alias)
    except OSError:
        temporary = alias.with_name(f".{alias.name}.{os.getpid()}.tmp")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=8 * 1024 * 1024)
                writer.flush(); os.fsync(writer.fileno())
            try:
                os.link(temporary, alias)
            except FileExistsError:
                if _sha256(alias) != source_hash:
                    raise FileExistsError(f"prepared Touchstone alias appeared with different content: {alias}") from None
            temporary.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
    return alias


def _quarantine_orphan(path: Path) -> Path:
    digest = _sha256(path)[:12]
    for index in range(100_000):
        suffix = f"-{index:03d}" if index else ""
        candidate = path.with_name(f"{path.stem}.orphan-{digest}{suffix}{path.suffix}")
        if candidate.exists():
            continue
        path.rename(candidate)
        return candidate
    raise RuntimeError(f"could not preserve orphaned prepared reference beside {path}")


def _convert(source: Path, output: Path, manifest: Path, against: Path | None) -> dict[str, object]:
    try:
        from .touchstone_to_zdiag import convert_touchstone
    except ImportError:  # engine_worker imports workstation modules as top-level files
        from touchstone_to_zdiag import convert_touchstone

    return convert_touchstone(source, output, port_manifest=manifest, against=against)


def _discover(data_dir: Path, excluded: Path | None = None) -> tuple[list[Path], list[Path], list[Path]]:
    def admitted(path: Path) -> bool:
        return path.is_file() and (excluded is None or not path.resolve().is_relative_to(excluded))

    files = sorted((path for path in data_dir.rglob("*") if admitted(path)), key=lambda p: str(p).casefold())
    spds = [path for path in files if path.suffix.casefold() == ".spd"]
    touchstones = [path for path in files if path.suffix.casefold() == ".ts" or _TOUCHSTONE_SUFFIX.search(path.name)]
    npzs = [path for path in files if path.suffix.casefold() == ".npz"]
    if not spds:
        raise ValueError(f"{data_dir}: no .spd files found recursively")
    if not touchstones:
        raise ValueError(f"{data_dir}: no .sNp or .ts Touchstone files found recursively")
    return spds, touchstones, npzs


def prepare_data(data_dir: Path, root: Path) -> dict:
    """Prepare references and return values suitable for merging into config.

    The result contains absolute paths.  ``data_dir`` continues to point at
    the selected source folder; only generated NPZ references and immutable
    setup evidence live below ``root/prepared-data``.
    """
    data_dir = Path(data_dir).expanduser().resolve()
    root = Path(root).expanduser().resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"data folder does not exist: {data_dir}")
    prepared_root = root / "prepared-data"
    prepared_root.mkdir(parents=True, exist_ok=True)
    spd_paths, touchstone_paths, npz_paths = _discover(data_dir, prepared_root.resolve())
    spds = [_read_spd(path) for path in spd_paths]
    touchstones = [_read_touchstone(path) for path in touchstone_paths]
    pairs = _pair(spds, touchstones)

    designs: dict[str, dict[str, object]] = {}
    metadata_designs: dict[str, dict[str, object]] = {}
    all_ports: list[tuple[str, str]] = []
    for spd, touchstone in pairs:
        manifest_names = _ordered_manifest(spd, touchstone)
        identity_document = {
            "schema_version": _SCHEMA_VERSION,
            "spd_sha256": spd.sha256,
            "touchstone_sha256": touchstone.sha256,
            "family": spd.family,
            "port_names": manifest_names,
        }
        identity = hashlib.sha256(_canonical(identity_document)).hexdigest()
        design_id = f"design_{identity[:12]}"
        directory = prepared_root / design_id
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / "port-manifest.json"
        _write_immutable_json(manifest, {"port_names": manifest_names})
        output = directory / "reference.npz"
        receipt_path = directory / "setup.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt.get("identity") != identity or not output.is_file() or receipt.get("reference_sha256") != _sha256(output):
                raise ValueError(f"prepared evidence is incomplete or corrupt: {directory}")
            conversion = receipt.get("conversion")
        else:
            if output.exists():
                _quarantine_orphan(output)
            source = _source_for_converter(touchstone.path, touchstone.port_count, directory, touchstone.sha256)
            against = _existing_against(npz_paths, manifest_names, spd.path, touchstone.path)
            conversion = _convert(source, output, manifest, against)
            _assert_source_unchanged(spd.path, spd.stat, spd.sha256)
            _assert_source_unchanged(touchstone.path, touchstone.stat, touchstone.sha256)
            receipt = {
                "schema_version": _SCHEMA_VERSION,
                "identity": identity,
                "identity_document": identity_document,
                "source_spd": str(spd.path),
                "source_touchstone": str(touchstone.path),
                "reference": str(output),
                "reference_sha256": _sha256(output),
                "family_method": "not-declared-by-source",
                "conversion": conversion,
                "existing_npz_gate": str(against) if against else None,
            }
            _write_immutable_json(receipt_path, receipt)
        designs[design_id] = {
            "spd": str(spd.path),
            "reference": str(output),
            "family": spd.family,
            "ports": list(spd.ports),
        }
        metadata_designs[design_id] = {
            "identity": identity,
            "spd_sha256": spd.sha256,
            "touchstone_sha256": touchstone.sha256,
            "source_touchstone": str(touchstone.path),
            "manifest": str(manifest),
            "setup_receipt": str(receipt_path),
            "family_method": "not-declared-by-source",
            "conversion": conversion,
        }
        all_ports.extend((design_id, port) for port in spd.ports)

    for spd, touchstone in pairs:
        _assert_source_unchanged(spd.path, spd.stat, spd.sha256)
        _assert_source_unchanged(touchstone.path, touchstone.stat, touchstone.sha256)

    def selected(limit: int | None) -> dict[str, list[str] | str]:
        chosen = all_ports if limit is None else all_ports[:limit]
        result: dict[str, list[str] | str] = {}
        for design_id, port in chosen:
            result.setdefault(design_id, [])  # type: ignore[arg-type]
            assert isinstance(result[design_id], list)
            result[design_id].append(port)
        return result

    p92 = {design_id: "all" for design_id in designs}
    port_sets = {"P9": selected(9), "P20": selected(20), "P92": p92, "E4": selected(1)}
    return {
        "data_dir": str(data_dir),
        "designs": designs,
        "port_sets": port_sets,
        "metadata": {
            "schema_version": _SCHEMA_VERSION,
            "mode": "generated-from-selected-data",
            "claim": "generated convenience subsets; not the preregistered canonical plan",
            "selection": {
                "order": "design path casefold order, then authoritative SPD .Port order",
                "P9": "first up to 9 ports overall",
                "P20": "first up to 20 ports overall",
                "P92": "all ports of every discovered design",
                "E4": "first available port",
            },
            "prepared_root": str(prepared_root),
            "designs": metadata_designs,
        },
    }
