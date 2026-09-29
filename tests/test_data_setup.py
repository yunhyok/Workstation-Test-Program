from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tools.engine_studies.workstation import data_setup


class DataSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.inputs = self.base / "inputs"
        self.root = self.base / "study"
        self.inputs.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_touchstone(path: Path, rails: list[str]) -> None:
        lines = [f"! Port[{index}] = SITE{rail[-1]}_RUN-{rail}"
                 for index, rail in enumerate(rails, 1)]
        lines.extend(["# Hz S RI R 50", "1 " + "0 0 " * (len(rails) ** 2)])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _fake_convert(calls: list[tuple[Path, list[str]]]):
        def convert(source: Path, output: Path, manifest: Path, against: Path | None):
            names = json.loads(manifest.read_text(encoding="utf-8"))["port_names"]
            calls.append((source, names))
            output.parent.mkdir(parents=True, exist_ok=True)
            np.savez(
                output,
                freq=np.asarray([1.0]),
                port_names=np.asarray(names, dtype=np.str_),
                Zdiag=np.zeros((1, len(names)), dtype=np.complex128),
            )
            return {"port_count": len(names), "verified_against": str(against) if against else None}
        return convert

    def _patch_runtime(self, mappings, calls):
        def rails(path: Path):
            return mappings[path.name]

        def parsed(label: str):
            return label.split("-", 1)[1] if label.startswith("SITE") and "-" in label else None

        return mock.patch.multiple(
            data_setup,
            _spd_port_rails=rails,
            _product_rail=parsed,
            _convert=self._fake_convert(calls),
        )

    def test_prepare_data_uses_exact_spd_port_to_rail_manifest_and_deterministic_sets(self):
        spd = self.inputs / "design_a.spd"
        touchstone = self.inputs / "design_a_export.s3p"
        spd.write_text("source", encoding="utf-8")
        rails = ["VDD_A/0", "VDD_B/1", "VDD_C/0"]
        self._write_touchstone(touchstone, rails)
        mappings = {spd.name: {"PortA": rails[0], "PortB": rails[1], "PortC": rails[2]}}
        calls = []
        with self._patch_runtime(mappings, calls):
            result = data_setup.prepare_data(self.inputs, self.root)

        self.assertEqual(result["data_dir"], str(self.inputs.resolve()))
        self.assertEqual(len(result["designs"]), 1)
        design_id, design = next(iter(result["designs"].items()))
        self.assertEqual(design["family"], "unknown")
        self.assertEqual(design["ports"], ["PortA", "PortB", "PortC"])
        self.assertTrue(Path(design["reference"]).is_relative_to((self.root / "prepared-data").resolve()))
        self.assertEqual(calls[0][1], [
            "PortA::VDD_A/0", "PortB::VDD_B/1", "PortC::VDD_C/0",
        ])
        self.assertEqual(result["port_sets"]["P9"], {design_id: ["PortA", "PortB", "PortC"]})
        self.assertEqual(result["port_sets"]["P20"], result["port_sets"]["P9"])
        self.assertEqual(result["port_sets"]["P92"], {design_id: "all"})
        self.assertEqual(result["port_sets"]["E4"], {design_id: ["PortA"]})
        self.assertIn("not the preregistered", result["metadata"]["claim"])

    def test_ts_alias_gets_inferred_snp_suffix_and_is_reused_without_overwrite(self):
        spd = self.inputs / "design_a.spd"
        touchstone = self.inputs / "design_a.ts"
        spd.write_text("source", encoding="utf-8")
        rails = ["VDD_A/0", "VDD_B/1"]
        self._write_touchstone(touchstone, rails)
        mappings = {spd.name: {"PortA": rails[0], "PortB": rails[1]}}
        calls = []
        with self._patch_runtime(mappings, calls):
            first = data_setup.prepare_data(self.inputs, self.root)
            second = data_setup.prepare_data(self.inputs, self.root)

        self.assertEqual(first["designs"], second["designs"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].suffix.casefold(), ".s2p")
        self.assertEqual(calls[0][0].read_bytes(), touchstone.read_bytes())

    def test_unreceipted_reference_is_quarantined_then_regenerated(self):
        spd = self.inputs / "design_a.spd"
        touchstone = self.inputs / "design_a.s2p"
        spd.write_text("source", encoding="utf-8")
        rails = ["VDD_A/0", "VDD_B/1"]
        self._write_touchstone(touchstone, rails)
        mappings = {spd.name: {"PortA": rails[0], "PortB": rails[1]}}
        calls = []
        with self._patch_runtime(mappings, calls):
            first = data_setup.prepare_data(self.inputs, self.root)
            design = next(iter(first["designs"].values()))
            reference = Path(design["reference"])
            reference.with_name("setup.json").unlink()
            data_setup.prepare_data(self.inputs, self.root)
        self.assertEqual(len(calls), 2)
        self.assertTrue(reference.is_file())
        self.assertEqual(len(list(reference.parent.glob("reference.orphan-*.npz"))), 1)

    def test_source_timestamp_change_during_conversion_is_rejected(self):
        spd = self.inputs / "design_a.spd"
        touchstone = self.inputs / "design_a.s2p"
        spd.write_text("source", encoding="utf-8")
        rails = ["VDD_A/0", "VDD_B/1"]
        self._write_touchstone(touchstone, rails)
        mappings = {spd.name: {"PortA": rails[0], "PortB": rails[1]}}
        calls = []
        normal = self._fake_convert(calls)

        def changing_convert(source, output, manifest, against):
            result = normal(source, output, manifest, against)
            current = spd.stat().st_mtime_ns
            os.utime(spd, ns=(current + 1_000_000_000, current + 1_000_000_000))
            return result

        with mock.patch.multiple(
            data_setup,
            _spd_port_rails=lambda path: mappings[path.name],
            _product_rail=lambda label: label.split("-", 1)[1],
            _convert=changing_convert,
        ):
            with self.assertRaisesRegex(ValueError, "source changed during data preparation"):
                data_setup.prepare_data(self.inputs, self.root)
        self.assertFalse(any((self.root / "prepared-data").rglob("setup.json")))

    def test_rejects_ambiguous_exact_manifest_matches(self):
        spd_a = self.inputs / "alpha.spd"
        spd_b = self.inputs / "beta.spd"
        spd_a.write_text("a", encoding="utf-8")
        spd_b.write_text("b", encoding="utf-8")
        rails = ["VDD_A/0", "VDD_B/1"]
        self._write_touchstone(self.inputs / "first.s2p", rails)
        self._write_touchstone(self.inputs / "second.s2p", rails)
        mappings = {
            spd_a.name: {"PortA": rails[0], "PortB": rails[1]},
            spd_b.name: {"PortA": rails[0], "PortB": rails[1]},
        }
        with self._patch_runtime(mappings, []):
            with self.assertRaisesRegex(ValueError, "expected one Touchstone file.*found 2"):
                data_setup.prepare_data(self.inputs, self.root)

    def test_rejects_port_rail_mismatch_instead_of_pairing_by_basename(self):
        spd = self.inputs / "design_a.spd"
        spd.write_text("source", encoding="utf-8")
        self._write_touchstone(self.inputs / "design_a.s2p", ["VDD_A/0", "WRONG/1"])
        mappings = {spd.name: {"PortA": "VDD_A/0", "PortB": "VDD_B/1"}}
        with self._patch_runtime(mappings, []):
            with self.assertRaisesRegex(ValueError, "exact ordered port/rail manifest; found 0"):
                data_setup.prepare_data(self.inputs, self.root)

    def test_touchstone_permutation_drives_manifest_column_order(self):
        spd = self.inputs / "design_a.spd"
        touchstone = self.inputs / "design_a.s3p"
        spd.write_text("source", encoding="utf-8")
        self._write_touchstone(touchstone, ["VDD_C/0", "VDD_A/0", "VDD_B/1"])
        mappings = {spd.name: {
            "PortA": "VDD_A/0", "PortB": "VDD_B/1", "PortC": "VDD_C/0",
        }}
        calls = []
        with self._patch_runtime(mappings, calls):
            data_setup.prepare_data(self.inputs, self.root)
        self.assertEqual(calls[0][1], [
            "PortC::VDD_C/0", "PortA::VDD_A/0", "PortB::VDD_B/1",
        ])

    def test_incomplete_ts_header_reports_missing_ports(self):
        source = self.inputs / "broken.ts"
        source.write_text("! Port[1] = VDD_A/0\n! Port[3] = VDD_C/0\n# Hz S RI R 50\n1 0 0\n")
        with self.assertRaisesRegex(ValueError, r"missing=\[2\]"):
            data_setup._touchstone_header(source)


if __name__ == "__main__":
    unittest.main()
