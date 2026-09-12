#!/usr/bin/env python3
"""Retrospective, stage-resolved geometry diagnosis; no inference or causal PASS.

Raw generation CIF, inverse-folded CIF, and free-fold NPZ are distinct sources.
The frozen 4.5-Angstrom heavy-atom definition is unchanged. Framework-aligned
CDR residue-centroid displacement and internal-distance change are descriptive.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time

import gemmi
import numpy as np
import yaml
from scipy.optimize import linear_sum_assignment
from boltzgen.data import const

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as E
import evaluate_vhh_pose_v2 as V
from summarize_vhh_controlled_expansion import source_reference
from summarize_vhh_pilot import cdr_annotation

STAGES = ("generation", "inverse_folding", "free_folding")
NUMERIC = ("his_min_cdr_distance_angstrom", "ala_min_cdr_distance_angstrom",
    "min_target_cdr_distance_angstrom", "min_target_framework_distance_angstrom")
BOOLEAN = ("both_epitope_contacts", "any_target_cdr_contact", "any_target_framework_contact", "framework_only_contact")


def read(path, inputs):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("source must be a nonsymlink regular file")
    raw = path.read_bytes()
    inputs.append({"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)})
    return raw


def cif_arrays(raw, cdr):
    """Explicit A target/B VHH label numbering, full canonical named atoms."""
    block = gemmi.cif.read_string(raw.decode()).sole_block()
    residues, atoms, coords, tokens, backbone = {}, {}, [], [], []
    tags = ["label_asym_id", "label_seq_id", "label_comp_id", "label_atom_id", "type_symbol",
        "label_alt_id", "pdbx_PDB_model_num", "Cartn_x", "Cartn_y", "Cartn_z"]
    for chain, number, name, atom, element, alt, model, x, y, z in block.find("_atom_site.", tags):
        if chain not in ("A", "B") or alt not in (".", "?") or model != "1" or element in ("H", "D"):
            raise ValueError("unsupported CIF chain, alternate/model or hydrogen")
        token = int(number)-1+(30 if chain == "B" else 0)
        if (chain == "A" and not 0 <= token < 30) or (chain == "B" and not 30 <= token < 151):
            raise ValueError("CIF label_seq_id outside explicit target/VHH mapping")
        if name not in E.RESIDUES or residues.get(token, name) != name or atom in atoms.get(token, set()):
            raise ValueError("ambiguous CIF atom/residue identity")
        residues[token] = name
        atoms.setdefault(token, set()).add(atom)
        coords.append([float(x), float(y), float(z)])
        tokens.append(token)
        backbone.append(atom in ("N", "CA", "C", "O"))
    if set(residues) != set(range(151)) or any(atoms[i] != set(const.ref_atoms[residues[i]]) for i in residues):
        raise ValueError("CIF canonical named heavy atoms incomplete")
    token = np.asarray(tokens)
    ids = np.array([E.RESIDUES.index(residues[i])+2 for i in range(151)])
    fold = {"coords": np.array(coords, dtype=float)[None], "atom_to_token": np.eye(151, dtype=bool)[token][None],
        "atom_resolved_mask": np.ones((1, len(token)), dtype=bool), "token_index": np.arange(151)[None],
        "res_type": np.eye(33, dtype=int)[ids][None], "mol_type": np.zeros((1,151), dtype=int),
        "backbone_mask": np.array(backbone)[None]}
    design = {"design_mask": np.isin(np.arange(151), cdr)}
    return design, fold


def evaluate_stage(design, fold, reference):
    contacts = E.evaluate_arrays(design, fold, target_tokens=list(range(30)))
    ids = np.asarray(fold["res_type"])[0].argmax(axis=-1)
    if tuple(E.RESIDUES[int(i)-2] for i in ids[:30]) != V.ACTIVE_SEQUENCE_NAMES:
        raise ValueError("target identity mismatch")
    mapping = np.asarray(fold["atom_to_token"])[0]
    valid = mapping.sum(axis=1) == 1
    token = mapping.argmax(axis=-1)
    bb = E._binary(fold["backbone_mask"], (1,len(token)), "backbone_mask")[0]
    if np.any(bb & ~valid) or not np.all(np.bincount(token[bb], minlength=151) == 4):
        raise ValueError("exactly four observed backbone atoms per token required")
    cdr = np.flatnonzero(design["design_mask"])
    framework = np.array([i for i in range(30,151) if i not in set(cdr)])
    rows = []
    for xyz, contact in zip(np.asarray(fold["coords"], dtype=float), contacts):
        centres = np.stack([xyz[bb & (token == i)].mean(axis=0) for i in range(151)])
        ta = np.flatnonzero(valid & (token < 30))
        fa = np.flatnonzero(valid & np.isin(token, framework))
        minimum = float(np.linalg.norm(xyz[ta,None,:]-xyz[None,fa,:],axis=-1).min())
        pose = V.evaluate_geometry(centres[:30], centres[cdr].mean(axis=0), centres[framework].mean(axis=0),
            reference["centroids"], biological_numbers=V.BIOLOGICAL_NUMBERS, usage="RETROSPECTIVE_METHOD_DEVELOPMENT")
        sidechain = valid & ~bb & np.isin(token,cdr)
        placeholders = int(np.sum(sidechain & np.all(xyz == 0,axis=1)))
        row = {**contact, "min_target_framework_distance_angstrom": minimum,
            "any_target_framework_contact": minimum <= 4.5,
            "framework_only_contact": minimum <= 4.5 and not contact["any_target_cdr_contact"],
            "pose": pose, "backbone_centroids": centres.tolist(),
            "zero_coordinate_cdr_sidechain_atom_count":placeholders,
            "all_atom_cdr_contact_applicability":"APPLICABLE"}
        if placeholders:
            row["all_atom_cdr_contact_applicability"]="NOT_ASSESSABLE_ZERO_COORDINATE_CDR_SIDECHAIN_PLACEHOLDERS"
            row["legacy_computed_with_dummy_sidechains_not_physical"]=contact
            for key in contact:
                if key not in ("sample_index",): row[key]=None
            row["framework_only_contact"]=None
        rows.append(row)
    return {"status": "OBSERVED", "sample_count": len(rows), "residue_ids": ids.tolist(), "samples": rows}


def rigid_fit(mobile, fixed):
    mobile, fixed = np.asarray(mobile, float), np.asarray(fixed, float)
    if mobile.shape != fixed.shape or mobile.ndim != 2 or mobile.shape[1] != 3 or not np.isfinite([mobile,fixed]).all():
        raise ValueError("invalid alignment arrays")
    a, b = mobile-mobile.mean(axis=0), fixed-fixed.mean(axis=0)
    if np.linalg.matrix_rank(a) < 2 or np.linalg.matrix_rank(b) < 2:
        raise ValueError("degenerate framework alignment")
    u, _, vt = np.linalg.svd(a.T@b)
    correction = np.eye(3); correction[-1,-1] = np.linalg.det(u@vt)
    rotation = u@correction@vt
    return rotation, mobile.mean(axis=0), fixed.mean(axis=0)


def compare_geometry(before, after, cdr, framework):
    a, b = np.asarray(before, float), np.asarray(after, float)
    rotation, centre, target = rigid_fit(b[framework], a[framework])
    aligned = (b-centre)@rotation+target
    da = np.linalg.norm(a[cdr,None,:]-a[None,cdr,:],axis=-1)
    db = np.linalg.norm(b[cdr,None,:]-b[None,cdr,:],axis=-1)
    v0 = a[cdr].mean(axis=0)-a[framework].mean(axis=0)
    v1 = aligned[cdr].mean(axis=0)-aligned[framework].mean(axis=0)
    angle = None if min(np.linalg.norm(v0),np.linalg.norm(v1)) < 1e-8 else float(np.degrees(np.arccos(np.clip(np.dot(v0,v1)/(np.linalg.norm(v0)*np.linalg.norm(v1)),-1,1))))
    return {"framework_fit_rmsd_angstrom": float(np.sqrt(np.mean(np.sum((aligned[framework]-a[framework])**2,axis=1)))),
        "cdr_centroid_rmsd_after_framework_fit_angstrom": float(np.sqrt(np.mean(np.sum((aligned[cdr]-a[cdr])**2,axis=1)))),
        "cdr_internal_distance_rms_change_angstrom": float(np.sqrt(np.mean((da-db)**2))),
        "framework_relative_cdr_direction_change_degrees": angle}


def input_atom_linkage(cif_fold, free):
    """Prove per-residue heavy-atom coordinate bijection, including side chains.

    NPZ does not retain atom names. Match its coordinates to the named canonical
    CIF atoms bijectively, not by assuming an undocumented within-token order.
    The 0.003-A tolerance is file-coordinate matching only, not a contact rule.
    """
    if not np.array_equal(cif_fold["res_type"],free["res_type"]):
        raise ValueError("CIF/NPZ residue identities differ")
    left=np.asarray(cif_fold["coords"],float)[0]
    right=np.asarray(free["input_coords"],float)[0,0]
    lm=np.asarray(cif_fold["atom_to_token"])[0]; rm=np.asarray(free["atom_to_token"])[0]
    maximum=0.
    for token in range(lm.shape[1]):
        a=left[lm[:,token].astype(bool)];b=right[rm[:,token].astype(bool)]
        if len(a)!=len(b): raise ValueError("CIF/NPZ per-token atom counts differ")
        distances=np.linalg.norm(a[:,None,:]-b[None,:,:],axis=-1)
        rows,match=linear_sum_assignment(distances)
        maximum=max(maximum,float(distances[rows,match].max()))
    if maximum>0.003: raise ValueError("CIF/NPZ heavy-atom coordinates differ")
    return maximum


def transition(before, after, cdr):
    if before["status"] != "OBSERVED" or after["status"] != "OBSERVED":
        return {"status":"NOT_ASSESSABLE_MISSING_STAGE", "samples":[], "cdr_identity_change_count":None}
    framework = np.array([i for i in range(30,151) if i not in set(cdr)])
    ids0, ids1 = np.asarray(before["residue_ids"]), np.asarray(after["residue_ids"])
    rows = []
    for row in after["samples"]:
        try:
            geometry = compare_geometry(before["samples"][0]["backbone_centroids"], row["backbone_centroids"], cdr, framework)
        except ValueError as error:
            geometry = {"status":"NOT_ASSESSABLE_ALIGNMENT", "reason":str(error)}
        rows.append({**geometry, "both_terminal_contact_before":before["samples"][0]["both_epitope_contacts"],
            "both_terminal_contact_after":row["both_epitope_contacts"]})
    return {"status":"OBSERVED", "cdr_identity_change_count":int(np.sum(ids0[cdr]!=ids1[cdr])),
        "framework_identity_change_count":int(np.sum(ids0[framework]!=ids1[framework])), "samples":rows}


def aggregate(candidates):
    result = {}
    for stage in STAGES:
        rows = [s for c in candidates for s in c["stages"][stage].get("samples", [])]
        result[stage] = {"observed_candidate_count":sum(c["stages"][stage]["status"]=="OBSERVED" for c in candidates),
            "sample_count":len(rows), **{name+"_count":sum(r[name] for r in rows if r[name] is not None) if any(r[name] is not None for r in rows) else None for name in BOOLEAN},
            "contact_metric_applicable_sample_counts":{name:sum(r[name] is not None for r in rows) for name in BOOLEAN},
            "zero_coordinate_cdr_sidechain_atom_count":E._summary([r["zero_coordinate_cdr_sidechain_atom_count"] for r in rows]),
            **{name:E._summary([r[name] for r in rows if r[name] is not None]) for name in NUMERIC},
            "pose_applicable_count":sum(r["pose"]["engineering_domain"]=="WITHIN" for r in rows)}
    result["transitions"] = {}
    for key in ("generation_to_inverse_folding", "inverse_to_free_folding"):
        transitions = [c["transitions"][key] for c in candidates]
        rows = [s for t in transitions for s in t["samples"]]
        measures = ("framework_fit_rmsd_angstrom", "cdr_centroid_rmsd_after_framework_fit_angstrom",
            "cdr_internal_distance_rms_change_angstrom", "framework_relative_cdr_direction_change_degrees")
        result["transitions"][key] = {"candidate_count":len(transitions), "sample_count":len(rows),
            "cdr_identity_change_count":E._summary([t["cdr_identity_change_count"] for t in transitions if t["cdr_identity_change_count"] is not None]),
            "framework_identity_change_count":E._summary([t["framework_identity_change_count"] for t in transitions if t.get("framework_identity_change_count") is not None]),
            **{name:E._summary([r[name] for r in rows if r.get(name) is not None]) for name in measures}}
    return result


def diagnose(index_path, output_dir):
    started = time.perf_counter(); inputs = []; candidates = []
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output directory must be new")
    index = json.loads(read(index_path, inputs))
    if index.get("status") != "COMPUTATION_COMPLETE_PENDING_STRICT_SUMMARY" or [c["condition_id"] for c in index["cells"]] != ["A","B","C"]:
        raise ValueError("expected the completed three-condition continuation INDEX")
    for cell in index["cells"]:
        source, attempt = Path(cell["spec_path"]).parent, Path(cell["attempt_root"])
        source_bytes = {name:read(source/name,inputs) for name in ("design.yaml","scaffold.yaml","target.cif")}
        if any(hashlib.sha256(raw).hexdigest()!=cell["spec_hashes"][name] for name,raw in source_bytes.items()):
            raise ValueError("frozen source changed")
        cdr, _ = cdr_annotation(yaml.safe_load(source_bytes["scaffold.yaml"])); cdr = np.asarray(cdr)
        reference = source_reference(source/"target.cif",yaml.safe_load(source_bytes["design.yaml"]))
        inverse_config = yaml.safe_load(read(attempt/"config/inverse_folding.yaml",inputs))
        fold_config = yaml.safe_load(read(attempt/"config/folding.yaml",inputs))
        generation, inverse = Path(inverse_config["data"]["design_dir"]), Path(fold_config["data"]["design_dir"])
        if generation != attempt/"intermediate_designs" or inverse != attempt/"intermediate_designs_inverse_folded" or Path(inverse_config["output"]) != inverse:
            raise ValueError("stage lineage is not the expected direct pipeline")
        for number in range(2):
            stem = f"design_{number}"; stages = {}
            metadata = {}; cif_folds = {}
            for stage, directory in (("generation",generation),("inverse_folding",inverse)):
                path = directory/(stem+".cif")
                if not path.is_file():
                    stages[stage] = {"status":"NOT_ASSESSABLE_MISSING_CIF", "samples":[]}; continue
                raw = read(path,inputs)
                design, fold = cif_arrays(raw,cdr)
                with np.load(io.BytesIO(read(directory/(stem+".npz"),inputs)),allow_pickle=False) as z:
                    meta = {k:z[k] for k in z.files}
                if not np.array_equal(np.flatnonzero(meta["design_mask"]),cdr) or not np.array_equal(meta["binding_type"][:30],cell["expected_target_binding_types"]):
                    raise ValueError("stage metadata differs from frozen conditions")
                stages[stage] = evaluate_stage(design,fold,reference)
                cif_folds[stage] = fold
                metadata[stage] = meta
            with np.load(io.BytesIO(read(inverse/"fold_out_npz"/(stem+".npz"),inputs)),allow_pickle=False) as z:
                free = {k:z[k] for k in z.files}
            if len(free["coords"]) != 5:
                raise ValueError("exactly five nested free folds required")
            design = metadata.get("inverse_folding",{"design_mask":np.isin(np.arange(151),cdr)})
            stages["free_folding"] = evaluate_stage(design,free,reference)
            linkage = None; atom_linkage = None
            if stages["inverse_folding"]["status"] == "OBSERVED":
                input_stage = evaluate_stage(design,dict(free,coords=free["input_coords"][0]),reference)
                b0=np.asarray(stages["inverse_folding"]["samples"][0]["backbone_centroids"])
                b1=np.asarray(input_stage["samples"][0]["backbone_centroids"])
                linkage=float(np.max(np.linalg.norm(b0-b1,axis=1)))
                if linkage > 0.003 or stages["inverse_folding"]["residue_ids"] != stages["free_folding"]["residue_ids"]:
                    raise ValueError("inverse CIF and free-fold input do not establish the same stage")
                atom_linkage=input_atom_linkage(cif_folds["inverse_folding"],free)
            candidates.append({"candidate_id":cell["condition_id"]+"/"+stem,"condition_id":cell["condition_id"],"stages":stages,
                "inverse_cif_to_fold_input_max_centroid_difference_angstrom":linkage,
                "inverse_cif_to_fold_input_max_heavy_atom_difference_angstrom":atom_linkage,
                "transitions":{"generation_to_inverse_folding":transition(stages["generation"],stages["inverse_folding"],cdr),
                    "inverse_to_free_folding":transition(stages["inverse_folding"],stages["free_folding"],cdr)}})
    public={"schema":"VHH_TERMINAL_INTERFACE_DIAGNOSIS_V1","status":"RETROSPECTIVE_DESCRIPTIVE_DIAGNOSIS_COMPLETE",
        "biological_pass":False,"causal_failure_mechanism_established":False,"contact_threshold_angstrom":4.5,
        "candidate_count":6,"stage_sources":{"generation":"original generation CIF","inverse_folding":"inverse-folded CIF","free_folding":"five NPZ predicted structures"},
        "historical_label_correction":"Old generated_input summaries describe INVERSE_FOLDED input, not RAW_GENERATION; historical files remain unchanged.",
        "inverse_fold_input_correction":"Inverse-fold writer adds zero-coordinate designed side-chain placeholders. CDR heavy-atom contact of those inputs is NOT_ASSESSABLE, not biological loss or a valid zero-contact structure.",
        "inverse_cif_to_fold_input_max_heavy_atom_difference_angstrom":E._summary([c["inverse_cif_to_fold_input_max_heavy_atom_difference_angstrom"] for c in candidates if c["inverse_cif_to_fold_input_max_heavy_atom_difference_angstrom"] is not None]),
        "by_condition":{c:aggregate([r for r in candidates if r["condition_id"]==c]) for c in ("A","B","C")},
        "limitations":["Retrospective diagnosis after seeing these six draws; not a preregistered causal test or new acceptance gate.",
            "Identity is read from each actual stage; inverse-fold input_coords is not the original generation stage.",
            "CDR shape uses four-backbone-atom residue centroids; it does not resolve individual side-chain rotations.",
            "Framework alignment removes rigid-body motion; differences between independent free predictions are not a physical time trajectory.",
            "Target-anchored V2 pose applicability is diagnostic; outside-domain pose is not replaced by a fabricated orientation.",
            "Each condition has two draws; five free folds per draw are nested, not five independent sequences."],
        "implementation_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"analysis_seconds":time.perf_counter()-started}
    private={**public,"inputs":inputs,"candidates":candidates}
    encoded={"PUBLIC_INTERFACE_DIAGNOSIS.json":json.dumps(public,indent=2,sort_keys=True,allow_nan=False),
        "PRIVATE_INTERFACE_DIAGNOSIS.json":json.dumps(private,indent=2,sort_keys=True,allow_nan=False)}
    output.mkdir(parents=True,exist_ok=False)
    for name,payload in encoded.items():
        with (output/name).open("x",encoding="utf-8") as stream: stream.write(payload+"\n")
    return public


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index",type=Path,required=True); parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(); result=diagnose(args.index,args.output)
    print(json.dumps({"status":result["status"],"candidate_count":result["candidate_count"],"analysis_seconds":result["analysis_seconds"]}))
