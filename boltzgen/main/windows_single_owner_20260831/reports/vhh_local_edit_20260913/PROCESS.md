# Local editing pilot process — 2026-09-13

## Preparation and implementation

The user's requested output is a separate direct-text report, not HTML. The
agreed implementation tries the local editing strategy and does not access or
benchmark the hosted Boltz API. No cloud account, upload, or paid API job is used.

Work began at 2026-09-12 17:51:03 UTC (2026-09-13 01:51:03 Asia/Shanghai). CPU
preparation, bounded execution code, and scoring were developed in parallel.
The original data, existing runtime, and previous measured 9NK9 calibration
were reused. The deterministic window and evaluation are in PROTOCOL.md.

Preparation 01 passed actual CPU parsing/masking but was not run on GPU because
the cooperating scripts were still being finalized. The implementation binding
correctly rejected that stale plan. After source freeze, preparation 02 passed
the same CPU checks in 8.70 seconds and was used for the single GPU attempt.
The two warnings about specifying structure for designed residues are expected
for the intentionally backbone-preserving sequence-only arm, not silent errors.

Runtime source was frozen as commit `9a0576d` in a clean detached checkout. All
predictions write to a new private attempt directory. One GPU process is allowed;
no automatic retry or additional candidate search is permitted. The runner
checks only four actually used model/dictionary assets before and after running.

## Verification and known implementation limits

The related directory suite passed 518 tests in 29.67 seconds at its initial
snapshot. Two last whole-structure placeholder cases were then added; the final
scoring module passed all 30 of its tests in 0.61 seconds. These counts overlap
and must not be summed. The pre-existing pynvml deprecation warning is retained;
no environment replacement was performed to remove a non-fatal warning.

The full repository policy check still reports historical missing module
docstrings and ignored Python caches; these unrelated items were not deleted or
repaired. All eight newly committed files passed the same per-file policy checks
and the staged whitespace check. This is not a claim of whole-repository policy
compliance.

Read-only configuration review found an additional asymmetry: sequence-only
inverse folding excludes newly selected CYS, whereas the local design arm has no
equivalent explicit exclusion. Neither original editing window contains CYS.
The existing mechanisms also use different weights, precision, representation,
and sampling defaults. Inputs and candidate budgets are matched, but this is a
comparison of two ready-made editing workflows, not an isolated causal test of
backbone reconstruction or a definitive algorithm ranking. No config is changed
after seeing GPU output to remove this limitation.

The inverse-folding intermediate may contain synthetic sidechain placeholders;
they are never treated as measured binding loss. Final predictions are checked
for complete canonical heavy atoms, finite coordinates, unchanged identities
outside the window, correct template scope, and the exact sample count. The
independent verifier recalculates contact distances from output arrays without
calling the project contact-scoring functions.

## Publication boundary

Only code, tests, this protocol/process record, and compact redacted evidence are
eligible for GitHub. Raw sequences, atom coordinates, NPZ arrays, full logs,
private paths, credentials, and model assets stay on this computer. Existing
source and superseded preparation directories are retained.

## Completed run and interpretation

The only GPU attempt completed at 18:02:51.590 UTC, after 228.145 seconds. All
four edit stages and all 18 free predictions completed without retry. The four
used runtime assets were unchanged. Final frozen-checkout related tests passed
520 cases in 30.11 seconds; this supersedes, rather than adds to, earlier test
snapshots.

The independent checker read all 18 actual predictions and reproduced whole-CDR
double-terminal counts ORIGINAL 1/6, SEQUENCE_ONLY 0/6, LOCAL_INPAINT 1/6 and
general-contact counts 5/6, 6/6, 6/6. Every arm had zero of two candidates with
all-three double-terminal contact. The inpainting double-terminal occurrence
was source draw 1, repeat index 1 (zero-based), with His 4.242725 and Ala 2.236537
angstrom. A different, non-terminal-positive inpainting sample (source draw 0,
repeat index 1) had the 1.096953-angstrom close-contact diagnostic. Neither was
dropped from the denominator.

Both sequence-only outputs had zero substitutions; both inpainting outputs had
one. A targeted actual CPU recheck of both IF inputs found exactly five edited
positions changed to UNK identity features, 146 outside identities retained, no
mask override, and no constraint locking edited positions to their original
residues. Upstream sampling copies only non-designed positions. This supports
retaining the no-op output, not treating the run as a mask failure or reporting
the unchanged sequence as an adverse editing effect. All six final input
secondary-structure labels were also unspecified.

Inpainting context backbone RMSDs were 0.183683 and 0.113069 angstrom; maximum
fixed-region displacements were 1.551538 and 1.500852 angstrom. Sequence-only
maximum aligned backbone changes were about 1.5e-6 angstrom. Sequence preservation
and geometric preservation are reported separately.
