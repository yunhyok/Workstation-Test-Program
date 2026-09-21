"""Convert a PowerSI Touchstone export to the engine's Zdiag NPZ format.

The actual parser and S-to-Z conversion come from the shipping product.  This
wrapper only validates complete PowerSI labels, selects the diagonal, and
writes ``freq``, ``port_names``, and ``Zdiag``.  It refuses to overwrite an
existing output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

import numpy as np

from spd_decap_pi._core.io.touchstone import (
    TouchstoneError,
    powersi_rail_from_header_label,
    read_touchstone,
    s_to_z,
)


def _header_rails(network, expected_names: Sequence[str] | None = None) -> list[str]:
    ports = int(network.s_parameters.shape[1])
    mapping = network.port_mapping
    if set(mapping) != set(range(1, ports + 1)):
        raise ValueError("Touchstone header must contain exactly one ! Port[n] label for every port")
    rails: list[str] = []
    for index in range(1, ports + 1):
        label = mapping[index]
        try:
            rail = powersi_rail_from_header_label(label)
        except TouchstoneError:
            # Some older PCB exports contain the bare rail in Port[n], rather
            # than PowerSI's SITE-qualified package spelling.  Admit that
            # legacy form only when an explicit artifact/manifest supplies the
            # expected exact rail; a bare label is never guessed on its own.
            expected = None
            if expected_names is not None and index <= len(expected_names):
                candidate = expected_names[index - 1]
                if "::" in candidate:
                    expected = candidate.rsplit("::", 1)[1]
            if expected is None or label != expected:
                raise ValueError(
                    f"unsupported PowerSI header label at port {index}; provide an exact "
                    "port manifest or --against NPZ for a legacy bare-rail export"
                )
            rail = expected
        rails.append(rail)
    if len(set(rails)) != len(rails):
        raise ValueError("PowerSI header resolves to duplicate rail names")
    return rails


def _read_port_names(path: Path, rails: Sequence[str]) -> list[str]:
    """Read exact SPD port names from JSON or one-name-per-line text.

    Each name may be a short SPD port name or ``short::rail``.  In the latter
    form the rail suffix is checked against the Touchstone header.  A plain
    short-name manifest is useful when the SPD naming convention cannot be
    reconstructed from a PowerSI rail label (for example ``Port5_SITE0_1721``).
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix.casefold() == ".json":
        value = json.loads(text)
        if isinstance(value, dict):
            value = value.get("port_names")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("port manifest JSON must be an array or contain a port_names array")
        names = [item.strip() for item in value]
    else:
        names = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(names) != len(rails) or any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("port manifest must contain one unique non-empty name per Touchstone port")
    for index, (name, rail) in enumerate(zip(names, rails), 1):
        if "::" in name and name.rsplit("::", 1)[1] != rail:
            raise ValueError(f"port manifest rail mismatch at port {index}: expected {rail!r}")
    return names


def _against_raw_names(path: Path) -> list[str]:
    with np.load(path, allow_pickle=True) as expected:
        if "port_names" not in expected:
            raise ValueError("against NPZ has no port_names")
        return [str(item) for item in expected["port_names"]]


def _against_names(path: Path, rails: Sequence[str], names: Sequence[str] | None = None) -> list[str]:
    """Use an existing verified NPZ only as an explicit exact-name manifest."""
    names = list(names) if names is not None else _against_raw_names(path)
    if len(names) != len(rails):
        raise ValueError("against NPZ port count differs")
    for index, (name, rail) in enumerate(zip(names, rails), 1):
        encoded = name.rsplit("::", 1)[-1]
        if encoded != rail:
            raise ValueError(f"against NPZ rail mismatch at port {index}: {encoded!r} != {rail!r}")
    return names


def _validate_against(freq: np.ndarray, names: Sequence[str], zdiag: np.ndarray, path: Path) -> None:
    with np.load(path, allow_pickle=True) as expected:
        required = {"freq", "port_names", "Zdiag"}
        if not required.issubset(expected.files):
            raise ValueError(f"against NPZ is missing {sorted(required.difference(expected.files))}")
        expected_freq = np.asarray(expected["freq"], dtype=np.float64)
        expected_names = [str(item) for item in expected["port_names"]]
        expected_zdiag = np.asarray(expected["Zdiag"], dtype=np.complex128)
    if freq.shape != expected_freq.shape or not np.allclose(freq, expected_freq, rtol=1e-12, atol=0.0):
        raise ValueError("frequency array does not reproduce the against NPZ at rtol=1e-12")
    if list(names) != expected_names:
        raise ValueError("port_names do not reproduce the against NPZ")
    if zdiag.shape != expected_zdiag.shape or not np.allclose(zdiag, expected_zdiag, rtol=1e-12, atol=0.0):
        delta = None
        if zdiag.shape == expected_zdiag.shape:
            denom = np.maximum(np.abs(expected_zdiag), np.finfo(float).tiny)
            delta = float(np.max(np.abs(zdiag - expected_zdiag) / denom))
        detail = "" if delta is None else f" (max relative error {delta:.6g})"
        raise ValueError("Zdiag does not reproduce the against NPZ at rtol=1e-12" + detail)


