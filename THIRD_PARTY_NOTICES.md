# Third-party notices

The Windows control application bundles CPython 3.12.10 and Tcl/Tk, and is built with
PyInstaller 6.20.0. Applicable notices are included in `packaging/licenses` and in the
installed application's `_internal/licenses` directory. The Python license file also
contains the notices supplied with its Tcl/Tk distribution.

The installer is produced using Inno Setup. The standalone numerical runtime includes
CPython, NumPy, SciPy, Matplotlib, Shapely, Pydantic, and their dependencies, plus the
CuPy, NVIDIA CUDA/cuDSS and nvmath Python runtime distributions. Their distribution
metadata and license files are preserved in `engine-runtime/Lib/site-packages`.
`engine-runtime/LICENSE.txt` contains CPython's license. The exact bundled versions
are recorded in `engine-runtime/runtime-manifest.json`.

The owner-provided SPD PI engine remains proprietary; its license is included at
`engine-runtime/licenses/SPD-PI-Engine.txt`. It is supplied locally at build time
and is not committed to this public repository. No project is relicensed here.
The NVIDIA display driver is not bundled or installed by this program.
