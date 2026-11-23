# Review fixes — 1.0.1 candidate

The September 21 review findings are addressed on `codex/review-fixes-20260921`,
based on `f6549ea`. This is a PR candidate; the owner merges the PR. It does not
publish a new release or claim completion of the target workstation study.

## Finding, change, regression evidence

| Finding | Change | Regression coverage |
| --- | --- | --- |
| P1 baseline falsely returns failure | Comparisons start successful; an unconfigured laptop comparison is `not_configured`; failed comparisons have named JSON logs and a per-run summary; stop remains exit 130. | Fake workers cover successful default CPU/GPU baseline without laptop receipts, failed CPU/GPU comparison, and stop. |
| P2 private defaults in public code | `designs` and `port_sets` are local configuration; P9/P20/P92/E4 and the agent design allowlist use that configuration. Report families are explicit metadata. README uses an owner-provided engine checkout. | Config selection, dynamic agent allowlist, family metadata, and repository source privacy scan. |
| P2 half-open connection exhaustion | 30-second socket timeout, 32 active handlers, excess requests receive 503 and close, 64 KiB body limit preserved. | 40 half-open connections; a normal request receives a response within five seconds; handlers are reclaimed. Production timeout is asserted separately; reclamation uses a shorter test timeout. |
| P2 excessive version gates | Only Python, numpy, and scipy are hard requirements. Seven other differences are recorded; three GPU stack differences also warn. Post-gate fingerprint changes still lock measurement. | Pydantic differences appear in env/gates and do not block a matching gate fingerprint; numpy mismatch and changed fingerprints block. |
| P2 example paths | Forward-slash JSON paths; optional laptop paths default to null. | JSON parse, slash spelling, manifest references, and optional-path defaults. |
| P3 graceful stop | Check immediately before each worker launch; wait for already running ports. | A stop appearing after the first launch prevents the next launch. README/help describe in-flight batch completion. |
| P3 remote artifacts | Permit gates.json and matrix_plan.json. | Authenticated fetch coverage retains traversal and allowlist checks. |
| P3 access logging | Query key names only; 1 MiB size rotation, three backups. | Query-value redaction and bounded rotation. |
| P3 held-out references | Document exact SPD port names and Touchstone column order in `--port-manifest`. | Exact composite names are preserved; invalid/ambiguous manifest inputs fail. Conversion numerics are unchanged. |
| P3 agent self-check | Add real env subprocess/HTTP/client round trip alongside deterministic conflict/stop workers. | Self-check asserts `real_env`; absent engine configuration is a diagnostic result, not numerical PASS. |
| P3 token length | Reject tokens shorter than 32 characters. | Short-token startup rejection. |
| P3 affinity caveat | Report template states lowest logical CPU selection can bias NUMA/CCD/SMT comparisons. | Template inspection; affinity implementation is unchanged. |
| P3 VRAM idle baseline | Median of five pre-launch samples, one second apart; record those samples. | Five-sample median and fast unavailable-GPU path. Report warns that whole-device estimates include other processes. |
| P3 fan-out rationale | README explains per-port limits, VRAM samples, and stop/resume identity. | Documentation inspection; engine numerical options are unchanged. |
| P3 root summary collision | Rename program evidence to program_validation_summary.json. | Regression rejects a repository-root summary.json and checks the new file. |

## Test-first record

Local logs are under ignored `output/review-fixes/` because raw engine probes and
review fixtures contain local paths and design identifiers. The public summary
contains sanitized results only.

- Validation regressions: **12 failed, 8 passed** before implementation; **23 passed** after.
- Remote regressions: **7 failed, 9 passed** before implementation; **17 passed** after.
  Some initial failures exercise the absent bounded-server constructor options;
  those are API-presence failures, not measurements of the old server's timeout.
- Public-source/report check: **1 failed, 23 passed** before privacy changes;
  the source scan passes after changes. Report-specific suite: **25 passed**.
- App configuration/naming: **3 failed** before changes; **3 passed** after.
- Final independent review found the optional laptop-path example problem:
  its added regression **failed once before correction**, then passed.
- HQ separately ran the three P1 tests against a clean archived `f6549ea` source
  snapshot: **3 failed**. The baseline success test reproduced exit 1; the failure
  and stop tests exposed absent run summaries (the original stop already returned 130).
- The original config file **already parsed as JSON**. Its new slash-style test
  failed before the change; no original JSON parse failure is claimed.
- Documentation-only clarifications were inspected; they are not represented as
  newly failing runtime tests. Manifest conversion behavior and body limits were
  already correct and are preserved by regression coverage.

## Final validation and boundaries

Exact final counts and installer SHA-256 are in
[`program_validation_summary.json`](../../program_validation_summary.json).
Validation includes the full local test suite; validation, agent, and converter
self-checks; an actual configured external-engine env probe; installer/GUI/CLI
startup; uninstall; and preservation of study files.

The requested existing CPU fixture was rerun through the actual engine worker:
27 identical frequency points, maximum relative impedance error
**3.231881322361924e-12**, below **1e-9**. This is a single-port numerical regression,
not a full workstation matrix. Target workstation gates and axes A–F remain
**NOT_RUN**. There were no engine-repository changes, no worker numerical changes,
and no preregistration changes. Frozen plan SHA-256:
`e58b32f68749c75c24054477bb4a4f80f2b5935fdfd14047eff7f16a282de004`.

All listed P3 items are addressed. Affinity uses the requested documentation
option rather than adding a new mask interface. Cancellation waits for already
running ports; VRAM is an estimate, not per-process allocation telemetry.

## Public scope and owner decision

The original user explicitly requested a public repository. This PR retains that
visibility and removes operational design defaults from public code and examples.
The immutable preregistration, historical report, existing installer, and Git
history can still contain prior identifiers. The owner should decide whether
those historical materials may remain public or require a separate visibility or
history-remediation action. This PR does not rewrite history or change repository
visibility, and it leaves merging to the owner.
