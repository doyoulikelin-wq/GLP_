# Continuation process, 2026-09-13

Start checkpoint: `e92bd60`, clean canonical worktree, ACTIVE Windows owner mode.
The first recorded clock read was 2026-09-13 04:57:22 UTC. The preceding local
pilot is complete and immutable; this continuation contains no new GPU jobs.

The analysis plan was recorded before receiving the decomposition results.
Contact-location code, shape/pose code, and local model-interface inspection ran
in parallel. Root independently implemented SciPy proper-rotation fitting and
direct target-replacement distance calculations without calling project fit or
contact helpers. No private structures were sent to external services.

## Execution and checks

- Pose diagnostic: all 18 predictions retained, about 0.61 s CPU analysis.
- Contact-location diagnostic: all 18 retained, about 0.52 s CPU analysis.
- Original whole-CDR counts reconcile exactly with the published pilot:
  ORIGINAL 1/6, SEQUENCE_ONLY 0/6, LOCAL_INPAINT 1/6.
- Ninety scalar structural measurements agree between the main and independent
  implementations within 3.0871e-6 angstrom.
- Thirty-six target-replacement terminal distances agree within 1.066e-14
  angstrom. Both the 26-point primary central alignment and the 16-point
  sensitivity alignment leave zero double-terminal contacts after replacement.
- The original raw-source target differs from the common target reference by
  only about 0.122/0.095 angstrom CA RMSD, so the large reported pose changes are
  not an accidental comparison between unrelated target structures.
- Final related tests: 547 pass in 32.36 s, with the existing pynvml deprecation
  warning. The three new test modules account for 27 new tests; totals overlap
  with previous suites and are not added across runs.

No source outputs were deleted or overwritten. The independent replacement
exists only as a mathematical distance calculation, not as a fabricated model
output. It is not physically relaxed, may clash, and must not enter the list of
predictions or successful candidates.

## Interpretation corrections and source review

Contact topology shows 15 terminal-negative samples with CDR middle/C-region
contact and one with framework-only contact. All 18 retain some VHH contact at
the cutoff. Therefore the large source-relative RMSD must not be paraphrased as
complete dissociation. Both terminal positives involve CDR3, with only the
inpainting positive involving the selected five-residue window.

The reference CIF identifies itself as `6X18_GLP1_7_36_CHAIN_P`. The experimental
6X18 entry describes receptor-bound GLP-1. Primary GLP-1 NMR work and 1D0R describe
solvent-dependent structure and must not be treated as a physiological free-state
ground truth. Thus neither high target RMSD nor template fidelity alone proves
biological failure/success. No new conformer is introduced or new threshold fit
to these 18 outcomes.

Source definitions reviewed on 2026-09-13:

- https://www.rcsb.org/structure/6X18 — receptor-bound reference state.
- https://doi.org/10.1002/mrc.880 — primary NMR/solvent condition context.
- https://www.rcsb.org/structure/1D0R — TFE/water, not an untreated physiological ensemble.
- https://github.com/jwohlwend/boltz/blob/main/docs/prediction.md — confidence
  definitions and independent-Boltz template-force capability. Latest online
  features are not proof of installed BoltzGen support.

Read-only installed-interface inspection confirmed only BoltzGen 0.3.2 and no
standalone `boltz` distribution/module. Actual CPU schema probing rejected
`templates`, `force`, and `threshold`. Inspection of the installed target-template
feature path and diffusion updates found soft template conditioning, not a
directly enabled target coordinate clamp. This is an interface compatibility
finding; independent Boltz weight-loading compatibility remains untested.

## Handoff

Publish only scoped scripts/tests, this plan/process, and numerical summaries.
Keep sequences, coordinates, logs, raw arrays and machine paths private. Update
the project entry points to the new research record without rewriting historical
results. No HTML is created; preserve the user's direct-text delivery preference.
