#!/usr/bin/env python3
"""One no-retry 4-task x 2-fold run, gated by new 9NK9 two-of-two recovery."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np
import yaml

sys.path.insert(0,str(Path(__file__).resolve().parent))
import prepare_vhh_sequence_retention as P
from run_owner_t12_split_template import (RunFailure,compute_processes,locate_acceptance,
    run_folding,runtime_contract,scan_fatal_logs,utc_now,validate_owner,verify_runtime)
from run_vhh_short_peptide_control import validate_calibration_receipt


def verify_prepared(root,report):
    if (report.get('schema')!='VHH_SEQUENCE_RETENTION_PREPARATION_V1' or report.get('task_count')!=4
            or report.get('expected_fold_samples')!=8 or report.get('hard_timeout_seconds')!=900
            or report.get('implementation_sha256')!=P.implementations() or report.get('cpu_preflight',{}).get('status')!='PASS'):
        raise ValueError('frozen preparation contract differs')
    tasks=report['tasks']
    if {(t['source_draw'],t['sequence_source']) for t in tasks}!={(i,s) for i in range(2) for s in P.SOURCES} or len(tasks)!=4:
        raise ValueError('all four predetermined sequence tasks required')
    for relative,digest in report['output_sha256'].items():
        path=root/relative
        if path.is_symlink() or root.resolve() not in path.resolve().parents or P.sha(path)!=digest:raise ValueError('prepared output changed')
    for path,digest in report['source_files'].items():
        if P.sha(path)!=digest:raise ValueError('original source changed')


def validated_backbone(fold, bound):
    mapping=np.asarray(fold['atom_to_token'])[0]
    chars=np.asarray(bound['ref_atom_name_chars']).argmax(axis=-1)
    names=[''.join(chr(int(v)+32) for v in row).strip() for row in chars]
    expected=np.isin(names,('N','CA','C','O')) & np.asarray(bound['atom_pad_mask']).astype(bool)
    actual=P.D.E._binary(fold['backbone_mask'],(1,len(expected)),'backbone_mask')[0]
    if not np.array_equal(actual,expected) or not np.all(np.bincount(mapping.argmax(axis=1)[actual],minlength=mapping.shape[1])==4):
        raise ValueError('output backbone mask differs from CPU-bound atom names')
    return actual


def score(attempt,preparation):
    root=attempt/'intermediate_designs';tasks=preparation['tasks'];private=[]
    for directory,suffix in (('fold_out_npz','.npz'),('refold_cif','.cif')):
        paths=list((root/directory).glob('*'+suffix))
        if {p.stem for p in paths}!={t['task_id'] for t in tasks} or any(p.is_symlink() or p.stat().st_size==0 for p in paths):
            raise ValueError('four-task output closure mismatch')
    for task in tasks:
        name=task['task_id'];design,_=P.D.E._load_npz(root/(name+'.npz'));fold,source=P.D.E._load_npz(root/'fold_out_npz'/(name+'.npz'))
        if np.any(design['binding_type']!=0) or np.asarray(fold['coords']).shape[0]!=2:raise ValueError('free evaluation metadata/count mismatch')
        if fold['res_type'][0].argmax(axis=-1).tolist()!=task['residue_ids']:raise ValueError('fixed sequence changed during folding')
        with np.load(attempt/'atom_mappings'/(name+'.npz'),allow_pickle=False) as z:
            if not np.array_equal(fold['atom_to_token'][0],z['atom_to_token']):raise ValueError('output atom map differs from CPU-bound input')
            bb=validated_backbone(fold,z)
        rows=P.D.E.evaluate_arrays(design,fold,target_tokens=list(range(30)))
        mapping=fold['atom_to_token'][0];valid=mapping.sum(axis=1)==1;token=mapping.argmax(axis=-1)
        cdr=np.asarray(design['design_mask']).astype(bool)
        side=valid&~bb&cdr[token]
        if any(np.any(np.all(xyz[side]==0,axis=1)) for xyz in fold['coords']):raise ValueError('predicted CDR retains zero-coordinate placeholders')
        for metric in ('iptm','ptm','design_to_target_iptm','design_ptm'):
            if np.asarray(fold[metric]).shape!=(2,) or not np.isfinite(fold[metric]).all():raise ValueError('invalid output confidence arrays')
        private.append({**task,'samples':rows,'source':source})
    public={'schema':'VHH_SEQUENCE_RETENTION_RESULT_V1','status':'COMPUTATIONAL_SEQUENCE_COMPARISON_COMPLETE',
        'biological_pass':False,'task_count':4,'fold_sample_count':8,'contact_threshold_angstrom':4.5,
        'selection':'ENTIRE_C_ARM_TWO_SOURCE_DRAWS','same_original_target_geometry':True,
        'binding_type_conditioning_used':False,'binder_template_used':False,
        'by_sequence_source':{s:{'source_draw_count':2,'fold_sample_count':4,
            'both_terminal_contact_fold_count':sum(r['both_epitope_contacts'] for t in private if t['sequence_source']==s for r in t['samples']),
            'both_folds_terminal_contact_candidate_count':sum(all(r['both_epitope_contacts'] for r in t['samples']) for t in private if t['sequence_source']==s),
            'any_target_cdr_contact_fold_count':sum(r['any_target_cdr_contact'] for t in private if t['sequence_source']==s for r in t['samples'])} for s in P.SOURCES},
        'limitations':['Two source draws, with two nested stochastic folds per sequence, cannot establish a robust method superiority or binding effect.',
            'RAW_GENERATION and INVERSE_FOLDED name the true sequence provenance; neither input contact geometry is used as a free-fold result.',
            'No active-versus-truncated state comparison or atomically verified terminal chemistry is performed.']}
    return {'public':public,'tasks':private},public


def run(args):
    start=time.monotonic();started=utc_now();workspace=args.workspace.resolve(strict=True);repo=args.repo_root.resolve(strict=True)
    prepared=args.prepared.resolve(strict=True);attempt=args.output.absolute()
    if attempt.exists() or attempt.is_symlink():raise ValueError('run directory must be new')
    if args.hard_timeout_seconds!=900:raise ValueError('frozen 900-second maximum required')
    preparation=json.loads((prepared/'PREPARATION.json').read_text());verify_prepared(prepared,preparation)
    calibration=validate_calibration_receipt(args.calibration_receipt)
    clean=subprocess.run(['git','-C',str(repo),'status','--porcelain'],capture_output=True,text=True,check=True)
    if clean.stdout.strip():raise ValueError('clean repository required before controlled GPU run')
    validate_owner(workspace/'WINDOWS_OWNER_MODE.json');_,accepted=locate_acceptance(workspace)
    python=Path(accepted['python_bin'])
    if Path(sys.executable).resolve()!=python.resolve():raise ValueError('locally accepted Python required')
    attempt.mkdir(parents=True,mode=0o700);logs=attempt/'operator_logs';logs.mkdir()
    receipt={'schema':'VHH_SEQUENCE_RETENTION_RUN_V1','status':'FAILED','started_at_utc':started,
        'preparation':{'path':str(prepared/'PREPARATION.json'),'sha256':P.sha(prepared/'PREPARATION.json')},
        'calibration_receipt':{'path':str(args.calibration_receipt),'sha256':P.sha(args.calibration_receipt)},
        'new_9nk9_two_of_two_gate_passed':True,'biological_pass':False,'automatic_retry':False}
    lock=None;before=None;expected=None;code=1
    try:
        lock=os.open(Path(f'/run/user/{os.getuid()}'),os.O_RDONLY);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if compute_processes().strip():raise RunFailure('another GPU compute process active')
        if shutil.disk_usage(attempt).free<2*1024**3:raise RunFailure('insufficient free disk')
        runtime=args.runtime_root.resolve(strict=True);expected=runtime_contract(runtime);before=verify_runtime(runtime,expected)
        if before!=calibration['runtime_assets_after']:raise RunFailure('runtime assets differ from the new 9NK9 calibration')
        shutil.copytree(prepared/'design_inputs',attempt/'intermediate_designs');shutil.copytree(prepared/'atom_mappings',attempt/'atom_mappings')
        config=P.control_config(attempt/'intermediate_designs',runtime);(attempt/'config').mkdir()
        with (attempt/'config/folding.yaml').open('x') as stream:yaml.safe_dump(config,stream,sort_keys=False)
        with (attempt/'steps.yaml').open('x') as stream:yaml.safe_dump({'steps':[{'name':'folding','config_file':'config/folding.yaml'}]},stream)
        remaining=900-(time.monotonic()-start)-135
        if remaining<=1:raise RunFailure('budget exhausted before launch')
        exitcode,seconds,timeout=run_folding(python.parent/'boltzgen-wsl-sm120',attempt,repo,Path(__file__).resolve().parent,logs,remaining)
        receipt.update(folding_exit_code=exitcode,folding_seconds=seconds,timed_out=timeout)
        if exitcode!=0 or timeout or scan_fatal_logs(logs):raise RunFailure('folding failed, no retry')
        private,public=score(attempt,preparation)
        P.write_json(attempt/'PRIVATE_SEQUENCE_RETENTION_RESULT.json',private);P.write_json(attempt/'PUBLIC_SEQUENCE_RETENTION_RESULT.json',public)
        verify_prepared(prepared,preparation)
        for relative,digest in preparation['output_sha256'].items():
            if relative.startswith('design_inputs/'):
                copied=attempt/'intermediate_designs'/Path(relative).name
            elif relative.startswith('atom_mappings/'):
                copied=attempt/relative
            else:continue
            if P.sha(copied)!=digest:raise RunFailure('copied fixed folding/scoring input changed')
        receipt.update(status='COMPLETED',result={'path':str(attempt/'PUBLIC_SEQUENCE_RETENTION_RESULT.json'),'sha256':P.sha(attempt/'PUBLIC_SEQUENCE_RETENTION_RESULT.json')})
        code=0
    except Exception as error:receipt['error']=f'{type(error).__name__}: {error}'
    finally:
        if before is not None:
            try:
                after=verify_runtime(args.runtime_root,expected);receipt['weights_unchanged']=after==before
                receipt['runtime_before']=before;receipt['runtime_after']=after
                if after!=before:receipt['status']='FAILED';code=1
            except Exception as error:receipt['status']='FAILED';receipt['runtime_error']=str(error);code=1
        elapsed=time.monotonic()-start
        if elapsed>900:receipt['status']='FAILED';receipt['wall_budget_exceeded']=True;code=1
        receipt.update(finished_at_utc=utc_now(),actual={'execution_device':'cuda','fold_samples':8 if code==0 else None,'wall_seconds':elapsed})
        P.write_json(attempt/'SEQUENCE_RETENTION_RUN.json',receipt)
        if lock is not None:fcntl.flock(lock,fcntl.LOCK_UN);os.close(lock)
    print(json.dumps({'status':receipt['status'],'wall_seconds':elapsed}));return code


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('workspace','repo-root','prepared','output','runtime-root','calibration-receipt'):p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--hard-timeout-seconds',type=int,default=900)
    raise SystemExit(run(p.parse_args()))
