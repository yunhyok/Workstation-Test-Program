# Standalone installer validation — 2026-09-29

Version 1.2.0 is a PR candidate. The owner merges PR #2 and decides whether to publish the release draft.

The installer contains an isolated CPython runtime, the owner-provided engine, CPU libraries, and GPU runtime libraries. Only the SPD/Touchstone input directory is required. The default GUI prepares files before launching a batch; advanced engine and laptop comparison settings are optional.

## Verified

| Check | Result |
| --- | --- |
| Source tests | 132 passed |
| Host Python isolation | PASS with hostile Python environment variables; all numerical imports came from the bundle |
| Real folder discovery/conversion | 3 designs, 344 ports; 92×826, 92×826, 160×626 |
| Converted references against existing reference artifacts | Maximum relative error 6.063e-15; frequency grids and port names identical |
| Real 27-frequency CPU/GPU solve | Relative error 3.12581301794138e-11, limit 1e-8 |
| CPU vs previously verified worker result | Relative error 2.70344240724803e-13, limit 1e-9 |
| Installed workflow | GUI, prepare, env, CPU/GPU/input gates, validation/agent self-checks passed |
| Uninstall | Executables removed; study evidence preserved |
| Original engine checkout / frozen plan | No modifications |

The installed gate checks runtime operations and input consistency. It does not claim that the historical default/GPU/slow reproduction suite ran. Automatic subsets are generated from the selected data, and inputs without an explicit package/PCB classification use the existing stricter comparison limit. Full target-workstation A–F measurements were not run as part of this packaging validation.

Input files are read-only. Preparation rejects ambiguous matching, supports permuted Touchstone port columns, checks source stability, and recovers interrupted generated artifacts without overwriting source data. Folder reports select the current gate fingerprint and preserve excluded older evidence. Failed preparation keeps the last valid configuration.

Installer: `Workstation-Test-Program-1.2.0-Setup-x64.exe` (1,875,062,716 bytes).
SHA-256: `54fece14376f86ca7aceed19e8b627aeba48050c5e78f7edf42d7d38e58dc1cf`. The candidate is unsigned. NVIDIA hardware and its display driver remain required for GPU measurements; CPU-only machines record GPU work as not run.

Local evidence: `output/standalone-study/` contains build/install logs, conversion and numerical checks, and pytest output. These files contain local input paths and are not committed.
