#!/usr/bin/env python3
"""CPU-screen six existing T4 VHH scaffolds without designing sequences or a GPU.

Rechecks small source hashes, fixed loop annotations and actual backbone/sequence
content. This is an input-library check, not GLP1 binding/developability evidence.
Raw copied specs and sequences remain private; the public registry omits them.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import time

import gemmi
import numpy as np
import yaml

SELECTED = ("01_pdb_00007xl0-A", "02_pdb_00006apo-A", "09_pdb_00008im0-B",
            "03_pdb_00008v9x-A", "06_pdb_00008fq7-A", "11_pdb_00008gz6-A")
BASELINES = SELECTED[:2]
MEMBERS = ("design.yaml", "scaffold.yaml", "scaffold.cif", "target.cif")
HEAVY_COUNTS = dict(zip("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL".split(),
                       (5, 11, 8, 8, 6, 9, 9, 4, 10, 8, 8, 9, 8, 11, 7, 6, 7, 14, 12, 7)))


def sha(path: Path) -> str:
    """Digest one small direct input, refusing links and unexpectedly large files."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024**2:
        raise ValueError(f"not a small regular source file: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sequence_digest(sequence: str) -> str:
    """Stable sequence fingerprint, without public sequence disclosure."""
    return hashlib.sha256(sequence.encode("ascii")).hexdigest()


def edit_distance(left: str, right: str) -> int:
    """Unit-cost Levenshtein distance; supports unequal framework lengths."""
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        row = [i]
        for j, b in enumerate(right, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j-1] + (a != b)))
        previous = row
    return previous[-1]


def annotation(scaffold: dict, length: int) -> list[list[int]]:
    """Require exactly three explicit positive disjoint CDR label-index ranges."""
    if len(scaffold.get("design", [])) != 1:
        raise ValueError("expected one annotated VHH chain")
    chain = scaffold["design"][0]["chain"]
    text = chain["res_index"]
    if chain["id"] != "A" or not re.fullmatch(r"\d+\.\.\d+,\d+\.\.\d+,\d+\.\.\d+", text):
        raise ValueError("expected fixed CDR1/CDR2/CDR3 ranges on normalized A chain")
    ranges = [list(map(int, item.split(".."))) for item in text.split(",")]
    if any(start < 1 or end < start or end > length for start, end in ranges):
        raise ValueError("CDR range outside modeled chain")
    if any(ranges[i][1] >= ranges[i+1][0] for i in (0, 1)):
        raise ValueError("CDR ranges overlap or are out of order")
    return ranges


