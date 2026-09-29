"""Build a relocatable, isolated numerical runtime from the pinned build Python.

Only named distributions (and their declared dependencies) are copied. The engine
is supplied by its owner at build time; proprietary sources never enter Git.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

BUNDLE_SCHEMA_VERSION = 2
ENGINE_DISTRIBUTION = "spd-decap-pi-evaluator"
EXPECTED_ENGINE_VERSION = "0.23.1"
ENGINE_PACKAGES = ("spd_pi_engine", "spd_decap_pi")

PINNED = {
    "numpy": "2.4.4", "scipy": "1.18.0", "matplotlib": "3.10.9",
    "shapely": "2.1.2", "pydantic": "2.13.3", "threadpoolctl": "3.6.0",
    "httpx": "0.28.1", "openpyxl": "3.1.5", "xlsxwriter": "3.2.9",
    "nvmath-python": "1.0.0", "cupy-cuda12x": "14.2.0",
    "cuda-bindings": "12.9.8", "cuda-core": "1.2.0",
    "nvidia-cudss-cu12": "0.8.0.10", "nvidia-cublas-cu12": "12.9.2.10",
    "nvidia-cuda-nvrtc-cu12": "12.9.86", "nvidia-cuda-runtime-cu12": "12.9.79",
    "nvidia-cufft-cu12": "11.4.1.4", "nvidia-curand-cu12": "10.3.10.19",
    "nvidia-cusolver-cu12": "11.7.5.82", "nvidia-cusparse-cu12": "12.5.10.65",
    "nvidia-nvjitlink-cu12": "12.9.86",
}


_VERIFY_CODE = r"""
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["WORKSTATION_RUNTIME_ROOT"]).resolve()
manifest = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))
if manifest.get("schema_version") != 2:
    raise RuntimeError("unsupported runtime manifest schema")
if not sys.flags.isolated:
    raise RuntimeError("runtime Python is not isolated")
if Path(sys.executable).resolve().parent != root or Path(sys.prefix).resolve() != root:
    raise RuntimeError(f"runtime executable/prefix escaped selected root: {sys.executable} / {sys.prefix}")
escaped = [entry for entry in sys.path if entry and not Path(entry).resolve().is_relative_to(root)]
if escaped:
    raise RuntimeError(f"runtime sys.path escaped selected root: {escaped}")

modules = (
    "numpy", "scipy", "matplotlib", "shapely", "pydantic", "threadpoolctl",
    "httpx", "openpyxl", "xlsxwriter", "nvmath", "nvmath.bindings.cudss",
    "cupy", "cuda.bindings", "spd_pi_engine", "spd_decap_pi",
)
origins = {}
for name in modules:
    module = importlib.import_module(name)
    origin = getattr(module, "__file__", None)
    if origin and not Path(origin).resolve().is_relative_to(root):
        raise RuntimeError(f"{name} imported outside selected runtime: {origin}")
    origins[name] = origin
for name, expected in manifest.get("packages", {}).items():
    actual = metadata.version(name)
    if actual != expected:
        raise RuntimeError(f"{name} version mismatch: expected {expected}, found {actual}")
engine = manifest.get("engine", {})
if metadata.version(engine.get("distribution", "")) != engine.get("version"):
    raise RuntimeError("engine distribution version differs from runtime manifest")
