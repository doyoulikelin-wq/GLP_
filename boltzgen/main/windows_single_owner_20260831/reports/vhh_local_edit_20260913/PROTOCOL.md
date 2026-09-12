# Local VHH editing pilot — frozen before GPU execution

Protocol recorded on 2026-09-13 (Asia/Shanghai). This is a local BoltzGen 0.3.2
analogue of sequence redesign and local inpainting, not a test of the hosted
Boltz API. No cloud upload, training, fusion experiment, or platform migration.

## Question and scope

Can restricting edits to one small CDR window retain or improve the terminal
contact geometry of the two original C-arm VHH designs under free refolding?
The experiment is exploratory and cannot establish experimental binding,
affinity, selectivity, pharmacology, or general method superiority.

- Use both original C-arm RAW_GENERATION candidates from the existing terminal
  continuation, including the candidate without simultaneous His7/Ala8 contact.
  Never substitute the later inverse-folded intermediates for these sources.
- Select one common contiguous five-residue window within a single annotated
  CDR, deterministically using the mean distance to His7 and Ala8 over the two
  existing raw structures. Freeze this choice before any new GPU result exists.
- Three arms per source: ORIGINAL (no edit), SEQUENCE_ONLY (official local
  `--only_inverse_fold`, backbone retained), and LOCAL_INPAINT (design step only,
  same five-residue window and same length). One edited output per source/arm.
- Keep sequence outside the edit window unchanged, including the target and
  framework. Do not expand the scaffold set or the CDR lengths in this pilot.
- Both editing arms use unspecified binding labels and the original structural
  context. This is a conditioned local edit, not a new unbiased binding result.
  Measure structural retention; a soft structural condition is not a promise
  that every context coordinate remains mathematically fixed.
- Evaluate all six final sequences under one target-only template protocol,
  without a VHH template, the original relative binding pose, or binding labels.
  Three stochastic free folds each: 18 expected samples in total.
- Reuse the existing actual 9NK9 2/2 short-peptide recovery receipt after checking
  its binding to the folding assets. Do not claim an old VHH gate passed.

## Frozen evaluation

Main contact measurements use the original full three-CDR annotation, not only
the five edited positions. The edit window is an additional diagnostic. A
terminal contact means a canonical heavy-atom distance at most 4.5 angstrom.
Report simultaneous His7/Ala8 contacts, any CDR-target contact, distances, and
the number of candidates retaining simultaneous terminal contact in all three
folds. Do not tune thresholds after observing results.

Three folds are nested stochastic repeats of one candidate, not three distinct
molecules. Reusing seed settings does not establish paired common-random-number
sampling between editing methods. Sequence changes and input conditioning can
also change sampling behavior.

Verify six-task output closure, three samples per task, residue identities,
lengths, finite coordinates, canonical atom mapping, and complete heavy atoms.
Zero-coordinate sidechain placeholders in an inverse-folding intermediate are
not assessable for physical contact: do not count them as a loss of binding.
Final free-fold outputs must have valid heavy-atom coordinates.

## Resource and evidence boundaries

One GPU process at a time, one attempt, no automatic GPU retry. The runner has a
1,200-second total wall-time budget including runtime verification, with time
reserved for post-run checks. Stop on process errors, invalid outputs, or budget
exhaustion; partial evidence remains partial rather than being silently rerun.
Verify only the actually used runtime assets before and after execution.

Keep raw sequences, structures, large logs, weights, and machine paths private.
Publish scoped source/tests and small redacted summaries/process records. The
user-facing report is direct Chinese prose, not HTML. Record preparation time
separately from runner time and do not label all elapsed time as GPU compute.

Terminal amidation, protonation and charges are not atomically validated in this
pilot. Any result concerns the specified modeled geometry only. Original data
and all earlier attempts remain unchanged.

## CPU-selected window (before GPU)

The enumeration considered 18 eligible windows and selected CDR3 VHH residues
106–110 (one-based VHH numbering; complex token indices 135–139). The selection
score is the mean, over both source draws and all five residues, of each
residue's minimum heavy-atom distance to His7 and to Ala8. The selected score was
8.6011 angstrom (source-specific means 9.3626 and 7.8396). This mean-of-minima
selection score is not the final whole-CDR terminal-contact metric.

The actual CPU parser and masker verified both edit modes: 151 total residues,
5 editable residues, 146 fixed residue identities, target not designed, and
unspecified binding labels. This is an input-compatibility result, not yet a
GPU result or evidence of improved recognition.
