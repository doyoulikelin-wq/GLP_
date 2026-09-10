#!/usr/bin/env python3
"""CPU-only inspection of actual generation conditioning; never run a model.

The checkpoint is memory-mapped once per distinct file. Only hyperparameters and
the tiny binding embedding are accessed; no model is instantiated, weights are
not modified or copied, and no GPU is used. Feature snapshots remain private.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import numpy as np


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inspect_features(binding, residues, design, groups, distance_mask, centres):
    binding, residues = np.asarray(binding), np.asarray(residues)
    design, groups = np.asarray(design, dtype=bool), np.asarray(groups)
    mask, centres = np.asarray(distance_mask), np.asarray(centres, dtype=float)
    n = len(binding)
    if residues.shape != (n,33) or design.shape != (n,) or groups.shape != (n,) or mask.shape != (n,n) or centres.shape != (n,3):
        raise ValueError("unexpected conditioning feature shapes")
    if not np.isfinite(centres).all() or not np.isin(binding,[0,1,2]).all() or not np.isin(mask,[0,1]).all():
        raise ValueError("invalid conditioning feature values")
    active = np.flatnonzero(binding == 1)
    if active.tolist() != [0,1] or residues.argmax(axis=1)[:2].tolist() != [10,2]:
        raise ValueError("binding labels must identify target His7/Ala8 at tokens 0,1")
    if n <= 30 or design[:30].any() or not design[30:].any():
        raise ValueError("expected fixed 30-token target followed by CDR-annotated VHH")
    expected_mask = (groups[:,None] == groups[None,:]) & (groups[:,None] > 0)
    if not np.array_equal(mask,expected_mask):
        raise ValueError("distance mask differs from declared positive structure groups")
    shifted = centres.copy(); shifted[30:] += [100,37,-29]
    original_distances = np.linalg.norm(centres[:,None]-centres[None,:],axis=-1)*mask
    shifted_distances = np.linalg.norm(shifted[:,None]-shifted[None,:],axis=-1)*mask
    difference = float(np.max(np.abs(original_distances-shifted_distances)))
    return {"token_count":n,"target_token_count":30,"cdr_token_count":int(design.sum()),
            "binding_token_indices":active.tolist(),"binding_residue_names":["HIS","ALA"],
            "not_binding_token_indices":np.flatnonzero(binding==2).tolist(),
            "unspecified_target_token_count":int(np.sum(binding[:30]==0)),
            "structure_group_token_counts":{str(int(g)):int(np.sum(groups==g)) for g in np.unique(groups)},
            "target_vhh_visible_distance_pair_count":int(np.count_nonzero(mask[:30,30:])),
            "shifted_vhh_masked_distance_max_difference_angstrom":difference,
            "distance_channel_shift_invariant":difference<1e-6,
            "invariance_scope":"masked token-distance channel only, not full network causal invariance"}


def checkpoint_metadata(path):
    import torch
    from omegaconf import OmegaConf
    before = path.stat()
    checkpoint = torch.load(path,map_location="cpu",mmap=True,weights_only=False)
    hyper = checkpoint["hyper_parameters"]
    convert = lambda value: OmegaConf.to_container(value,resolve=True) if OmegaConf.is_config(value) else dict(value)
    args = convert(hyper.get("embedder_args",{}))
    masker = convert(hyper.get("masker_args",{}))
    key = "input_embedder.binding_specification_conditioning_init.weight"
    weight = checkpoint["state_dict"].get(key)
    summary = {"checkpoint_name":path.name,"add_binding_specification":args.get("add_binding_specification",False),
               "embedder_args":args,"checkpoint_masker_args":masker,"binding_embedding_present":weight is not None,
               "inspection":"CPU mmap metadata and tiny embedding only; no full model instantiation"}
    if weight is not None:
        if weight.ndim != 2 or weight.shape[0] != 3 or not torch.isfinite(weight).all():
            raise ValueError("unexpected binding embedding shape/values")
        summary.update(binding_embedding_shape=list(weight.shape),
                       binding_embedding_row_norms=weight.float().norm(dim=1).tolist(),
                       binding_vs_unspecified_delta_norm=float((weight[1]-weight[0]).float().norm()),
                       not_binding_vs_unspecified_delta_norm=float((weight[2]-weight[0]).float().norm()))
    after = path.stat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
        raise ValueError("checkpoint changed during read-only inspection")
    summary["checkpoint_unchanged_stat"] = True
    return summary


def inspect(pilot_index, output, additional_specs=()):
    import torch
    import boltzgen
    from hydra.utils import instantiate
    from omegaconf import OmegaConf
    from boltzgen.model.modules.masker import BoltzMasker
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new private directory")
    index = json.loads(Path(pilot_index).read_text())
    checkpoints, rows, snapshots, inputs = {}, [], {}, []
    cells = list(index["cells"])
    for i,spec in enumerate(additional_specs):
        cells.append({"cell_id":f"additional_cpu_spec_{i+1}","attempt_root":index["cells"][0]["attempt_root"],
                      "spec_path":str(Path(spec).resolve(strict=True)),"diagnostic_only_spec_override":True})
    for cell in cells:
        path = Path(cell["attempt_root"]) / "config/design.yaml"
        config = OmegaConf.load(path)
        role = "NEW_SPEC_CPU_PREPARATION_NOT_HISTORICAL_RUN" if cell.get("diagnostic_only_spec_override") else "HISTORICAL_CONFIG_CPU_RECONSTRUCTION"
        if cell.get("diagnostic_only_spec_override"):
            config.data.cfg.yaml_path = [str(cell["spec_path"])]
        elif list(config.data.cfg.yaml_path) != [str(cell["spec_path"])]:
            raise ValueError("actual generation config does not identify the indexed spec")
        spec_label = Path(cell["spec_path"]).parent.name
        if not re.fullmatch(r"\d{2}_pdb_[a-z0-9]{8}-[A-Za-z0-9]+",spec_label):
            raise ValueError("public spec label must identify a curated scaffold")
        if config.data._target_ != "boltzgen.task.predict.data_from_yaml.FromYamlDataModule":
            raise ValueError("only the actual FromYaml generation input path is supported")
        checkpoint = Path(config.checkpoint).resolve(strict=True)
        if checkpoint not in checkpoints:
            checkpoints[checkpoint] = checkpoint_metadata(checkpoint)
        metadata = checkpoints[checkpoint]
        effective_args = config.get("override",{}).get("embedder_args",metadata["embedder_args"])
        if not effective_args.get("add_binding_specification",False) or not metadata["binding_embedding_present"]:
            raise ValueError("generation binding branch is not checkpoint-backed and enabled")
        module = instantiate(config.data)
        sample = module.predict_set[0]
        arrays = {key:sample[key].detach().cpu().numpy() for key in
                  ("binding_type","res_type","design_mask","structure_group","token_distance_mask","center_coords")}
        features = inspect_features(arrays["binding_type"],arrays["res_type"],arrays["design_mask"],arrays["structure_group"],arrays["token_distance_mask"],arrays["center_coords"])
        masker_args = dict(config.get("override",{}).get("masker_args",metadata["checkpoint_masker_args"]))
        batch = module.collate([sample])
        masked = BoltzMasker(**masker_args)(batch)
        if not torch.equal(masked["binding_type"],batch["binding_type"]):
            raise ValueError("runtime masker changed binding labels")
        features.update(cell_id=cell["cell_id"],checkpoint_name=checkpoint.name,
                        source_spec_label=spec_label,configuration_role=role,
                        enabled_checkpoint_backed_binding_branch=True,masker_preserves_binding=True,
                        effective_masker_args={"mask_disto":False,**masker_args},
                        generation_starts_from_random_noise_not_input_pose=True)
        rows.append(features); snapshots[f"cell_{len(rows)}_features.npz"] = arrays
        inputs.append({"path":str(path),"sha256":file_digest(path),"spec_path":str(cell["spec_path"]),"spec_sha256":file_digest(cell["spec_path"]),
                       "configuration_role":role,"resolved_cpu_config_sha256":hashlib.sha256(OmegaConf.to_yaml(config,resolve=True).encode()).hexdigest()})
    source_root = Path(boltzgen.__file__).parent
    source_files = ["data/parse/schema.py","task/predict/data_from_yaml.py","data/feature/featurizer.py",
                    "model/modules/masker.py","model/modules/trunk.py","task/predict/predict.py","model/modules/diffusion.py"]
    evidence = {relative:{"sha256":file_digest(source_root/relative)} for relative in source_files}
    public = {"schema":"VHH_GENERATION_CONDITIONING_DIAGNOSTIC_V1","status":"CPU_MECHANISM_CHECK_COMPLETE",
              "cell_count":len(rows),"checkpoint_diagnostics":list(checkpoints.values()),
              "cells":[{k:v for k,v in row.items() if k!="cell_id"} for row in rows],
              "source_implementation":evidence,"gpu_started":False,"training_performed":False,"model_weights_modified":False,
              "conclusion":"GENERATION_CONSUMES_TRAINED_BINDING_CONDITION_BUT_CONTACT_ADHERENCE_NOT_GUARANTEED",
              "causal_explanation_of_observed_c_terminal_contacts":"NOT_ESTABLISHED_NO_GENERATION_ABLATION_RUN",
              "limitations":["Soft token conditioning is not a hard contact constraint.",
                             "Remainder of target is UNSPECIFIED, not NOT_BINDING.",
                             "Masked geometry-channel invariance does not prove whole-network causal invariance.",
                             "Fresh CPU features check the actual config path but are not captured historical GPU batches.",
                             "No candidate generation or folding was run; no binding or selectivity conclusion."]}
    private = {**public,"pilot_index":{"path":str(pilot_index),"sha256":file_digest(pilot_index)},"inputs":inputs,"cells":rows,
               "checkpoint_paths":[str(path) for path in checkpoints]}
    output.mkdir(parents=True,exist_ok=False)
    for name,arrays in snapshots.items():
        with (output/name).open("xb") as stream: np.savez_compressed(stream,**arrays)
    for name,result in (("PUBLIC_CONDITIONING_DIAGNOSTIC.json",public),("PRIVATE_CONDITIONING_DIAGNOSTIC.json",private)):
        with (output/name).open("x",encoding="utf-8") as stream: json.dump(result,stream,indent=2,sort_keys=True,allow_nan=False)
    return public


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-index",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--additional-spec",type=Path,action="append",default=[])
    args=parser.parse_args()
    result=inspect(args.pilot_index,args.output,args.additional_spec)
    print(json.dumps({k:result[k] for k in ("status","cell_count","conclusion","gpu_started")}))


if __name__=="__main__": main()