print(json.dumps({"ok": True, "isolated": True, "root": str(root), "imports": origins}, sort_keys=True))
"""


def distributions():
    found = {}
    pending = list(PINNED)
    while pending:
        name = canonicalize_name(pending.pop())
        if name in found:
            continue
        dist = metadata.distribution(name)
        expected = PINNED.get(name)
        if expected and dist.version != expected:
            raise RuntimeError(f"{name} must be {expected}, found {dist.version}")
        found[name] = dist
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                dependency = metadata.distribution(req.name)
                if req.specifier and dependency.version not in req.specifier:
                    raise RuntimeError(f"Unsatisfied runtime requirement: {req}")
                pending.append(req.name)
    return found


def _engine_version(engine: Path) -> str:
    pyproject = engine / "pyproject.toml"
    try:
        with pyproject.open("rb") as stream:
            project = tomllib.load(stream).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"Cannot read engine version from {pyproject}: {exc}") from exc
    name = canonicalize_name(str(project.get("name", "")))
    version = str(project.get("version", ""))
    if name != canonicalize_name(ENGINE_DISTRIBUTION) or not version:
        raise RuntimeError(f"{pyproject} must declare project.name={ENGINE_DISTRIBUTION!r} and a version")
    if version != EXPECTED_ENGINE_VERSION:
        raise RuntimeError(
            f"{ENGINE_DISTRIBUTION} must be {EXPECTED_ENGINE_VERSION}, found {version} in {pyproject}")
    return version


def _engine_digest(engine: Path) -> str:
    digest = hashlib.sha256()
    sources: list[Path] = []
    for name in ENGINE_PACKAGES:
        package = engine / "src" / name
        if not (package / "__init__.py").is_file():
            raise RuntimeError(f"Missing engine package: {package}")
        sources.extend(path for path in package.rglob("*")
                       if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    license_path = engine / "LICENSE.txt"
    if not license_path.is_file():
        raise RuntimeError(f"Missing engine license: {license_path}")
    sources.append(license_path)
    for source in sorted(sources, key=lambda path: path.relative_to(engine).as_posix()):
        relative = source.relative_to(engine).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big")); digest.update(relative)
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def verify_runtime(output: Path) -> None:
    output = output.resolve()
    python = output / "python.exe"
    manifest = output / "runtime-manifest.json"
    if not python.is_file() or not manifest.is_file():
        raise RuntimeError(f"Incomplete runtime at {output}: python.exe and runtime-manifest.json are required")
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid runtime manifest {manifest}: {exc}") from exc
    if document.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise RuntimeError(
            f"Runtime manifest schema must be {BUNDLE_SCHEMA_VERSION}, found {document.get('schema_version')!r}")
    with tempfile.TemporaryDirectory(prefix="workstation-hostile-python-") as raw:
        hostile = Path(raw)
        (hostile / "sitecustomize.py").write_text(
            "raise RuntimeError('host sitecustomize was imported')\n", encoding="utf-8")
        (hostile / "numpy.py").write_text(
            "raise RuntimeError('host PYTHONPATH shadowed runtime NumPy')\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(PYTHONHOME=str(hostile), PYTHONPATH=str(hostile), PYTHONUSERBASE=str(hostile),
                   WORKSTATION_RUNTIME_ROOT=str(output))
        process = subprocess.run([str(python), "-c", _VERIFY_CODE], cwd=str(output), env=env,
                                 capture_output=True, text=True, timeout=120)
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()
        raise RuntimeError(f"Runtime verification failed for {output}: {detail}")
    print(process.stdout.strip())


def build(engine: Path, output: Path):
    if sys.platform != "win32" or platform.python_version() != "3.12.10":
        raise RuntimeError("Build with Windows x64 CPython 3.12.10")
    engine = engine.resolve()
    packages = distributions()
    engine_version = _engine_version(engine)
    engine_digest = _engine_digest(engine)
    signature = {"schema_version": BUNDLE_SCHEMA_VERSION, "builder_version": BUNDLE_SCHEMA_VERSION,
                 "python": platform.python_version(), "engine_source_sha256": engine_digest,
                 "engine": {"distribution": ENGINE_DISTRIBUTION, "version": engine_version,
                            "source_sha256": engine_digest},
                 "packages": {name: dist.version for name, dist in sorted(packages.items())}}
    manifest = output / "runtime-manifest.json"
    if manifest.exists():
        if manifest.is_file() and json.loads(manifest.read_text()) == signature:
            verify_runtime(output)
            print(f"Reusing runtime: {output}")
            return
        raise RuntimeError(f"Output exists with a different or incomplete runtime: {output}. Use a new --output.")
    output.mkdir(parents=True, exist_ok=True)
    base = Path(sys.base_prefix)
    for pattern in ("python.exe", "pythonw.exe", "python*.dll", "vcruntime*.dll", "LICENSE.txt"):
        for source in base.glob(pattern):
            shutil.copy2(source, output / source.name)
    ignored = shutil.ignore_patterns("site-packages", "__pycache__", "test", "tests", "idlelib",
                                     "ensurepip", "tkinter", "turtledemo", "*.pyc")
    shutil.copytree(base / "Lib", output / "Lib", ignore=ignored, dirs_exist_ok=True)
    shutil.copytree(base / "DLLs", output / "DLLs", ignore=shutil.ignore_patterns("*_tkinter*", "tcl*", "tk*"), dirs_exist_ok=True)
    site = output / "Lib" / "site-packages"
    site.mkdir(exist_ok=True)
    for name, dist in sorted(packages.items()):
        print(f"Bundling {name} {dist.version}", flush=True)
        for relative in dist.files or []:
            # Console scripts, editable .pth files and build-machine paths are unnecessary.
            if ".." in relative.parts or relative.suffix in {".pyc", ".pth"} or relative.name == "direct_url.json":
                continue
            source = Path(dist.locate_file(relative))
            if not source.is_file():
                raise RuntimeError(f"Missing distribution file: {name}/{relative}")
            target = site / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in ENGINE_PACKAGES:
        source = engine / "src" / name
        if not (source / "__init__.py").is_file():
            raise RuntimeError(f"Missing engine package: {source}")
        shutil.copytree(source, site / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), dirs_exist_ok=True)
    license_dir = output / "licenses"
    license_dir.mkdir(exist_ok=True)
    shutil.copy2(engine / "LICENSE.txt", license_dir / "SPD-PI-Engine.txt")
    engine_meta = site / f"spd_decap_pi_evaluator-{engine_version}.dist-info"
    engine_meta.mkdir(exist_ok=True)
    (engine_meta / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {ENGINE_DISTRIBUTION}\nVersion: {engine_version}\nLicense: Proprietary\n",
        encoding="utf-8")
    # Isolate from PATH, registry, PYTHONHOME, PYTHONPATH and user site-packages.
    (output / "python312._pth").write_text(".\nLib\nDLLs\nLib/site-packages\nimport site\n", encoding="utf-8")
    manifest.write_text(json.dumps(signature, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verify_runtime(output)
    print(f"Runtime ready: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--engine-root", type=Path)
    mode.add_argument("--verify-only", type=Path, metavar="RUNTIME")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "build" / "engine-runtime")
    args = parser.parse_args()
    if args.verify_only:
        verify_runtime(args.verify_only)
    else:
        build(args.engine_root, args.output.resolve())