def inspect_structure(path: Path, scaffold: dict) -> dict:
    """Recheck modeled canonical sequence, backbone, chain continuity and framework."""
    structure = gemmi.read_structure(str(path))
    if len(structure) != 1 or len(structure[0]) != 1 or structure[0][0].name != "A":
        raise ValueError("one model and normalized A chain required")
    residues = list(structure[0][0])
    sequence, atom_maps, missing_backbone, incomplete_heavy = [], [], [], []
    for i, residue in enumerate(residues, 1):
        if residue.name not in HEAVY_COUNTS or residue.label_seq != i:
            raise ValueError("canonical residues with contiguous 1-based label indices required")
        atoms = {atom.name: atom for atom in residue if not atom.element.is_hydrogen}
        if not np.isfinite([list(atom.pos) for atom in atoms.values()]).all():
            raise ValueError("nonfinite scaffold coordinates")
        if not {"N", "CA", "C", "O"}.issubset(atoms):
            missing_backbone.append(i)
        if len(set(atoms) - {"OXT"}) < HEAVY_COUNTS[residue.name]:
            incomplete_heavy.append(i)
        sequence.append(gemmi.find_tabulated_residue(residue.name).one_letter_code)
        atom_maps.append(atoms)
    ranges = annotation(scaffold, len(residues))
    cdr = {index for start, end in ranges for index in range(start-1, end)}
    full_sequence = "".join(sequence)
    framework = "".join(aa for i, aa in enumerate(sequence) if i not in cdr)
    gaps = []
    for i, (left, right) in enumerate(zip(atom_maps, atom_maps[1:]), 1):
        if "C" in left and "N" in right:
            distance = left["C"].pos.dist(right["N"].pos)
            if not 1.0 <= distance <= 2.0:
                gaps.append({"after_residue": i, "c_to_n_angstrom": distance})
    return {"length": len(residues), "cdr_ranges": ranges,
            "cdr_lengths": [end-start+1 for start, end in ranges],
            "framework_length": len(framework), "sequence": full_sequence,
            "framework_sequence": framework, "sequence_sha256": sequence_digest(full_sequence),
            "framework_sequence_sha256": sequence_digest(framework),
            "missing_backbone_residues": missing_backbone, "incomplete_heavy_residues": incomplete_heavy,
            "incomplete_framework_heavy_residues": [i for i in incomplete_heavy if i-1 not in cdr],
            "incomplete_cdr_heavy_residues": [i for i in incomplete_heavy if i-1 in cdr],
            "heavy_atom_screen_method": "per-residue canonical heavy atom count excluding OXT; not atom identity/chemical validity proof",
            "all_heavy_atom_counts_complete": not incomplete_heavy,
            "input_readiness_note": "SIDECHAIN_GAPS_RECORDED_BACKBONE_INPUT_ONLY" if incomplete_heavy else "MODELED_BACKBONE_AND_HEAVY_ATOM_COUNTS_COMPLETE",
            "peptide_continuity_outliers": gaps, "finite_coordinates": True,
            "cysteine_count": full_sequence.count("C"),
            "scaffold_input_ready": not missing_backbone and not gaps}


def choose_addition(records: list[dict]) -> dict | None:
    """Prioritize exact three-loop-length match and genuine nonidentical framework."""
    baseline = [record for record in records if record["scaffold_id"] in BASELINES]
    choices = []
    for record in records:
        if record["scaffold_id"] in BASELINES or not record["scaffold_input_ready"]:
            continue
        for old in baseline:
            distance = edit_distance(record["framework_sequence"], old["framework_sequence"])
            if distance and record["cdr_lengths"] == old["cdr_lengths"]:
                choices.append((record["resolution_angstrom"], record["scaffold_id"], old["scaffold_id"], distance))
    if not choices:
        return None
    _, scaffold, baseline_id, distance = sorted(choices)[0]
    return {"scaffold_id": scaffold, "matched_baseline": baseline_id,
            "all_three_cdr_lengths_matched": True, "framework_edit_distance": distance,
            "reason": "exact fixed loop lengths but a nonidentical framework; computational comparison only"}


