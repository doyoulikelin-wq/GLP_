#!/usr/bin/env python3
"""Strict final-free-fold scoring of two source draws by three local-edit arms.

No GPU or writes. Editing-mask provenance is distinct from full-CDR scoring.
Intermediate inverse-fold structures containing dummy side chains are not scored.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import sys

import gemmi
import numpy as np
from boltzgen.data import const

sys.path.insert(0,str(Path(__file__).resolve().parent))
import evaluate_vhh_epitope as E

ARMS=("ORIGINAL","SEQUENCE_ONLY","LOCAL_INPAINT")
TARGET_COUNT=30
TOKEN_COUNT=151
FULL_CDR_TOKENS=tuple([*range(55,63),*range(80,87),*range(125,140)])
TARGET_IDS=tuple(E.RESIDUES.index(name)+2 for name in (
    "HIS","ALA","GLU","GLY","THR","PHE","THR","SER","ASP","VAL","SER","SER","TYR","LEU","GLU",
    "GLY","GLN","ALA","ALA","LYS","GLU","PHE","ILE","ALA","TRP","LEU","VAL","LYS","GLY","ARG"))
NUMERIC=("his_min_cdr_distance_angstrom","ala_min_cdr_distance_angstrom","min_target_cdr_distance_angstrom")
BOOLEAN=("both_epitope_contacts","any_target_cdr_contact","his_contact","ala_contact","severe_close_contact_diagnostic")
ONE_LETTER="ARNDCQEGHILKMFPSTWYV"


def integer_ids(value,name):
    array=np.asarray(value)
    if array.shape!=(TOKEN_COUNT,) or array.dtype.kind not in "iu" or np.any((array<2)|(array>21)):
        raise ValueError(f"{name} must identify 151 canonical residues")
    if tuple(array[:TARGET_COUNT])!=TARGET_IDS:
        raise ValueError(f"{name} must preserve complete active target identity")
    return array


def validate_task(task):
    if task.get("arm") not in ARMS or task.get("source_draw") not in (0,1):
        raise ValueError("task must identify a prespecified source draw and arm")
    if not re.fullmatch(r"[A-Za-z0-9_-]+",task.get("task_id","")):
        raise ValueError("safe task_id required")
    if tuple(task.get("full_cdr_tokens",[]))!=FULL_CDR_TOKENS:
        raise ValueError("scoring must use all original three CDRs, not the editing mask")
    window=np.asarray(task.get("edit_window_tokens",[]))
    if (window.shape!=(5,) or window.dtype.kind not in "iu" or not np.all(np.diff(window)==1)
            or not set(window).issubset(FULL_CDR_TOKENS)):
        raise ValueError("one contiguous five-residue window within original CDRs required")
    source=integer_ids(task["source_residue_ids"],"source_residue_ids")
    expected=integer_ids(task["expected_residue_ids"],"expected_residue_ids")
    outside=np.ones(TOKEN_COUNT,dtype=bool);outside[window]=False
    if not np.array_equal(source[outside],expected[outside]):
        raise ValueError("sequence changed outside the edit window")
    if task["arm"]=="ORIGINAL" and not np.array_equal(source,expected):
        raise ValueError("ORIGINAL arm must preserve the entire raw source sequence")
    return source,expected,window


def validated_backbone(fold,bound,expected):
    mapping=np.asarray(fold["atom_to_token"])[0]
    if not np.array_equal(mapping,bound["atom_to_token"]):
        raise ValueError("fold atom mapping differs from pre-fold CPU binding")
    if not np.array_equal(np.asarray(fold["res_type"])[0],bound["res_type"]):
        raise ValueError("fold residue types differ from pre-fold CPU binding")
    valid=mapping.sum(axis=1)==1
    chars=np.asarray(bound["ref_atom_name_chars"])
    if (chars.shape!=(mapping.shape[0],4,64) or not np.isin(chars,[0,1]).all()
            or not np.all(chars[valid].sum(axis=-1)==1) or np.any(chars[~valid])):
        raise ValueError("invalid CPU-bound atom-name encoding")
    names=np.array(["".join(chr(int(v)+32) for v in row).strip() for row in chars.argmax(axis=-1)])
    pad=E._binary(bound["atom_pad_mask"],(len(valid),),"bound atom_pad_mask")
    if not np.array_equal(valid,pad):
        raise ValueError("CPU-bound atom padding differs from canonical assignments")
    token=mapping.argmax(axis=-1)
    for i,residue in enumerate(expected):
        observed=names[valid&(token==i)].tolist()
        canonical=const.ref_atoms[E.RESIDUES[int(residue)-2]]
        if len(observed)!=len(set(observed)) or set(observed)!=set(canonical):
            raise ValueError("CPU-bound canonical atom identities incomplete or ambiguous")
    bb=E._binary(fold["backbone_mask"],(1,len(valid)),"backbone_mask")[0]
    correct=valid&np.isin(names,("N","CA","C","O"))
    if not np.array_equal(bb,correct) or not np.all(np.bincount(token[bb],minlength=TOKEN_COUNT)==4):
        raise ValueError("backbone mask differs from canonical N/CA/C/O mapping")
    return valid,token,bb


def reject_dummy_sidechains(coords,valid,token,backbone):
    """Reject origin/coincident side-chain placeholders throughout both proteins."""
    side=valid&~backbone
    if np.any(np.all(coords[:,side,:]==0,axis=-1)):
        raise ValueError("zero-coordinate side-chain placeholder in final prediction")
    for t in range(TOKEN_COUNT):
        indices=np.flatnonzero(side&(token==t))
        if len(indices)>1:
            for sample in coords:
                points=sample[indices]
                squared=np.sum((points[:,None,:]-points[None,:,:])**2,axis=-1)
                if np.any(squared[np.triu_indices(len(points),1)]<=1e-12):
                    raise ValueError("coincident side-chain placeholders in final prediction")


def evaluate_candidate(design,fold,task,bound_atom_mapping):
    """Pure array API: exactly 3 final predictions, full CDR and window separate."""
    source,expected,window=validate_task(task)
    mask=E._binary(design["design_mask"],(TOKEN_COUNT,),"input design_mask")
    if tuple(np.flatnonzero(mask)) not in (FULL_CDR_TOKENS,tuple(window)):
        raise ValueError("input editing mask is neither the declared window nor original CDR mask")
    binding=np.asarray(design["binding_type"])
    if binding.shape!=(TOKEN_COUNT,) or not np.all(binding==0):
        raise ValueError("final free-fold binding metadata must be all unspecified")
    coords=np.asarray(fold["coords"])
    if coords.ndim!=3 or coords.shape[0]!=3:
        raise ValueError("exactly three final free folds per candidate required")
    for metric in ("iptm","ptm","design_to_target_iptm","design_ptm"):
        values=np.asarray(fold[metric])
        if values.shape!=(3,) or values.dtype.kind not in "fiu" or not np.isfinite(values).all():
            raise ValueError(f"invalid three-sample confidence array: {metric}")
    full_design={"design_mask":np.isin(np.arange(TOKEN_COUNT),FULL_CDR_TOKENS)}
    # Frozen E checks finite values including padding, complete canonical heavy
    # atom counts, contiguous tokens, protein identities and assigned=resolved.
    full_rows=E.evaluate_arrays(full_design,fold,target_tokens=list(range(TARGET_COUNT)))
    actual=np.asarray(fold["res_type"])[0].argmax(axis=-1)
    if not np.array_equal(actual,expected):
        raise ValueError("predicted fixed sequence differs from expected pre-fold sequence")
    valid,token,bb=validated_backbone(fold,bound_atom_mapping,expected)
    reject_dummy_sidechains(np.asarray(coords,float),valid,token,bb)
    window_rows=E.evaluate_arrays({"design_mask":np.isin(np.arange(TOKEN_COUNT),window)},fold,target_tokens=list(range(TARGET_COUNT)))
    sequence="".join(ONE_LETTER[int(v)-2] for v in actual[30:])
    return {"task_id":task["task_id"],"source_draw":task["source_draw"],"arm":task["arm"],
        "vhh_sequence_sha256":hashlib.sha256(sequence.encode("ascii")).hexdigest(),
        "strict_full_atom_validation":True,"zero_or_coincident_sidechain_placeholders":False,
        "source_sequence_preserved_outside_window":True,"framework_identity_preserved":True,
        "window_substitution_count":int(np.sum(source[window]!=actual[window])),
        "metadata_design_mask_token_count":int(mask.sum()),"scoring_cdr_token_count":30,"editing_window_token_count":5,
        "full_cdr_samples":full_rows,"edit_window_samples":window_rows,
        "full_cdr_candidate_summary":E.candidate_summary(full_rows),
        "edit_window_candidate_summary":E.candidate_summary(window_rows)}


def aggregate_rows(candidates,key):
    rows=[r for c in candidates for r in c[key]]
    return {"source_candidate_count":len(candidates),"free_fold_sample_count":len(rows),
        "folds_nested_per_candidate":3,
        "sample_contact_counts":{name:sum(r[name] is True for r in rows) for name in BOOLEAN},
        "candidate_all_three_contact_counts":{name:sum(all(r[name] is True for r in c[key]) for c in candidates) for name in BOOLEAN},
        "distances_angstrom":{name:E._summary([r[name] for r in rows]) for name in NUMERIC}}


def public_summary(candidates):
    if len(candidates)!=6 or {(c["source_draw"],c["arm"]) for c in candidates}!={(i,a) for i in range(2) for a in ARMS}:
        raise ValueError("complete two-source by three-arm result required")
    if any(len(c["full_cdr_samples"])!=3 or len(c["edit_window_samples"])!=3 for c in candidates):
        raise ValueError("each scoring range requires the same three nested repeats")
    return {"schema":"VHH_LOCAL_EDIT_SCORE_V1","status":"STRICT_FREE_FOLD_DESCRIPTIVE_SCORE_COMPLETE",
        "biological_pass":False,"causal_editing_benefit_established":False,"new_downstream_gpu_authorized":False,
        "evaluation_geometry":"FINAL_FREE_FOLD_PREDICTIONS_ONLY","intermediate_if_contact_status":"NOT_ASSESSABLE_DUMMY_SIDECHAINS_NOT_SCORED",
        "candidate_count":6,"source_draw_count":2,"free_fold_sample_count":18,"contact_threshold_angstrom":4.5,
        "full_cdr_token_count":30,"edit_window_token_count":5,"all_framework_and_outside_window_sequences_preserved":True,
        "strict_full_atom_validation":True,"zero_or_coincident_sidechain_placeholders":False,
        "by_arm":{arm:{"primary_full_cdr":aggregate_rows([c for c in candidates if c["arm"]==arm],"full_cdr_samples"),
            "diagnostic_edit_window":aggregate_rows([c for c in candidates if c["arm"]==arm],"edit_window_samples"),
            "window_substitution_count":E._summary([c["window_substitution_count"] for c in candidates if c["arm"]==arm]),
            "unique_sequence_count":len({c["vhh_sequence_sha256"] for c in candidates if c["arm"]==arm})} for arm in ARMS},
        "limitations":["Two source draws per arm, with three nested predictions per sequence, are not six independent molecules.",
            "Source candidates are matched; stochastic prediction indices are not random-number paired.",
            "The full original three-CDR mask defines primary scoring; the five-residue edit window is diagnostic only.",
            "All predetermined draws are retained, including duplicate sequences and edits with zero substitutions.",
            "His/Ala contacts and all-three contact stability are geometric descriptors, not experimental binding, affinity or selectivity.",
            "No blind lockbox was read; close-contact flags are not atom-type-aware clash scores."]}


def cif_residue_ids(raw):
    structure=gemmi.make_structure_from_block(gemmi.cif.read_string(raw.decode()).sole_block())
    if len(structure)<1:raise ValueError("empty structural CIF")
    identities=[]
    for model in structure:
        if [chain.name for chain in model]!=["A","B"] or len(model[0])!=30 or len(model[1])!=121:
            raise ValueError("CIF requires explicit complete target A and VHH B")
        names=[r.name for c in model for r in c]
        if any(name not in E.RESIDUES for name in names):raise ValueError("noncanonical CIF identity")
        ids=[E.RESIDUES.index(name)+2 for name in names]
        if identities and ids!=identities:raise ValueError("CIF models have inconsistent sequence identities")
        identities=ids
    return integer_ids(identities,"CIF sequence")


def score_outputs(attempt:Path,preparation:dict):
    """Read six final tasks and bound inputs, return (private, aggregate public).

    tasks need source_cif {path,sha256}, original source_residue_ids, final
    expected_residue_ids, source_draw, arm, task_id, full_cdr_tokens and
    edit_window_tokens. atom_mappings/<task>.npz are generated/bound before free
    folding, not fabricated afterward from prediction output.
    """
    attempt=Path(attempt);root=attempt/"intermediate_designs";tasks=preparation["tasks"]
    if len(tasks)!=6 or {(t["source_draw"],t["arm"]) for t in tasks}!={(i,a) for i in range(2) for a in ARMS}:
        raise ValueError("exactly six predetermined tasks required")
    ids=[t["task_id"] for t in tasks]
    if len(set(ids))!=6:raise ValueError("task IDs must be unique")
    if len({tuple(t["edit_window_tokens"]) for t in tasks})!=1:raise ValueError("all arms must use the same five-residue window")
    for directory,suffix in ((root,".npz"),(root/"fold_out_npz",".npz"),(root/"refold_cif",".cif"),(attempt/"atom_mappings",".npz")):
        paths=list(directory.glob("*"+suffix))
        if {p.stem for p in paths}!=set(ids) or any(p.is_symlink() or p.stat().st_size==0 for p in paths):
            raise ValueError("six-task file closure or nonempty output check failed")
    inputs=[];cache={};candidates=[];draw_sources={}
    for task in tasks:
        source,expected,_=validate_task(task)
        binding=task["source_cif"];sourcepath=Path(binding["path"])
        cachekey=(str(sourcepath.resolve()),binding["sha256"])
        if cachekey not in cache:
            if sourcepath.is_symlink() or not sourcepath.is_file():raise ValueError("source must be a regular original CIF")
            raw=sourcepath.read_bytes();digest=hashlib.sha256(raw).hexdigest()
            if digest!=binding["sha256"]:raise ValueError("original source CIF binding changed")
            cache[cachekey]=cif_residue_ids(raw);inputs.append({"path":str(sourcepath.resolve()),"sha256":digest})
        if not np.array_equal(source,cache[cachekey]):raise ValueError("source_residue_ids not derived from actual original CIF")
        previous=draw_sources.setdefault(task["source_draw"],cachekey)
        if previous!=cachekey:raise ValueError("arms do not share the same original source draw")
        name=task["task_id"]
        design,db=E._load_npz(root/(name+".npz"));fold,fb=E._load_npz(root/"fold_out_npz"/(name+".npz"))
        bound,mb=E._load_npz(attempt/"atom_mappings"/(name+".npz"));inputs.extend((db,fb,mb))
        result=evaluate_candidate(design,fold,task,bound)
        cif=(root/"refold_cif"/(name+".cif")).read_bytes()
        if not np.array_equal(cif_residue_ids(cif),expected):raise ValueError("final CIF sequence differs from fixed expected sequence")
        inputs.append({"path":str(root/"refold_cif"/(name+".cif")),"sha256":hashlib.sha256(cif).hexdigest()})
        candidates.append(result)
    public=public_summary(candidates)
    private={**public,"candidates":candidates,"inputs":inputs,"implementation_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    json.dumps(private,allow_nan=False);json.dumps(public,allow_nan=False)
    return private,public
