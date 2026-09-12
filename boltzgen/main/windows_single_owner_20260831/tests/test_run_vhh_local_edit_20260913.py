"""CPU-only fail-closed contracts for the bounded local-edit runner."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import numpy as np
import pytest
import yaml

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import run_vhh_local_edit_20260913 as R


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    """Minimal bound six-task contract without loading models."""
    for name in ("raw.cif","raw.npz","spec.yaml","config.yaml","target.cif"):
        (tmp_path/name).write_text("bound")
    real_sha=R.S.sha
    monkeypatch.setattr(R.S,"sha",lambda p:"frozen" if Path(p).parent==Path(R.__file__).parent else real_sha(p))
    bind=lambda name:{"path":str(tmp_path/name),"sha256":real_sha(tmp_path/name)}
    tasks=[]
    for source in range(2):
        for arm in R.ARMS:
            tasks.append({"task_id":f"c{source}_{arm.lower()}","source_draw":source,"arm":arm,
                "fold_samples":3,"edit_tokens":[50,51,52,53,54],"full_cdr_tokens":list(range(48,56)),
                "expected_residue_ids":[2]*151,"source_cif":bind("raw.cif"),"source_npz":bind("raw.npz"),
                "spec":None if arm=="ORIGINAL" else bind("spec.yaml"),
                "config":None if arm=="ORIGINAL" else bind("config.yaml")})
    plan={"schema":"VHH_LOCAL_EDIT_PREPARATION_V1","cpu_preflight":{"status":"PASS"},
        "hard_timeout_seconds":1200,"expected_fold_samples":18,"folds_per_task":3,"automatic_retry":False,
        "tasks":tasks,"source_files":{},"output_sha256":{},
        "target_reference":bind("target.cif"),"implementation_sha256":{n:"frozen" for n in
            ("prepare_vhh_local_edit_20260913.py","run_vhh_local_edit_20260913.py","score_vhh_local_edit_20260913.py")}}
    return tmp_path,plan


def test_exact_six_task_contract(prepared):
    """All two-source by three-arm combinations are retained."""
    root,plan=prepared;R.verify_prepared(root,plan)


@pytest.mark.parametrize("fault",["samples","timeout","cpu","retry","arm","duplicate","window","target_edit","code","original_edit","scope"])
def test_changed_scope_rejected(prepared,fault):
    """No incomplete, expanded, or unbound experiment reaches GPU."""
    root,plan=prepared
    if fault=="samples":plan["expected_fold_samples"]=17
    elif fault=="timeout":plan["hard_timeout_seconds"]=1800
    elif fault=="cpu":plan["cpu_preflight"]["status"]="FAIL"
    elif fault=="retry":plan["automatic_retry"]=True
    elif fault=="arm":plan["tasks"][0]["arm"]="EXTRA"
    elif fault=="duplicate":plan["tasks"][1]["task_id"]=plan["tasks"][0]["task_id"]
    elif fault=="window":plan["tasks"][0]["edit_tokens"]=[50,51,52,53]
    elif fault=="target_edit":plan["tasks"][0]["edit_tokens"]=[0,1,2,3,4]
    elif fault=="code":plan["implementation_sha256"]={}
    elif fault=="original_edit":plan["tasks"][0]["spec"]=plan["tasks"][1]["spec"]
    else:plan["tasks"]=plan["tasks"][:-1]
    with pytest.raises(ValueError):R.verify_prepared(root,plan)


def test_changed_source_blocks(prepared):
    """An input mutation is not silently accepted."""
    root,plan=prepared;(root/"raw.cif").write_text("changed")
    with pytest.raises(ValueError,match="changed"):R.verify_prepared(root,plan)


def test_path_escape_blocked(prepared):
    """Prepared file manifests cannot escape the preparation root."""
    root,plan=prepared;plan["output_sha256"]={"../escape":"wrong"}
    with pytest.raises(ValueError,match="changed"):R.verify_prepared(root,plan)


def test_missing_calibration_blocks_before_output(prepared,monkeypatch):
    """A new independent gate requires the real short-peptide receipt."""
    root,plan=prepared;R.S.write_json(root/"PRIVATE_PLAN.json",plan)
    def denied(path):raise ValueError("9NK9 absent")
    monkeypatch.setattr(R,"validate_calibration_receipt",denied)
    args=SimpleNamespace(workspace=root,repo_root=root,prepared=root,output=root/"attempt",
        hard_timeout_seconds=1200,calibration_receipt=root/"absent",runtime_root=root)
    with pytest.raises(ValueError,match="9NK9"):R.run(args)
    assert not args.output.exists()


def config_task(tmp_path,arm="SEQUENCE_ONLY"):
    """Small official-shaped config showing only output-root mutation."""
    spec=tmp_path/"spec.yaml";spec.write_text("entities: []")
    config={"diffusion_samples":1,"output":"/old/output","data":{"cfg":{"multiplicity":1,
        "skip_existing":False,"yaml_path":[str(spec)],"output_dir":"${output}"}},
        "trainer":{"devices":1},"writer":{"output_dir":"${output}"},"override":{
        "inverse_fold":arm=="SEQUENCE_ONLY","masker_args":{"mask_backbone":False}}}
    path=tmp_path/"config.yaml";path.write_text(yaml.safe_dump(config))
    task={"arm":arm,"spec":R.binding(spec),"config":R.binding(path)}
    return task,config


def test_only_runtime_output_changes(tmp_path):
    """Only output interpolation changes; source configuration stays immutable."""
    task,original=config_task(tmp_path);before=R.S.sha(task["config"]["path"])
    config,step=R.edit_config(task,tmp_path/"out")
    assert step=="inverse_folding"
    assert config["output"]==config["writer"]["output_dir"]==config["data"]["cfg"]["output_dir"]==str(tmp_path/"out")
    assert config["data"]["cfg"]["yaml_path"]==original["data"]["cfg"]["yaml_path"]
    assert R.S.sha(task["config"]["path"])==before


@pytest.mark.parametrize("fault",["samples","reuse","backbone","paths","devices"])
def test_invalid_official_config_rejected(tmp_path,fault):
    """Reject unknown scope or sequence-only backbone masking."""
    task,config=config_task(tmp_path)
    if fault=="samples":config["diffusion_samples"]=2
    elif fault=="reuse":config["data"]["cfg"]["skip_existing"]=True
    elif fault=="backbone":config["override"]["masker_args"]["mask_backbone"]=True
    elif fault=="paths":config["data"]["cfg"]["yaml_path"]=["other.yaml"]
    else:config["trainer"]["devices"]=2
    path=Path(task["config"]["path"]);path.write_text(yaml.safe_dump(config));task["config"]=R.binding(path)
    with pytest.raises(ValueError):R.edit_config(task,tmp_path/"out")


def test_inpaint_does_not_add_if_stage(tmp_path):
    """Inpainting executes only design, never a hidden full-CDR sequence step."""
    task,_=config_task(tmp_path,"LOCAL_INPAINT")
    _,step=R.edit_config(task,tmp_path/"out");assert step=="design"


def test_output_pair_exact_one(tmp_path):
    """One pair only, no accidental multiple designs."""
    (tmp_path/"one.cif").write_text("cif");(tmp_path/"one.npz").write_text("npz")
    assert R.output_pair(tmp_path)[0].name=="one.cif"
    (tmp_path/"two.cif").write_text("cif")
    with pytest.raises(ValueError):R.output_pair(tmp_path)


def test_stage_failure_has_receipt_and_no_retry(tmp_path,monkeypatch):
    """A failed child is launched once and preserves a private failure receipt."""
    calls=[]
    class Process:
        pid=101
        def wait(self,timeout):return 9
    def launch(*args,**kwargs):calls.append(args);return Process()
    monkeypatch.setattr(R.subprocess,"Popen",launch)
    with pytest.raises(R.RunFailure,match="no retry"):
        R.run_stage(tmp_path/"launcher",tmp_path/"stage","design",{},tmp_path,10)
    assert len(calls)==1
    record=__import__("json").loads((tmp_path/"stage/STAGE_RUN.json").read_text())
    assert record["status"]=="FAILED" and record["exit_code"]==9


def test_timeout_terminates_process_group_once(tmp_path,monkeypatch):
    """A timeout kills the child group rather than silently retrying the stage."""
    kills=[]
    class Process:
        pid=102
        count=0
        def wait(self,timeout):
            self.count+=1
            if self.count==1:raise subprocess.TimeoutExpired("stage",timeout)
            return -15
    monkeypatch.setattr(R.subprocess,"Popen",lambda *a,**k:Process())
    monkeypatch.setattr(R.os,"killpg",lambda pid,sig:kills.append((pid,sig)))
    with pytest.raises(R.RunFailure):
        R.run_stage(tmp_path/"launcher",tmp_path/"stage","design",{},tmp_path,1.1)
    assert len(kills)==1 and kills[0][0]==102


def test_exhausted_budget_never_launches(tmp_path,monkeypatch):
    """Deadline exhaustion is checked before filesystem or subprocess work."""
    monkeypatch.setattr(R.subprocess,"Popen",lambda *a,**k:pytest.fail("must not launch"))
    with pytest.raises(R.RunFailure,match="budget"):
        R.run_stage(tmp_path/"launcher",tmp_path/"stage","design",{},tmp_path,0)
    assert not (tmp_path/"stage").exists()
