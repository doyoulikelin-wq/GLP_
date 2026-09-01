# T12 theoretical-design audit bundle

This directory is the small, public, reproducible summary for the T12 CPU mechanism audit. It deliberately excludes model weights, NPZ inputs, CIF collections and complete run directories.

## Terminal state

```text
C2=FAIL (7/30 < 10/30)
G0=BLOCKED
T12_GPU=NOT_STARTED
BINDCRAFT=NOT_STARTED
```

The split-template adapter and tests are CPU engineering assets. Their presence does not change the failed gate and does not imply that T12 inference ran.

## Files

| File | Purpose | SHA-256 |
|---|---|---|
| `T12_FRAMEWORK_ALIGNED_CDR_AUDIT_20260902.json` | Four-arm and candidate-level summary, method contract and gate result | `67b19c6898c2b040ded93e2a4fec31c81e1591f5ba0b917142fdfb96ad35fd23` |
| `T12_FRAMEWORK_ALIGNED_CDR_SAMPLES_20260902.tsv` | All 155 `(arm, design_i, sample_index)` rows | `763f84ee7323d3786b7a6a0c1361c28ab09aaecf86d116b8d3a59c521f97675f` |
| `T12_HANDOFF_ARTIFACT_20260902.json` | Canonical source artifact for the offline report | `e1d678d17b9575c636136f1b3bf4d7bc9ef20b9defd0955917323509f5f4b3d9` |
| `T12_WINDOWS_RUN_LOGIC_AUDIT_HANDOFF_20260901.html` | Self-contained Windows/WSL2 handoff report, refreshed on 2026-09-02 while retaining the original request-date filename | `972dad7311ab12c25c813ba3f947c09c9915cca3f18de02b87a08238b9572011` |

The HTML passed artifact validation, packaging and structural verification. Browser-based visual/interaction verification was unavailable because this Windows host had no callable Chromium headless shell; the report contains a static fallback, embedded payload/runtime and no external script or stylesheet dependency.

## Reproduce the 155-sample audit in WSL2

Run from `/home/lin/creator/GLP_` and choose new output paths; the auditor refuses to overwrite an existing JSON or TSV.

```bash
ACCEPTED_PY=/home/lin/creator/gpu_work/environments/cu128_blackwell_candidate/attempt_004/env/bin/python
OWNER_TASK=boltzgen/main/windows_single_owner_20260831

"$ACCEPTED_PY" -I "$OWNER_TASK/scripts/audit_t12_framework_aligned_cdr.py" \
  --internal-root /home/lin/creator/gpu_work/owner_mode/t8_exploratory_inference/7xl0_design3_pose_adherence_b1_n10_f5/attempt_20260831T182438Z/intermediate_designs_inverse_folded \
  --high-contact-root /home/lin/creator/gpu_work/owner_mode/t8_exploratory_inference/7xl0_design3_highcontact_adherence_b1_n10_f5/attempt_20260831T183938Z/intermediate_designs_inverse_folded \
  --diverse-root /home/lin/creator/gpu_work/owner_mode/t8_exploratory_inference/7xl0_design3_highcontact_diverse_b1_n5_f5/attempt_20260831T185650Z/intermediate_designs_inverse_folded \
  --fixed-ifold-root /home/lin/creator/gpu_work/owner_mode/t11_only_inverse_fold_from_pose_spec/7xl0_highcontact_ifold_n6_f5_v3/attempt_20260831T201438Z/intermediate_designs \
  --output-json NEW_DIRECTORY/t12_framework_aligned_cdr_audit.json \
  --output-tsv NEW_DIRECTORY/t12_framework_aligned_cdr_samples.tsv \
  --framework-threshold-angstrom 4.0 \
  --fixed-ifold-min-pass 10
```

Expected WSL-shell exit code: `42`. This is the registered scientific-gate failure, not a Python crash. On this host, invoking a nonzero Linux command directly through `wsl.exe` normalizes the Windows process status to `1`; inspect the generated JSON field `exit_code: 42` or run inside the WSL shell when the exact Linux code is required.

Expected result:

| Arm | Samples | Framework-aligned CDR RMSD ≤4 Å |
|---|---:|---:|
| internal | 50 | 50 |
| high_contact | 50 | 34 |
| diverse | 25 | 20 |
| fixed_ifold | 30 | 7 |

The four independently reproduced target-aligned minima are `23.264`, `22.052`, `25.327` and `29.899 Å`; all four arms have zero target-aligned samples at or below `8 Å`.

## Verification performed before publication

- Full owner suite: `70 passed`, one existing `pynvml` deprecation warning, `28.84 s`.
- Split-template targeted tests: `5 passed`.
- Audit targeted tests: `5 passed`.
- Real sealed `design_0` CPU DataModule smoke: template shape `(2,151)`, slot sums `[30,91]`, CDR visible count `0`, injected `tokenized` feature removed.
- No GPU compute process, no T12 GPU attempt and no BindCraft run.

The source T11 run was produced at commit `d989db24066bda4652d48f4e14dd80e6409890aa`. The later Git commit containing this directory adds audit code, tests and documentation only; it must not be presented as the source commit of the historical model output.
