#!/usr/bin/env python3
"""Prepare all C-arm raw/IF sequences for a bounded, sequence-retention control.

No GPU. Four fixed-sequence tasks, two free folds each. All tasks use the same
original target geometry; no binder template or binding-type conditioning.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

import gemmi
import numpy as np
import yaml

sys.path.insert(0,str(Path(__file__).resolve().parent))
import diagnose_vhh_terminal_interface as D
from prepare_vhh_native_control import control_config

SOURCES=("RAW_GENERATION","INVERSE_FOLDED")
IMPLEMENTATIONS=("prepare_vhh_sequence_retention.py","run_vhh_sequence_retention.py",
    "diagnose_vhh_terminal_interface.py","evaluate_vhh_epitope.py","prepare_vhh_native_control.py",
    "run_owner_t12_split_template.py","run_vhh_short_peptide_control.py")


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def implementations(): return {name:sha(Path(__file__).with_name(name)) for name in IMPLEMENTATIONS}


def write_json(path,value):
    with Path(path).open("x",encoding="utf-8") as stream:
        json.dump(value,stream,indent=2,sort_keys=True,allow_nan=False);stream.write("\n")


def normalize_input(raw, target_atoms, target_names):
    structure=gemmi.make_structure_from_block(gemmi.cif.read_string(raw.decode()).sole_block())
    if len(structure)!=1 or [c.name for c in structure[0]]!=["A","B"]:
        raise ValueError("require one model with explicit target A / VHH B")
    target,binder=structure[0][0],structure[0][1]
    if len(target)!=30 or len(binder)!=121 or [r.name for r in target]!=target_names:
        raise ValueError("target/VHH identity or length differs")
    for i,residue in enumerate(target,1):
        if {a.name for a in residue}!={name for number,name in target_atoms if number==i}:
            raise ValueError("fixed target atom names differ")
        for atom in residue: atom.pos=gemmi.Position(*target_atoms[(i,atom.name)])
    centre=np.mean([[a.pos.x,a.pos.y,a.pos.z] for r in binder for a in r if a.name=="CA"],axis=0)
    for residue in binder:
        for atom in residue:
            xyz=np.array([atom.pos.x,atom.pos.y,atom.pos.z])-centre+[100.,0,0]
            atom.pos=gemmi.Position(*xyz)
    return structure.make_mmcif_document().as_string()


def preflight(config,tasks,design_dir):
    from hydra.utils import instantiate
    from omegaconf import OmegaConf
    module=instantiate(OmegaConf.create(config["data"]))
    expected={r["task_id"]:r for r in tasks};seen=set();mapfiles={}
    if len(module.predict_set)!=4: raise ValueError("four fixed-sequence CPU tasks required")
    for i in range(4):
        sample=module.predict_set[i];task=str(sample["id"])
        if task not in expected or task in seen: raise ValueError("CPU task identity mismatch")
        mask=sample["template_mask"].numpy()
        if mask.shape!=(1,151) or not np.all(mask[0,:30]==1) or np.any(mask[0,30:]):
            raise ValueError("target-only template violation")
        if np.any(sample["binding_type"].numpy()!=0): raise ValueError("free-fold binding metadata must be all unspecified")
        if np.flatnonzero(sample["design_mask"].numpy()).tolist()!=expected[task]["cdr_tokens"]:
            raise ValueError("CDR annotation changed during featurization")
        ids=sample["res_type"].numpy().argmax(axis=-1)
        if ids.tolist()!=expected[task]["residue_ids"]: raise ValueError("fixed sequence changed in CPU features")
        # Alternate binder displacement must not change ANY visible template tensor.
        path=design_dir/(task+".cif")
        structure=gemmi.read_structure(str(path))
        for residue in structure[0][1]:
            for atom in residue:atom.pos.x+=57.;atom.pos.y-=31.
        perturb=design_dir.parent/"cpu_only_perturbations"/(task+".cif")
        perturb.parent.mkdir(exist_ok=True)
        with perturb.open("x") as stream:stream.write(structure.make_mmcif_document().as_string())
        alternative=module.predict_set.get_feat(perturb,sample["design_mask"].numpy())
        keys=[k for k in sample if k.startswith("template_") or k=="visibility_ids"]
        if not keys or any(not np.allclose(sample[k].numpy(),alternative[k].numpy(),atol=1e-5) for k in keys):
            raise ValueError("binder coordinates leak into visible template features")
        mapfiles[task]={k:sample[k].numpy() for k in ("atom_to_token","atom_pad_mask","ref_atom_name_chars","res_type")}
        seen.add(task)
    return {"status":"PASS","task_count":4,"target_template_tokens":30,"binder_template_tokens":0,
        "binding_types_all_unspecified":True,"binder_displacement_template_invariance":True},mapfiles


def prepare(index_path,output,runtime):
    output=Path(output).absolute();runtime=Path(runtime).resolve(strict=True)
    if output.exists() or output.is_symlink():raise ValueError("preparation output must be new")
    index=json.loads(Path(index_path).read_text())
    if index.get("status")!="COMPUTATION_COMPLETE_PENDING_STRICT_SUMMARY":raise ValueError("original terminal control incomplete")
    cells=[c for c in index["cells"] if c["condition_id"]=="C"]
    if len(cells)!=1:raise ValueError("exactly the entire C condition required")
    cell=cells[0];source=Path(cell["spec_path"]).parent;attempt=Path(cell["attempt_root"])
    sourcefiles={str(Path(index_path).resolve()):sha(index_path)}
    for name,digest in cell["spec_hashes"].items():
        if sha(source/name)!=digest:raise ValueError("original source changed")
        sourcefiles[str(source/name)]=digest
    annotation=yaml.safe_load((source/"scaffold.yaml").read_text());cdr,_=D.cdr_annotation(annotation);cdr=np.asarray(cdr)
    block=gemmi.cif.read_file(str(source/"target.cif")).sole_block();target_atoms={};names={}
    for chain,num,name,atom,x,y,z in block.find("_atom_site.",["label_asym_id","label_seq_id","label_comp_id","label_atom_id","Cartn_x","Cartn_y","Cartn_z"]):
        if chain=="E" and num not in (".","?") and 1<=int(num)<=30:
            key=(int(num),atom)
            if key in target_atoms:raise ValueError("ambiguous source target atom")
            target_atoms[key]=[float(x),float(y),float(z)];names[int(num)]=name
    if set(names)!=set(range(1,31)):raise ValueError("complete original target reference required")
    target_names=[names[i] for i in range(1,31)]
    dirs=(attempt/"intermediate_designs",attempt/"intermediate_designs_inverse_folded")
    if any({p.stem for p in d.glob('design_*.cif')}!={"design_0","design_1"} for d in dirs):
        raise ValueError("entire C arm must contain exactly both original draws")
    tasks=[];prepared=[];pair_checks=[]
    for number in range(2):
        pair=[]
        for label,directory in zip(SOURCES,dirs):
            p=directory/f"design_{number}.cif";m=p.with_suffix('.npz')
            raw=p.read_bytes();design,fold=D.cif_arrays(raw,cdr)
            with np.load(m,allow_pickle=False) as z:metadata={k:z[k] for k in z.files}
            if not np.array_equal(np.flatnonzero(metadata['design_mask']),cdr):raise ValueError("source CDR mismatch")
            metadata['binding_type']=np.zeros(151,dtype=np.int64)
            task=f"c{number}_{label.lower()}";ids=fold['res_type'][0].argmax(axis=-1)
            sequence=''.join(D.E.RESIDUES[int(v)-2] for v in ids[30:])
            tasks.append({'task_id':task,'source_draw':number,'sequence_source':label,'fold_samples':2,
                'cdr_tokens':cdr.tolist(),'residue_ids':ids.tolist(),'vhh_sequence_sha256':hashlib.sha256(sequence.encode()).hexdigest()})
            prepared.append((task,normalize_input(raw,target_atoms,target_names),metadata))
            sourcefiles.update({str(p):sha(p),str(m):sha(m)})
            token=fold['atom_to_token'][0].argmax(axis=-1);bb=fold['backbone_mask'][0]
            centres=np.stack([fold['coords'][0,bb&(token==i)].mean(axis=0) for i in range(151)])
            pair.append((ids,centres))
        framework=np.array([i for i in range(30,151) if i not in set(cdr)])
        if not np.array_equal(pair[0][0][:30],pair[1][0][:30]) or not np.array_equal(pair[0][0][framework],pair[1][0][framework]):
            raise ValueError("raw/IF target or framework sequence differs")
        metrics=D.compare_geometry(pair[0][1],pair[1][1],cdr,framework)
        if metrics['cdr_centroid_rmsd_after_framework_fit_angstrom']>0.003 or metrics['framework_fit_rmsd_angstrom']>0.003:
            raise ValueError("raw/IF backbone equivalence unsupported")
        pair_checks.append({'source_draw':number,'cdr_identity_change_count':int(np.sum(pair[0][0][cdr]!=pair[1][0][cdr])),**metrics})
    output.mkdir(parents=True,mode=0o700);design_dir=output/'design_inputs';design_dir.mkdir();(output/'atom_mappings').mkdir()
    for task,cif,meta in prepared:
        with (design_dir/(task+'.cif')).open('x') as stream:stream.write(cif)
        np.savez_compressed(design_dir/(task+'.npz'),**meta)
    config=control_config(design_dir,runtime)
    with (output/'folding.yaml').open('x') as stream:yaml.safe_dump(config,stream,sort_keys=False)
    check,mappings=preflight(config,tasks,design_dir)
    for task,arrays in mappings.items():np.savez_compressed(output/'atom_mappings'/(task+'.npz'),**arrays)
    outputs={str(p.relative_to(output)):sha(p) for sub in ('design_inputs','atom_mappings','cpu_only_perturbations') for p in (output/sub).glob('*')}
    outputs['folding.yaml']=sha(output/'folding.yaml')
    report={'schema':'VHH_SEQUENCE_RETENTION_PREPARATION_V1','status':'CPU_READY_REQUIRES_NEW_9NK9_CALIBRATION',
        'created_at_utc':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        'task_count':4,'expected_fold_samples':8,'folds_per_task':2,'hard_timeout_seconds':900,'automatic_retry':False,
        'tasks':tasks,'pair_checks':pair_checks,'source_files':sourcefiles,'output_sha256':outputs,
        'implementation_sha256':implementations(),'cpu_preflight':check,'gpu_started':False,'biological_pass':False,
        'selection':'ENTIRE_ORIGINAL_C_ARM_BOTH_DRAWS_NO_BEST_CANDIDATE_SELECTION',
        'input_geometry':'SAME_ORIGINAL_TARGET_FOR_ALL_FOUR_TASKS_BINDER_TRANSLATED_NO_BINDER_TEMPLATE',
        'claim_boundary':'RAW_VS_IF_SEQUENCE_FREE_REFOLD_DIAGNOSTIC_NOT_BINDING_OR_SELECTIVITY',
        'fold_pairing':'candidate source paired; stochastic fold indices not common-random-number pairs'}
    write_json(output/'PREPARATION.json',report)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('index','output','runtime-root'):p.add_argument('--'+n,type=Path,required=True)
    a=p.parse_args();r=prepare(a.index,a.output,a.runtime_root)
    print(json.dumps({k:r[k] for k in ('status','task_count','expected_fold_samples')}))
