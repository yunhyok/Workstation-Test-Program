# Third-party notices

The Windows control application bundles CPython 3.12.10 and Tcl/Tk, and is built with
PyInstaller 6.20.0. Applicable notices are included in `packaging/licenses` and in the
installed application's `_internal/licenses` directory. The Python license file also
contains the notices supplied with its Tcl/Tk distribution.

The installer is produced using Inno Setup. The numerical engine, CUDA, cuDSS, NumPy,
SciPy and Matplotlib are not bundled; users install their own pinned engine environment.
This repository does not change or relicense those projects or the external SPD PI engine.
