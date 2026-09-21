"""User-facing configuration and release naming regressions."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_example_json_parses_and_uses_forward_slash_paths():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    for field in ("engine_python", "engine_root", "data_dir", "laptop_receipts", "laptop_freeze"):
        if example[field] is None:
            assert field in ("laptop_receipts", "laptop_freeze")
            continue
        assert "\\" not in example[field], field
        assert "/" in example[field], field


def test_example_port_sets_only_reference_provisioned_designs():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["designs"]
    assert {"P9", "P20", "P92", "E4"}.issubset(example["port_sets"])
    for group in example["port_sets"].values():
        for design, ports in group.items():
            assert design in example["designs"]
            assert ports == "all" or (isinstance(ports, list) and ports)


def test_example_does_not_configure_optional_laptop_artifacts():
    value = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert value.get("laptop_receipts") is None
    assert value.get("laptop_freeze") is None


def test_program_validation_summary_has_distinct_name():
    assert not (ROOT / "summary.json").exists()
    report = json.loads((ROOT / "program_validation_summary.json").read_text(encoding="utf-8"))
    assert report["kind"] == "program_release_validation"