def convert_touchstone(
    source: Path,
    output: Path,
    *,
    against: Path | None = None,
    port_manifest: Path | None = None,
) -> dict[str, object]:
    """Convert one file and return a small, non-numerical receipt.

    ``port_manifest`` supplies exact SPD port names.  If it is omitted and an
    ``against`` NPZ is provided, the existing names are admitted only after
    their rail suffixes match every parsed PowerSI header label.  With neither,
    the canonical rail names are stored; this is useful for new designs whose
    consumer identifies ports by rail.
    """
    source = Path(source); output = Path(output)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.resolve() == output.resolve():
        raise ValueError("source and output paths must differ")
    network = read_touchstone(source)
    against_names = _against_raw_names(Path(against)) if against is not None else None
    manifest_names = None
    if port_manifest is not None:
        # First read with provisional labels only to make exact legacy bare
        # rails available to the guarded parser.  Final validation follows.
        provisional = ["" for _ in range(network.s_parameters.shape[1])]
        text = Path(port_manifest).read_text(encoding="utf-8")
        if Path(port_manifest).suffix.casefold() == ".json":
            value = json.loads(text)
            if isinstance(value, dict):
                value = value.get("port_names")
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                provisional = [item.strip() for item in value]
        else:
            provisional = [line.strip() for line in text.splitlines()
                           if line.strip() and not line.lstrip().startswith("#")]
        manifest_names = provisional
    rails = _header_rails(network, manifest_names or against_names)
    converted = s_to_z(network)
    z = np.asarray(converted.z_parameters, dtype=np.complex128)
    zdiag = np.diagonal(z, axis1=1, axis2=2).copy()
    freq = np.asarray(network.frequencies_hz, dtype=np.float64).copy()
    if port_manifest is not None:
        names = _read_port_names(Path(port_manifest), rails)
    elif against is not None:
        names = _against_names(Path(against), rails, against_names)
    else:
        names = rails
    if against is not None:
        _validate_against(freq, names, zdiag, Path(against))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Use a sibling temporary and replace only after np.savez closes it.  The
    # explicit existence check above is the no-overwrite contract; the replace
    # only makes publishing a newly created file atomic.
    with tempfile.NamedTemporaryFile(prefix=f".{output.name}.", suffix=".npz", dir=output.parent,
                                     delete=False) as handle:
        temporary = Path(handle.name)
    try:
        np.savez(temporary, freq=freq, port_names=np.asarray(names, dtype=np.str_), Zdiag=zdiag)
        try:
            os.link(temporary, output)
        except FileExistsError:
            raise FileExistsError(f"output appeared during conversion; refusing overwrite: {output}") from None
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "source": str(source.resolve()), "output": str(output.resolve()),
        "frequency_count": int(freq.size), "port_count": int(len(names)),
        "verified_against": str(Path(against).resolve()) if against is not None else None,
        "verified": against is not None,
    }


def self_check() -> dict[str, object]:
    """Exercise the product parser/converter on a known nonreciprocal 2-port."""
    with tempfile.TemporaryDirectory(prefix="touchstone-zdiag-") as directory:
        root = Path(directory)
        source = root / "synthetic.s2p"
        output = root / "synthetic.npz"
        expected = root / "expected.npz"
        source.write_text(
            "! Port[1] = 2nd_SITE0-VDD_A/0\n"
            "! Port[2] = 2nd_SITE1-VDD_B/1\n"
            "# Hz S RI R 50\n"
            # Touchstone v1 2-port order is S11,S21,S12,S22.
            "1000000 0.5 0 0.1 0 0.2 0 0 0\n",
            encoding="utf-8",
        )
        network = read_touchstone(source)
        result = s_to_z(network)
        diag = np.diagonal(result.z_parameters, axis1=1, axis2=2)
        np.savez(expected, freq=network.frequencies_hz,
                 port_names=np.asarray(["VDD_A/0", "VDD_B/1"], dtype=np.str_), Zdiag=diag)
        receipt = convert_touchstone(source, output, against=expected)
        with np.load(output, allow_pickle=False) as actual:
            assert actual.files == ["freq", "port_names", "Zdiag"]
            assert actual["Zdiag"].shape == (1, 2)
            assert np.allclose(actual["Zdiag"], diag, rtol=1e-12, atol=0.0)
        receipt["self_check"] = "PASS"
        return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, help="input .sNp")
    parser.add_argument("output", nargs="?", type=Path, help="new output .npz")
    parser.add_argument("--against", type=Path, help="existing Zdiag NPZ reproduction gate")
    parser.add_argument("--port-manifest", type=Path, help="JSON or text list of exact SPD port names")
    parser.add_argument("--self-check", action="store_true", help="run the data-free synthetic 2-port check")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.self_check:
            if args.source is not None or args.output is not None:
                raise ValueError("--self-check does not accept source/output")
            result = self_check()
        else:
            if args.source is None or args.output is None:
                raise ValueError("source and output are required unless --self-check is used")
            result = convert_touchstone(args.source, args.output, against=args.against,
                                        port_manifest=args.port_manifest)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        return 0
    except (FileExistsError, FileNotFoundError, OSError, UnicodeError, ValueError,
            TouchstoneError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["convert_touchstone", "self_check"]
