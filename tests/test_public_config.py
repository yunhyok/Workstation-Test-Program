from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = {
    "README.md",
    "config.example.json",
    "tools/engine_studies/workstation/README.md",
}
CODE_SUFFIXES = {".py", ".ps1", ".iss", ".spec", ".yml", ".yaml"}

# Detect classes of private identifiers without embedding any particular board
# name or port from the owner's data.  Generic public examples such as
# ``design_a`` and ``PortA`` remain valid.
PRIVATE_IDENTIFIER_PATTERNS = {
    "numeric design label": re.compile(r"(?<![\d-])2\d{5}(?![\d-])"),
    "compact board family": re.compile(r"\b[Ss]\d+[Mm]\d+\b"),
    "numbered physical port": re.compile(r"\bPort\d+_(?:SITE\d+|U\d+(?:_\d+)*)\b", re.IGNORECASE),
    "board-like source filename": re.compile(r"\b[Ss]\d[A-Za-z0-9_-]*_\d{6}[A-Za-z0-9_.-]*"),
}


def _public_source_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        relative = Path(line)
        posix = relative.as_posix()
        if relative.suffix.lower() in CODE_SUFFIXES or posix in PUBLIC_DOCS:
            paths.append(ROOT / relative)
    return paths


def test_public_source_has_no_private_board_or_port_identifiers():
    findings = []
    for path in _public_source_paths():
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in PRIVATE_IDENTIFIER_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path.relative_to(ROOT).as_posix()}:{line}: {label}")
    assert not findings, "private identifiers remain in public source:\n" + "\n".join(findings)


def test_example_config_is_valid_json():
    value = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
