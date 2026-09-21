"""Source and frozen entry point for Workstation Test Program."""
from __future__ import annotations

import os
from pathlib import Path
import sys

VERSION = "1.0.0"
BASE = Path(__file__).resolve().parent
SCRIPTS = BASE / "tools" / "engine_studies" / "workstation"
sys.path.insert(0, str(SCRIPTS))


def main() -> int:
    args = sys.argv[1:]
    if args == ["--version"]:
        print(VERSION)
        return 0
    command = args.pop(0) if args else "gui"
    sys.argv = [sys.argv[0], *args]
    if command == "gui":
        from workstation_gui import main as run
    elif command == "validate":
        from ws_validate import main as run
    elif command == "agent":
        from ws_agent import main as run
    elif command == "ctl":
        from ws_ctl import main as run
    elif command in ("--help", "-h"):
        print("Workstation Test Program " + VERSION)
        print("Usage: WorkstationTest.exe {gui|validate|agent|ctl} [options]")
        print("Numerical work uses the separately configured Python 3.12.10 engine.")
        return 0
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 2
    return run() or 0


if __name__ == "__main__":
    # Windowed bootloaders have no console streams. Never make CLI output depend on this.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    raise SystemExit(main())