def screen(t4_attempt: Path, output: Path) -> dict:
    """Screen six selected existing candidates and create a fresh private/public record."""
    started = time.monotonic()
    if output.exists() or output.is_symlink():
        raise ValueError("refusing to reuse library output directory")
    if any((parent / ".git").exists() for parent in (output, *output.parents)):
        raise ValueError("raw scaffold library output must be outside a Git checkout")
    source = t4_attempt / "project_input"
    verification_path = t4_attempt / "provenance/spec_verification.json"
    verification = json.loads(verification_path.read_text())
    checks = {row["spec_id"]: row for row in verification["specs"]}
    registry_path = source / "scaffold_registry/selected_scaffolds.tsv"
    registry = {f"{int(row['selection_rank']):02d}_{row['candidate_id']}": row
                for row in csv.DictReader(registry_path.open(), delimiter="\t")}
    records = []
    for identifier in SELECTED:
        spec_root = source / "specs" / identifier
        if {path.name for path in spec_root.iterdir()} != set(MEMBERS):
            raise ValueError("source spec bundle has unexpected or missing members")
        hashes = {name: sha(spec_root / name) for name in MEMBERS}
        previous, row = checks[identifier], registry[identifier]
        for file, key in (("design.yaml", "spec_sha256"), ("scaffold.yaml", "scaffold_yaml_sha256"),
                          ("scaffold.cif", "scaffold_sha256"), ("target.cif", "target_sha256")):
            if hashes[file] != previous[key] or previous["status"] != "PASS":
                raise ValueError(f"historical T4 source binding mismatch: {identifier}:{file}")
        item = inspect_structure(spec_root / "scaffold.cif", yaml.safe_load((spec_root / "scaffold.yaml").read_text()))
        expected_lengths = [int(row[f"cdr{loop}_length_aa"]) for loop in (1, 2, 3)]
        if item["cdr_lengths"] != expected_lengths or item["length"] != int(row["variable_length_aa"]):
            raise ValueError("fresh structure/annotation disagrees with curated registry")
        records.append({"scaffold_id": identifier, "pdb_id": row["pdb_code"], "source_auth_chain": row["source_hchain"],
                        "source_url": f"https://www.rcsb.org/structure/{row['pdb_code']}",
                        "normalized_input_chain": "A", "resolution_angstrom": float(row["resolution_a"]),
                        "resolution_source": "existing_T4_curated_registry_not_remeasured",
                        "historical_machine_input_check": previous["status"], "source_hashes": hashes,
                        "source_spec_path": str(spec_root / "design.yaml"), **item})
    if len({record["source_hashes"]["target.cif"] for record in records}) != 1:
        raise ValueError("library target structures differ")
    comparisons = []
    for i, left in enumerate(records):
        for right in records[i+1:]:
            distance = edit_distance(left["framework_sequence"], right["framework_sequence"])
            comparisons.append({"left": left["scaffold_id"], "right": right["scaffold_id"],
                                "framework_edit_distance": distance,
                                "normalized_edit_distance": distance/max(left["framework_length"], right["framework_length"]),
                                "identical_framework": distance == 0})
    selection = choose_addition(records)
    public_rows = [{key: value for key, value in record.items() if key not in {"sequence", "framework_sequence", "source_spec_path"}}
                   for record in records]
    public = {"schema": "VHH_SCAFFOLD_LIBRARY_SCREEN_V1", "date": "2026-09-10", "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "screened_count": len(records), "input_ready_count": sum(row["scaffold_input_ready"] for row in records),
              "distinct_framework_count": len({row["framework_sequence_sha256"] for row in records}),
              "biological_pass": False, "gpu_started": False, "next_gpu_new_scaffold_limit": 1,
              "baseline_scaffolds": list(BASELINES), "recommended_addition": selection,
              "records": public_rows, "framework_comparisons": comparisons,
              "source_receipts": {"t4_spec_verification_sha256": sha(verification_path), "curated_registry_sha256": sha(registry_path)},
              "limitations": ["Existing normalized VHH fragments, not newly downloaded full original complexes",
                              "CDR ranges are local curated label indices, not independent IMGT reannotation",
                              "Framework nonidentity is sequence-based; not a claim of independent fold class",
                              "Complete modeled backbone does not establish expression, stability or binding",
                              "Input readiness tolerates explicitly recorded missing sidechain atoms; no sidechains were rebuilt by this screen",
                              "Matching loop lengths reduces but does not remove scaffold/geometry confounding",
                              "No de novo sequences, GPU predictions, affinity measurements or target selectivity evidence"],
              "elapsed_seconds": time.monotonic()-started}
    output.mkdir(parents=True, mode=0o700)
    (output / "specs").mkdir(mode=0o700)
    for record in records:
        shutil.copytree(Path(record["source_spec_path"]).parent, output / "specs" / record["scaffold_id"])
        for name, expected in record["source_hashes"].items():
            if sha(output / "specs" / record["scaffold_id"] / name) != expected:
                raise ValueError("copied private input changed during preparation")
    private = {**public, "records": records, "private_source_root": str(t4_attempt)}
    for name, data in (("PRIVATE_SCAFFOLD_LIBRARY.json", private), ("PUBLIC_SCAFFOLD_LIBRARY.json", public)):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    return public


def main() -> int:
    """CLI: source T4 attempt and a fresh owner-mode output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--t4-attempt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = screen(args.t4_attempt.resolve(strict=True), args.output.absolute())
    print(json.dumps({key: result[key] for key in ("screened_count", "input_ready_count", "distinct_framework_count", "recommended_addition", "elapsed_seconds")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
