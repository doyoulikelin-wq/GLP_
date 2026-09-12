#!/usr/bin/env python3
"""Run one frozen local-edit comparison: two sources, three arms, 18 free folds.

Editing is sequence-only IF or one local design step, never automatic full-CDR
IF after inpainting. No retry, no API/network, no model/installation mutations.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import sys
import time

import gemmi
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_vhh_sequence_retention as S
from run_owner_t12_split_template import (FATAL_LOG_RE, RunFailure, compute_processes,
    locate_acceptance, parse_manifest, runtime_contract, utc_now, validate_owner,
    validate_repo, verify_runtime)
from run_vhh_short_peptide_control import validate_calibration_receipt

ARMS = ("ORIGINAL", "SEQUENCE_ONLY", "LOCAL_INPAINT")
TIMEOUT = 1200
FOLDS = 3


def binding(path):
    """Return a private binding without exposing sequence content."""
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": S.sha(path)}


def verify_binding(value):
    """Reject changed or symlinked bound files."""
    path = Path(value["path"])
    if path.is_symlink() or not path.is_file() or S.sha(path) != value["sha256"]:
        raise ValueError("bound source/config file changed")
    return path


def verify_prepared(root, plan):
    """Require the complete, immutable two-source three-arm experiment."""
    if (plan.get("schema") != "VHH_LOCAL_EDIT_PREPARATION_V1"
            or plan.get("cpu_preflight", {}).get("status") != "PASS"
            or plan.get("hard_timeout_seconds") != TIMEOUT
            or plan.get("expected_fold_samples") != 18
            or plan.get("folds_per_task") != FOLDS
            or plan.get("automatic_retry") is not False):
        raise ValueError("frozen local edit scope/CPU contract differs")
    tasks = plan["tasks"]
    if len(tasks) != 6 or {(t["source_draw"], t["arm"]) for t in tasks} != {(i, a) for i in range(2) for a in ARMS}:
        raise ValueError("all six source-arm tasks required")
    if len({t["task_id"] for t in tasks}) != 6:
        raise ValueError("unique task IDs required")
    windows = set()
    for task in tasks:
        if not task["task_id"].replace("_", "").isalnum() or task["fold_samples"] != FOLDS:
            raise ValueError("unsafe task ID or fold count")
        window = task["edit_tokens"]
        if len(window) != 5 or window != list(range(window[0], window[0]+5)) or not set(window) <= set(task["full_cdr_tokens"]):
            raise ValueError("exact five-residue contiguous CDR edit window required")
        if min(window) < 30 or max(window) >= 151 or len(task["expected_residue_ids"]) != 151:
            raise ValueError("target/VHH or edit window differs")
        windows.add(tuple(window))
        verify_binding(task["source_cif"]); verify_binding(task["source_npz"])
        if task["arm"] == "ORIGINAL":
            if task.get("spec") is not None or task.get("config") is not None:
                raise ValueError("original arm cannot have an editing stage")
        else:
            verify_binding(task["spec"]); verify_binding(task["config"])
    if len(windows) != 1:
        raise ValueError("same window required across all six tasks")
    required = {"prepare_vhh_local_edit_20260913.py", "run_vhh_local_edit_20260913.py",
                "score_vhh_local_edit_20260913.py"}
    implementations = plan.get("implementation_sha256", {})
    if not required <= implementations.keys():
        raise ValueError("preparer/runner/scorer must be frozen")
    for name, digest in implementations.items():
        if Path(name).name != name or S.sha(Path(__file__).with_name(name)) != digest:
            raise ValueError("frozen implementation changed")
    for name, digest in plan["output_sha256"].items():
        path = root / name
        if path.is_symlink() or root.resolve() not in path.resolve().parents or S.sha(path) != digest:
            raise ValueError("prepared output changed")
    for name, digest in plan["source_files"].items():
        if Path(name).is_symlink() or S.sha(name) != digest:
            raise ValueError("original source changed")
    verify_binding(plan["target_reference"])


def edit_config(task, output):
    """Read official frozen config and change only its runtime output root."""
    from omegaconf import OmegaConf
    config = OmegaConf.load(verify_binding(task["config"]))
    if (config.diffusion_samples != 1 or config.data.cfg.multiplicity != 1
            or config.data.cfg.skip_existing is not False
            or list(config.data.cfg.yaml_path) != [task["spec"]["path"]]
            or config.trainer.devices != 1):
        raise ValueError("editing config must produce exactly one fresh sample")
    expected_stage = "inverse_folding" if task["arm"] == "SEQUENCE_ONLY" else "design"
    if expected_stage == "inverse_folding":
        if config.override.inverse_fold is not True or config.override.masker_args.mask_backbone is not False:
            raise ValueError("sequence-only arm must use backbone-preserving inverse folding")
    elif config.get("override", {}).get("inverse_fold", False):
        raise ValueError("inpainting must not silently run inverse folding")
    config.output = str(output)
    return OmegaConf.to_container(config, resolve=True), expected_stage


def run_stage(launcher, stage_root, step, config, repo, timeout_seconds):
    """Launch one official stage with a process-group deadline and no retry."""
    if timeout_seconds <= 1:
        raise RunFailure("budget exhausted before stage")
    stage_root.mkdir(parents=True)
    (stage_root / "config").mkdir()
    logs = stage_root / "operator_logs"; logs.mkdir()
    with (stage_root / "config" / (step+".yaml")).open("x") as stream:
        yaml.safe_dump(config, stream, sort_keys=False)
    with (stage_root / "steps.yaml").open("x") as stream:
        yaml.safe_dump({"steps": [{"name": step, "config_file": "config/"+step+".yaml"}]}, stream)
    command = [str(launcher), "execute", str(stage_root), "--no_subprocess", "--steps", step]
    (logs / "command.txt").write_text(shlex.join(command)+"\n")
    env = os.environ.copy(); env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
    started = time.monotonic(); timed_out = False
    with (logs / "stdout.txt").open("xb") as stdout, (logs / "stderr.txt").open("xb") as stderr:
        process = subprocess.Popen(command, cwd=repo, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
            code = 124
    fatal = []
    for path in (logs / "stdout.txt", logs / "stderr.txt"):
        if path.stat().st_size > 100*1024**2:
            fatal.append("oversized-log")
        elif FATAL_LOG_RE.search(path.read_text(errors="replace")):
            fatal.append(path.name)
    receipt = {"step": step, "exit_code": code, "timed_out": timed_out,
               "wall_seconds": time.monotonic()-started, "fatal_log_files": fatal,
               "automatic_retry": False, "status": "COMPLETED" if code == 0 and not fatal and not timed_out else "FAILED"}
    S.write_json(stage_root / "STAGE_RUN.json", receipt)
    if receipt["status"] != "COMPLETED":
        raise RunFailure("official stage failed; no retry")
    return receipt


def output_pair(output):
    """Require exactly one nonempty generated CIF/metadata pair, no native decoy."""
    cifs = list(output.glob("*.cif")); npzs = list(output.glob("*.npz"))
    if (len(cifs) != 1 or len(npzs) != 1 or cifs[0].stem != npzs[0].stem
            or any(p.is_symlink() or p.stat().st_size == 0 for p in cifs+npzs)):
        raise ValueError("editing stage output must be exactly one CIF/NPZ pair")
    return cifs[0], npzs[0]


def check_stage(task, cif, metadata):
    """Check exact edit identity scope and record geometry without scoring dummy atoms."""
    _, before = S.D.cif_arrays(verify_binding(task["source_cif"]).read_bytes(), task["full_cdr_tokens"])
    _, after = S.D.cif_arrays(cif.read_bytes(), task["full_cdr_tokens"])
    ids = after["res_type"][0].argmax(axis=-1)
    expected = np.asarray(task["expected_residue_ids"])
    if not np.array_equal(before["res_type"][0].argmax(axis=-1), expected):
        raise ValueError("source identity differs from frozen plan")
    allowed = np.zeros(151, dtype=bool)
    if task["arm"] != "ORIGINAL": allowed[task["edit_tokens"]] = True
    if np.any(ids[~allowed] != expected[~allowed]):
        raise ValueError("residue identity changed outside allowed edit window")
    with np.load(metadata, allow_pickle=False) as archive:
        meta = {k: archive[k] for k in archive.files}
    intended = task["full_cdr_tokens"] if task["arm"] == "ORIGINAL" else task["edit_tokens"]
    if np.flatnonzero(meta["design_mask"]).tolist() != intended:
        raise ValueError("stage metadata design mask differs from explicit scope")
    centres = []
    for fold in (before, after):
        token = fold["atom_to_token"][0].argmax(axis=-1); bb = fold["backbone_mask"][0]
        if not np.isfinite(fold["coords"]).all():
            raise ValueError("nonfinite generated coordinates")
        centres.append(np.stack([fold["coords"][0, bb & (token == i)].mean(axis=0) for i in range(151)]))
    fixed = np.array([i for i in range(151) if i not in task["edit_tokens"]])
    geometry = S.D.compare_geometry(centres[0], centres[1], np.array(task["edit_tokens"]), fixed)
    backbone = compare_backbone_atoms(Path(task["source_cif"]["path"]), cif, task["edit_tokens"])
    geometry.update(backbone)
    if task["arm"] == "SEQUENCE_ONLY" and backbone["all_backbone_max_displacement_after_fixed_fit_angstrom"] > 0.03:
        raise ValueError("sequence-only stage did not preserve backbone")
    atom_tokens = after["atom_to_token"][0].argmax(axis=-1)
    side = ~after["backbone_mask"][0] & np.isin(atom_tokens, task["full_cdr_tokens"])
    placeholders = int(np.sum(side & np.all(after["coords"][0] == 0, axis=1)))
    record = {"task_id": task["task_id"], "arm": task["arm"], "source_draw": task["source_draw"],
              "changed_residue_count": int(np.sum(ids != expected)), "changed_outside_window": 0,
              "geometry_audit": geometry, "zero_cdr_sidechain_placeholders": placeholders,
              "stage_contact_evaluation": "NOT_USED_AS_FREE_FOLD_EVIDENCE",
              "generated_cif": binding(cif), "generated_npz": binding(metadata)}
    return ids, meta, record


def compare_backbone_atoms(before_path, after_path, window):
    """Audit named N/CA/C/O atoms, not merely four-atom residue centroids."""
    arrays = []
    for path in (before_path, after_path):
        block = gemmi.cif.read_file(str(path)).sole_block()
        atoms = {}
        for chain, number, name, x, y, z in block.find("_atom_site.",
                ["label_asym_id", "label_seq_id", "label_atom_id", "Cartn_x", "Cartn_y", "Cartn_z"]):
            if name in ("N", "CA", "C", "O"):
                token = int(number)-1+(30 if chain == "B" else 0)
                key = (token, name)
                if key in atoms: raise ValueError("duplicate backbone atom")
                atoms[key] = [float(x),float(y),float(z)]
        keys = [(i,name) for i in range(151) for name in ("N","CA","C","O")]
        if set(atoms) != set(keys): raise ValueError("complete named backbone required")
        arrays.append(np.asarray([atoms[key] for key in keys]))
    fixed = ~np.isin(np.repeat(np.arange(151),4),window)
    rotation,centre,target = S.D.rigid_fit(arrays[1][fixed],arrays[0][fixed])
    displacement = np.linalg.norm((arrays[1]-centre)@rotation+target-arrays[0],axis=-1)
    return {"fixed_backbone_atom_rmsd_angstrom":float(np.sqrt(np.mean(displacement[fixed]**2))),
            "fixed_backbone_max_displacement_angstrom":float(displacement[fixed].max()),
            "all_backbone_max_displacement_after_fixed_fit_angstrom":float(displacement.max())}


def target_atoms(plan):
    """Read the single original target coordinate reference for all six final tasks."""
    ref = plan["target_reference"]; path = verify_binding(ref)
    chain = ref.get("target_chain_id", "E")
    block = gemmi.cif.read_file(str(path)).sole_block()
    atoms, names = {}, {}
    for label, number, residue, atom, x, y, z in block.find("_atom_site.",
            ["label_asym_id", "label_seq_id", "label_comp_id", "label_atom_id", "Cartn_x", "Cartn_y", "Cartn_z"]):
        if label == chain and number not in (".", "?") and 1 <= int(number) <= 30:
            key = (int(number), atom)
            if key in atoms: raise ValueError("duplicate target atom")
            atoms[key] = [float(x), float(y), float(z)]; names[int(number)] = residue
    if set(names) != set(range(1, 31)):
        raise ValueError("target reference must contain all 30 residues")
    return atoms, [names[i] for i in range(1, 31)]


def prepare_fold_inputs(plan, products, attempt, runtime):
    """Create a new common target-only, unconditioned six-task free-fold dataset."""
    from hydra.utils import instantiate
    from omegaconf import OmegaConf
    root = attempt / "intermediate_designs"; root.mkdir()
    maps = attempt / "atom_mappings"; maps.mkdir()
    atoms, names = target_atoms(plan); rows = []
    for task, cif, ids, metadata in products:
        task_id = task["task_id"]
        normalized = S.normalize_input(cif.read_bytes(), atoms, names)
        (root / (task_id+".cif")).write_text(normalized)
        metadata = dict(metadata)
        metadata["binding_type"] = np.zeros(151, dtype=np.int64)
        metadata["design_mask"] = np.isin(np.arange(151), task["full_cdr_tokens"])
        metadata.pop("inverse_fold_design_mask", None)
        metadata.pop("aa_constraint_mask", None)
        np.savez_compressed(root / (task_id+".npz"), **metadata)
        rows.append({**task, "residue_ids": ids.tolist(), "cdr_tokens": task["full_cdr_tokens"],
                     "expected_residue_ids": ids.tolist(),
                     "source_residue_ids": task["expected_residue_ids"],
                     "edit_window_tokens": task["edit_tokens"]})
    config = S.control_config(root, runtime); config["diffusion_samples"] = FOLDS
    module = instantiate(OmegaConf.create(config["data"]))
    if len(module.predict_set) != 6: raise ValueError("six unique free-fold inputs required")
    expected = {r["task_id"]: r for r in rows}; seen = set()
    for i in range(6):
        sample = module.predict_set[i]; name = str(sample["id"])
        if name not in expected or name in seen: raise ValueError("free-fold sample identity differs")
        row = expected[name]; mask = sample["template_mask"].numpy()
        if mask.shape != (1,151) or not np.all(mask[0,:30] == 1) or np.any(mask[0,30:]):
            raise ValueError("target-only template violated")
        if np.any(sample["binding_type"].numpy() != 0):
            raise ValueError("free-fold binding conditioning forbidden")
        if sample["res_type"].numpy().argmax(axis=-1).tolist() != row["residue_ids"]:
            raise ValueError("fixed final sequence changed in CPU featurization")
        if np.flatnonzero(sample["design_mask"].numpy()).tolist() != row["cdr_tokens"]:
            raise ValueError("full CDR scoring mask differs")
        # The input binder's geometry must not leak into any visible template.
        structure = gemmi.read_structure(str(root / (name+".cif")))
        for residue in structure[0][1]:
            for atom in residue: atom.pos.x += 57.; atom.pos.y -= 31.
        perturb = attempt / "cpu_only_perturbations"; perturb.mkdir(exist_ok=True)
        alternate = perturb / (name+".cif"); alternate.write_text(structure.make_mmcif_document().as_string())
        other = module.predict_set.get_feat(alternate, sample["design_mask"].numpy())
        keys = [k for k in sample if k.startswith("template_") or k == "visibility_ids"]
        if not keys or any(not np.allclose(sample[k].numpy(), other[k].numpy(), atol=1e-5) for k in keys):
            raise ValueError("binder coordinates leak into free-fold template")
        np.savez_compressed(maps / (name+".npz"), **{k:sample[k].numpy() for k in
            ("atom_to_token", "atom_pad_mask", "ref_atom_name_chars", "res_type")})
        seen.add(name)
    manifest = {str(p.relative_to(attempt)): S.sha(p) for directory in (root,maps) for p in directory.iterdir()}
    S.write_json(attempt / "FINAL_INPUTS.json", {"tasks":rows, "sha256":manifest,
        "cpu_preflight":{"status":"PASS","task_count":6,"fold_sample_count":18,
                         "binder_template_used":False,"binding_type_conditioning_used":False,
                         "binder_displacement_template_invariance":True}})
    return config, rows, manifest


def runtime_assets(plan, runtime):
    """Bind only the used editing/folding checkpoints and molecule dictionary."""
    expected = runtime_contract(runtime)
    available = dict(parse_manifest(runtime / "SHA256SUMS"))
    for task in plan["tasks"]:
        if task["arm"] == "ORIGINAL": continue
        config = yaml.safe_load(verify_binding(task["config"]).read_text())
        path = Path(config["checkpoint"])
        if path.resolve().parent != runtime.resolve() or path.name not in available:
            raise ValueError("editing checkpoint must be inside frozen runtime manifest")
        expected[path.name] = available[path.name]
    return expected


def run(args):
    """Execute the frozen comparison once and write a failure receipt on any stage error."""
    started = utc_now(); start = time.monotonic()
    if args.hard_timeout_seconds != TIMEOUT: raise ValueError("exact frozen 1200 second budget required")
    workspace = args.workspace.resolve(strict=True); repo = args.repo_root.resolve(strict=True)
    prepared = args.prepared.resolve(strict=True); attempt = args.output.absolute()
    if attempt.exists() or attempt.is_symlink(): raise ValueError("new output directory required")
    plan = json.loads((prepared / "PRIVATE_PLAN.json").read_text()); verify_prepared(prepared, plan)
    calibration = validate_calibration_receipt(args.calibration_receipt)
    if plan.get("calibration_private_binding") and verify_binding(plan["calibration_private_binding"]).resolve() != args.calibration_receipt.resolve():
        raise ValueError("calibration differs from preparation")
    commit, tree = validate_repo(repo); validate_owner(workspace / "WINDOWS_OWNER_MODE.json")
    _, accepted = locate_acceptance(workspace); python = Path(accepted["python_bin"])
    if Path(sys.executable).resolve() != python.resolve(): raise ValueError("locally accepted Python required")
    runtime = args.runtime_root.resolve(strict=True)
    if Path(plan["runtime_root"]).resolve() != runtime: raise ValueError("runtime root differs")
    attempt.mkdir(parents=True, mode=0o700)
    receipt = {"schema":"VHH_LOCAL_EDIT_RUN_V1", "status":"FAILED", "started_at_utc":started,
        "plan":binding(prepared / "PRIVATE_PLAN.json"), "calibration_receipt":binding(args.calibration_receipt),
        "commit":commit,"tree":tree,"hard_timeout_seconds":TIMEOUT,"automatic_retry":False,
        "new_9nk9_two_of_two_gate_passed":True,"biological_pass":False,"stages":[]}
    lock = None; before = None; expected = None; code = 1
    try:
        lock = os.open(Path(f"/run/user/{os.getuid()}"), os.O_RDONLY); fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if compute_processes().strip(): raise RunFailure("another GPU compute process active")
        if shutil.disk_usage(attempt).free < 2*1024**3: raise RunFailure("insufficient free disk")
        expected = runtime_assets(plan, runtime); before = verify_runtime(runtime, expected)
        if {k:before[k] for k in calibration["runtime_assets_after"]} != calibration["runtime_assets_after"]:
            raise RunFailure("free-fold runtime differs from actual 9NK9 calibration")
        products = []; launcher = python.parent / "boltzgen-wsl-sm120"
        for task in plan["tasks"]:
            if task["arm"] == "ORIGINAL":
                cif, metadata = verify_binding(task["source_cif"]), verify_binding(task["source_npz"])
            else:
                stage_root = attempt / "editing" / task["task_id"]
                output = stage_root / "products"
                config, step = edit_config(task, output)
                receipt["stages"].append({"task_id":task["task_id"], **run_stage(launcher,stage_root,step,config,repo,TIMEOUT-(time.monotonic()-start)-120)})
                cif, metadata = output_pair(output)
            ids, meta, observed = check_stage(task,cif,metadata)
            products.append((task,cif,ids,meta)); receipt.setdefault("edit_audit",[]).append(observed)
        folding, rows, manifest = prepare_fold_inputs(plan, products, attempt, runtime)
        # Keep standard folding output layout; run_stage creates a separate execution folder.
        fold_run = attempt / "folding_execution"
        receipt["stages"].append(run_stage(launcher,fold_run,"folding",folding,repo,TIMEOUT-(time.monotonic()-start)-120))
        from score_vhh_local_edit_20260913 import score_outputs
        private, public = score_outputs(attempt, {"tasks":rows})
        S.write_json(attempt / "PRIVATE_LOCAL_EDIT_RESULT.json",private)
        S.write_json(attempt / "PUBLIC_LOCAL_EDIT_RESULT.json",public)
        verify_prepared(prepared,plan)
        if any(S.sha(attempt/name) != digest for name,digest in manifest.items()):
            raise RunFailure("fixed free-fold inputs changed")
        receipt.update(status="COMPLETED",result=binding(attempt / "PUBLIC_LOCAL_EDIT_RESULT.json"))
        code = 0
    except Exception as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
    finally:
        if before is not None:
            try:
                after = verify_runtime(runtime,expected)
                receipt.update(runtime_assets_before=before,runtime_assets_after=after,weights_unchanged=before==after)
                if before != after: receipt["status"]="FAILED"; code=1
            except Exception as error: receipt.update(status="FAILED",runtime_error=str(error)); code=1
        elapsed = time.monotonic()-start
        if elapsed > TIMEOUT: receipt.update(status="FAILED",wall_budget_exceeded=True); code=1
        receipt.update(finished_at_utc=utc_now(),actual={"wall_seconds":elapsed,"fold_samples":18 if code==0 else None})
        S.write_json(attempt / "LOCAL_EDIT_RUN.json",receipt)
        if lock is not None: fcntl.flock(lock,fcntl.LOCK_UN); os.close(lock)
    print(json.dumps({"status":receipt["status"],"wall_seconds":elapsed})); return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace","repo-root","prepared","output","runtime-root","calibration-receipt"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--hard-timeout-seconds",type=int,default=TIMEOUT)
    raise SystemExit(run(parser.parse_args()))
