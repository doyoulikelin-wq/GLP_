# Terminal-recognition diagnosis — 2026-09-13 continuation

## Question and frozen scope

The preceding local-edit pilot completed 18 predictions: two original source
draws, three arms, three folds each, representing four unique final sequences.
Whole-CDR double-terminal contacts were 1/6, 0/6, and 1/6; no candidate achieved
three-of-three contact. This continuation asks where that instability appears
geometrically, before spending on another candidate-generation batch.

The controlling evidence is the actual completed pilot output, its fixed-input
manifest, bound original target/reference CIFs, and corresponding CPU atom-name
maps. Do not infer structure movement from scores alone. Retain every predicted
sample, including noncontacts and the existing close-contact warning. Preserve
the 4.5-angstrom endpoint; new descriptors are diagnostics, not revised pass gates.

## Three diagnostic questions

1. Is the target internally reshaping? Align target central C-alpha atoms
   (complex tokens 2–27, native GLP-1 residues 9–34) to the original target.
   Report target C-alpha error and terminal displacement, not just a whole-complex
   RMSD that mixes conformation and placement.
2. Is VHH structure retained but relative placement changing? Independently
   align each predicted VHH framework to its original source, excluding all CDRs.
   Measure framework and CDR C-alpha changes. Separately measure VHH framework
   error after target alignment as a descriptor of relative placement plus any
   residual structural difference. Do not call source-pose recovery native-pose
   recovery: the source itself was generated, not experimental ground truth.
3. Where are the contacts? Separate CDR1, CDR2, CDR3, the five-residue edit window,
   and framework. Describe target regions 7–8, 9–14, 15–28, and 29–36. These
   partitions describe location; only His7/Ala8 defines the original endpoint.

## Diagnostic coordinate substitution

Fit the original target to each predicted target using central C-alpha atoms,
then replace only the target coordinates while holding the predicted VHH fixed.
Recalculate terminal distances to ask whether target reshaping alone could
account for the lost contact in that coordinate construction. This operation
does not produce a new model prediction, physically relaxed structure, or
evidence of affinity. Contacts can appear or disappear because this is an
artificial geometric substitution; do not count them as new successful designs.

## Efficiency and evidence boundaries

This stage is CPU-only reuse of existing predictions, not another GPU campaign.
Independent direct-array recomputation and synthetic transformation tests cover
the consequential geometry. Record exact source hashes and small public summaries,
while keeping sequences, coordinates, full logs, and private paths on the local
computer. Original files are not overwritten. Deliver the conclusions directly
in Chinese, without HTML, retaining the user's selected format.

A further GPU contrast must answer a concrete question the existing geometry
cannot settle. Define its inputs, budget, limitations, and stop rule before any
launch. Do not declare causality, increase sampling until a positive appears,
promote template-conditioned contact as free recognition, or use a previously
opened development set as a blinded holdout.
