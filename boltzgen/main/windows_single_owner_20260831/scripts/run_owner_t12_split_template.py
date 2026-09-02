#!/usr/bin/env python3
"""Run one bounded, folding-only T12 split-template GPU attempt.

The runner copies six sealed T11 inverse-folded designs into a new attempt,
runs five Boltz2 folding samples per design with the repository-owned split
template adapter, validates output closure, and seals a local receipt.  It does
not run design generation, inverse folding, analysis, filtering, or BindCraft.
Full structures and logs stay outside Git.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import yaml


SCHEMA = "WINDOWS_OWNER_T12_SPLIT_TEMPLATE_GPU_RUN_V1"
VALIDATION_SCHEMA = "WINDOWS_OWNER_T12_SPLIT_TEMPLATE_VALIDATION_V1"
COMPLETE = "T12_SPLIT_TEMPLATE_COMPLETE"
FAILED = "T12_SPLIT_TEMPLATE_FAILED"
CANDIDATE_IDS = tuple(f"design_{index}" for index in range(6))
FOLD_SAMPLES = 5
EXPECTED_TOKENS = {"target": 30, "cdr": 30, "framework": 91, "total": 151}
MAX_TIMEOUT_SECONDS = 5400
FATAL_LOG_RE = re.compile(
    r"CUDA[^\n]{0,100}out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED|"
    r"(?:Number of )?failed structure predictions:\s*[1-9][0-9]*|"
    r"Traceback \(most recent call last\)",
    re.IGNORECASE,
)
OOM_RE = re.compile(
    r"CUDA[^\n]{0,100}out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED",
    re.IGNORECASE,
)
METRIC_KEYS = ("design_to_target_iptm", "design_ptm", "iptm", "ptm")


class RunFailure(RuntimeError):
    """A controlled failure that must be represented in the terminal receipt."""


def utc_now() -> str:
    """Return a compact UTC timestamp suitable for receipts."""
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    """Hash one regular file without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, content: str) -> None:
    """Create or atomically replace one runner-owned text file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object or fail closed."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunFailure(f"invalid JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunFailure(f"expected JSON object: {path}")
    return value


def command_output(command: Sequence[str], *, cwd: Path | None = None) -> str:
    """Run a short read-only command and return stdout."""
    result = subprocess.run(
        list(command), cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise RunFailure(
            f"command failed ({result.returncode}): {shlex.join(command)}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def parse_manifest(path: Path) -> list[tuple[str, str]]:
    """Parse the project's strict SHA-256 manifest format."""
    if path.is_symlink() or not path.is_file():
        raise RunFailure(f"missing or unsafe manifest: {path}")
    records: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (?:\./)?([^\x00\r\n]+)", line)
        if match is None:
            raise RunFailure(f"invalid manifest row: {line!r}")
        digest, relative = match.groups()
        member = Path(relative)
        if member.is_absolute() or "\\" in relative or any(
            part in {"", ".", ".."} for part in member.parts
        ) or relative in seen:
            raise RunFailure(f"unsafe or duplicate manifest member: {relative!r}")
        seen.add(relative)
        records.append((relative, digest))
    if not records:
        raise RunFailure(f"empty manifest: {path}")
    return records


def verify_manifest(base: Path, manifest: Path) -> dict[str, str]:
    """Replay a manifest without following members outside its base."""
    resolved_base = base.resolve(strict=True)
    verified: dict[str, str] = {}
    for relative, expected in parse_manifest(manifest):
        member = base / relative
        if member.is_symlink() or not member.is_file():
            raise RunFailure(f"missing or unsafe manifest member: {relative}")
        resolved = member.resolve(strict=True)
        if resolved_base not in resolved.parents:
            raise RunFailure(f"manifest member escapes base: {relative}")
        observed = sha256_file(member)
        if observed != expected:
            raise RunFailure(f"SHA-256 mismatch: {member}")
        verified[relative] = observed
    return verified


def validate_owner(marker: Path) -> None:
    """Validate the active Windows owner boundary."""
    payload = json_object(marker)
    expected = {
        "status": "ACTIVE",
        "authority": "WINDOWS_CODEX",
        "training_allowed": False,
        "model_weights_mutable": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise RunFailure(f"owner marker mismatch: {key}")


def locate_acceptance(workspace: Path) -> tuple[Path, dict[str, Any]]:
    """Return the latest sealed LOCAL_ENV_READY receipt."""
    root = workspace / "gpu_work" / "owner_mode" / "local_env_acceptance"
    receipts = sorted(root.glob("*/LOCAL_ENV_ACCEPTANCE.json"), key=lambda p: p.parent.name)
    if not receipts:
        raise RunFailure("no LOCAL_ENV_ACCEPTANCE receipt found")
    receipt = receipts[-1]
    verify_manifest(receipt.parent, receipt.parent / "SHA256SUMS")
    payload = json_object(receipt)
    if payload.get("status") != "LOCAL_ENV_READY" or payload.get("exit_code") != 0:
        raise RunFailure("latest environment receipt is not LOCAL_ENV_READY")
    return receipt, payload


def validate_repo(repo: Path) -> tuple[str, str]:
    """Require a clean repository and return commit/tree identities."""
    if repo.is_symlink() or not (repo / ".git").exists():
        raise RunFailure(f"invalid repository root: {repo}")
    if command_output(["git", "status", "--short"], cwd=repo):
        raise RunFailure("repository must be clean before T12 GPU launch")
    commit = command_output(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    tree = command_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo).strip()
    return commit, tree


def runtime_contract(runtime_root: Path) -> dict[str, str]:
    """Read expected hashes for the two assets used by folding."""
    records = dict(parse_manifest(runtime_root / "SHA256SUMS"))
    required = ("boltz2_conf_final.ckpt", "mols.zip")
    if any(name not in records for name in required):
        raise RunFailure("runtime manifest lacks a T12 folding asset")
    return {name: records[name] for name in required}


def verify_runtime(runtime_root: Path, expected: Mapping[str, str]) -> dict[str, str]:
    """Hash the exact model/dictionary assets used by this run."""
    observed: dict[str, str] = {}
    for name, digest in expected.items():
        path = runtime_root / name
        if path.is_symlink() or not path.is_file():
            raise RunFailure(f"missing runtime asset: {path}")
        observed[name] = sha256_file(path)
        if observed[name] != digest:
            raise RunFailure(f"runtime asset hash mismatch: {name}")
    return observed


def validate_source_t11(source: Path) -> tuple[dict[str, str], str]:
    """Verify the sealed T11 run and return the twelve input hashes."""
    if source.is_symlink() or not source.is_dir():
        raise RunFailure(f"unsafe T11 source attempt: {source}")
    status = (source / "operator_logs" / "STATUS.txt").read_text(encoding="utf-8").strip()
    if status != "ONLY_INVERSE_FOLD_COMPLETE":
        raise RunFailure(f"T11 source is not complete: {status!r}")
    receipt_path = source / "operator_logs" / "ONLY_INVERSE_FOLD_FROM_POSE_SPEC.json"
    receipt = json_object(receipt_path)
    if (
        receipt.get("status") != "ONLY_INVERSE_FOLD_COMPLETE"
        or receipt.get("candidate_count") != len(CANDIDATE_IDS)
        or receipt.get("candidate_ids") != list(CANDIDATE_IDS)
    ):
        raise RunFailure("T11 source receipt candidate contract mismatch")
    verify_manifest(source, source / "operator_logs" / "OUTPUT_SHA256SUMS")
    hashes: dict[str, str] = {}
    for candidate in CANDIDATE_IDS:
        for suffix in (".cif", ".npz"):
            path = source / "intermediate_designs" / f"{candidate}{suffix}"
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise RunFailure(f"missing T11 source input: {path}")
            hashes[path.name] = sha256_file(path)
    return hashes, sha256_file(receipt_path)


def copy_inputs(source: Path, destination: Path, expected: Mapping[str, str]) -> dict[str, str]:
    """Copy sealed inputs to a fresh design directory and verify each copy."""
    destination.mkdir(mode=0o700)
    copied: dict[str, str] = {}
    for name, digest in sorted(expected.items()):
        source_path = source / "intermediate_designs" / name
        output_path = destination / name
        if output_path.exists() or output_path.is_symlink():
            raise RunFailure(f"refusing to overwrite copied input: {output_path}")
        shutil.copy2(source_path, output_path)
        if output_path.is_symlink() or output_path.stat().st_nlink != 1:
            raise RunFailure(f"unsafe copied input: {output_path}")
        copied[name] = sha256_file(output_path)
        if copied[name] != digest:
            raise RunFailure(f"copied input hash mismatch: {name}")
    return copied


def source_input_manifest(source: Path, hashes: Mapping[str, str]) -> dict[str, Any]:
    """Describe the twelve sealed source files for independent replay."""
    return {
        "schema_version": "WINDOWS_OWNER_T12_SOURCE_INPUTS_V1",
        "source_t11_attempt": str(source),
        "files": {
            name: {
                "sha256": digest,
                "size_bytes": (source / "intermediate_designs" / name).stat().st_size,
            }
            for name, digest in sorted(hashes.items())
        },
    }


def build_folding_config(
    design_dir: Path, runtime_root: Path,
) -> dict[str, Any]:
    """Return the frozen folding-only Hydra configuration."""
    return {
        "_target_": "boltzgen.task.predict.predict.Predict",
        "debug": False,
        "data": {
            "_target_": "owner_split_template_data.SplitTemplateFromGeneratedDataModule",
            "cfg": {
                "_target_": "boltzgen.task.predict.data_from_generated.DataConfig",
                "tokenizer": {
                    "_target_": "boltzgen.data.tokenize.tokenizer.Tokenizer",
                    "atomize_modified_residues": False,
                },
                "featurizer": {"_target_": "boltzgen.data.feature.featurizer.Featurizer"},
                "suffix": ".cif",
                "suffix_metadata": ".npz",
                "suffix_native": "_native.cif",
                "samples_per_target": 10**15,
                "num_targets": 10**13,
                "moldir": str(runtime_root / "mols.zip"),
                "batch_size": 1,
                "num_workers": 4,
                "pin_memory": True,
                "disulfide_prob": 1.0,
                "disulfide_on": True,
            },
            "design_dir": str(design_dir),
            "target_templates": True,
            "design_mask_templates": False,
            "expected_target_tokens": EXPECTED_TOKENS["target"],
            "expected_cdr_tokens": EXPECTED_TOKENS["cdr"],
            "expected_framework_tokens": EXPECTED_TOKENS["framework"],
            "return_native": False,
            "fail_if_no_designs": True,
            "output_dir": None,
            "skip_existing": False,
            "skip_existing_kind": "folded",
        },
        "keys_dict_out": [
            "min_interaction_pae", "min_design_to_target_pae", "interaction_pae",
            "ligand_iptm", "protein_iptm", "iptm", "design_iptm", "design_iiptm",
            "design_to_target_iptm", "design_residue_iptm", "design_ptm",
            "target_ptm", "ptm",
        ],
        "writer": {
            "_target_": "boltzgen.task.predict.writer.FoldingWriter",
            "design_dir": "${data.design_dir}",
        },
        "trainer": {
            "accelerator": "gpu", "logger": False, "devices": 1,
            "precision": "bf16-mixed",
        },
        "name": None,
        "output": str(design_dir),
        "checkpoint": str(runtime_root / "boltz2_conf_final.ckpt"),
        "matmul_precision": None,
        "recycling_steps": 3,
        "sampling_steps": 200,
        "diffusion_samples": FOLD_SAMPLES,
        "override": {"validators": None, "use_kernels": True},
    }


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed if the resolved T12 folding contract drifts."""
    data = config.get("data", {})
    trainer = config.get("trainer", {})
    checks = {
        "data_target": data.get("_target_") == (
            "owner_split_template_data.SplitTemplateFromGeneratedDataModule"
        ),
        "target_templates": data.get("target_templates") is True,
        "design_mask_templates": data.get("design_mask_templates") is False,
        "expected_target_tokens": data.get("expected_target_tokens") == 30,
        "expected_cdr_tokens": data.get("expected_cdr_tokens") == 30,
        "expected_framework_tokens": data.get("expected_framework_tokens") == 91,
        "skip_existing": data.get("skip_existing") is False,
        "diffusion_samples": config.get("diffusion_samples") == 5,
        "sampling_steps": config.get("sampling_steps") == 200,
        "recycling_steps": config.get("recycling_steps") == 3,
        "batch_size": data.get("cfg", {}).get("batch_size") == 1,
        "devices": trainer.get("devices") == 1,
        "precision": trainer.get("precision") == "bf16-mixed",
        "kernels": config.get("override", {}).get("use_kernels") is True,
    }
    if not all(checks.values()):
        raise RunFailure(f"folding configuration contract failed: {checks}")
    return checks


def preflight_adapter(config_path: Path, scripts_dir: Path) -> dict[str, Any]:
    """Instantiate the exact Hydra target and inspect all six CPU samples."""
    sys.path.insert(0, str(scripts_dir))
    try:
        from hydra.utils import instantiate
        from omegaconf import OmegaConf

        config = OmegaConf.load(config_path)
        module = instantiate(config.data)
        if len(module.predict_set) != len(CANDIDATE_IDS):
            raise RunFailure("split-template preflight dataset length mismatch")
        rows: list[dict[str, Any]] = []
        for index in range(len(module.predict_set)):
            sample = module.predict_set[index]
            template_mask = sample["template_mask"]
            design_mask = sample["design_mask"].bool()
            slot_sums = [int(value) for value in template_mask.sum(dim=1).tolist()]
            cdr_visible = int(template_mask[:, design_mask].sum().item())
            if tuple(template_mask.shape) != (2, 151) or slot_sums != [30, 91] or cdr_visible:
                raise RunFailure("split-template CPU sample contract mismatch")
            rows.append({
                "id": str(sample["id"]), "template_shape": [2, 151],
                "slot_sums": slot_sums, "cdr_visible": cdr_visible,
            })
        if [row["id"] for row in rows] != list(CANDIDATE_IDS):
            raise RunFailure("split-template CPU sample IDs mismatch")
        return {"status": "PASS", "sample_count": len(rows), "samples": rows}
    finally:
        if sys.path and sys.path[0] == str(scripts_dir):
            sys.path.pop(0)


def design_input_hashes(design_dir: Path) -> dict[str, str]:
    """Hash only the twelve immutable direct inputs."""
    result: dict[str, str] = {}
    for candidate in CANDIDATE_IDS:
        for suffix in (".cif", ".npz"):
            path = design_dir / f"{candidate}{suffix}"
            if path.is_symlink() or not path.is_file():
                raise RunFailure(f"missing copied input after folding: {path.name}")
            result[path.name] = sha256_file(path)
    return result


def validate_fold_outputs(design_dir: Path) -> dict[str, Any]:
    """Validate 6x5 folded samples, finite arrays, masks, and CIF closure."""
    fold_root = design_dir / "fold_out_npz"
    cif_root = design_dir / "refold_cif"
    observed_npz = sorted(path.name for path in fold_root.glob("design_*.npz"))
    observed_cif = sorted(path.name for path in cif_root.glob("design_*.cif"))
    expected_npz = [f"{candidate}.npz" for candidate in CANDIDATE_IDS]
    expected_cif = [f"{candidate}.cif" for candidate in CANDIDATE_IDS]
    if observed_npz != expected_npz or observed_cif != expected_cif:
        raise RunFailure("fold NPZ/CIF candidate closure mismatch")

    rows: list[dict[str, Any]] = []
    total_samples = 0
    for candidate in CANDIDATE_IDS:
        input_npz = design_dir / f"{candidate}.npz"
        fold_npz = fold_root / f"{candidate}.npz"
        cif = cif_root / f"{candidate}.cif"
        if cif.stat().st_size == 0:
            raise RunFailure(f"empty refold CIF: {cif.name}")
        with np.load(input_npz, allow_pickle=False) as design, np.load(
            fold_npz, allow_pickle=False
        ) as fold:
            coords = np.asarray(fold["coords"])
            input_coords = np.asarray(fold["input_coords"])
            if coords.ndim != 3 or coords.shape[0] != FOLD_SAMPLES or coords.shape[2] != 3:
                raise RunFailure(f"fold coords shape mismatch: {candidate} {coords.shape}")
            if input_coords.ndim != 4 or input_coords.shape[0:2] != (1, 1):
                raise RunFailure(f"input_coords shape mismatch: {candidate}")
            if not np.isfinite(coords).all() or not np.isfinite(input_coords).all():
                raise RunFailure(f"non-finite coordinates: {candidate}")
            design_mask = np.asarray(design["design_mask"]) > 0
            if design_mask.shape != (151,) or int(design_mask.sum()) != 30:
                raise RunFailure(f"design mask mismatch: {candidate}")
            atom_to_token = np.asarray(fold["atom_to_token"])[0]
            resolved = np.asarray(fold["atom_resolved_mask"])[0].astype(bool)
            backbone = np.asarray(fold["backbone_mask"])[0] > 0
            if atom_to_token.ndim != 2 or atom_to_token.shape[1] != 151:
                raise RunFailure(f"atom_to_token shape mismatch: {candidate}")
            assignment_count = atom_to_token.sum(axis=1)
            if not np.isin(assignment_count, (0, 1)).all():
                raise RunFailure(f"atom_to_token is not zero/one-hot: {candidate}")
            required_assignment = resolved | backbone
            if not np.all(assignment_count[required_assignment] == 1):
                raise RunFailure(f"resolved/backbone atom lacks token mapping: {candidate}")
            token = atom_to_token.argmax(axis=1)
            target = token < 30
            cdr = design_mask[token]
            framework = (token >= 30) & ~cdr
            counts = {
                "target_backbone_atoms": int((resolved & backbone & target).sum()),
                "cdr_backbone_atoms": int((resolved & backbone & cdr).sum()),
                "framework_backbone_atoms": int((resolved & backbone & framework).sum()),
            }
            if counts != {
                "target_backbone_atoms": 120,
                "cdr_backbone_atoms": 120,
                "framework_backbone_atoms": 364,
            }:
                raise RunFailure(f"backbone atom contract mismatch: {candidate} {counts}")
            for key in METRIC_KEYS:
                values = np.asarray(fold[key])
                if values.shape != (FOLD_SAMPLES,) or not np.isfinite(values).all():
                    raise RunFailure(f"metric contract mismatch: {candidate} {key}")
            total_samples += int(coords.shape[0])
            rows.append({
                "candidate_id": candidate,
                "fold_samples": int(coords.shape[0]),
                "atom_count": int(coords.shape[1]),
                "fold_npz_size_bytes": fold_npz.stat().st_size,
                "refold_cif_size_bytes": cif.stat().st_size,
                **counts,
            })
    if total_samples != len(CANDIDATE_IDS) * FOLD_SAMPLES:
        raise RunFailure("total fold sample count mismatch")
    return {
        "schema_version": VALIDATION_SCHEMA,
        "status": "PASS",
        "candidate_count": len(CANDIDATE_IDS),
        "fold_samples_per_candidate": FOLD_SAMPLES,
        "fold_sample_count": total_samples,
        "finite_arrays": True,
        "token_contract": EXPECTED_TOKENS,
        "candidates": rows,
    }


class GPUMonitor:
    """Write one-second GPU telemetry without interpreting scientific output."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.rows = 0
        self.peak_mib = 0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while self.rows == 0 and self.error is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.rows == 0 or self.error is not None:
            raise RunFailure(f"GPU monitor failed to start: {self.error or 'no rows'}")

    def _run(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow([
                    "observed_at_utc", "name", "memory_total_mib", "memory_used_mib",
                    "utilization_gpu_percent", "temperature_c",
                ])
                stream.flush()
                while not self.stop_event.is_set():
                    result = subprocess.run(
                        [
                            "nvidia-smi",
                            "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                            "--format=csv,noheader,nounits",
                        ],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=10, check=False,
                    )
                    values = [value.strip() for value in result.stdout.strip().split(",")]
                    if result.returncode != 0 or len(values) != 5:
                        self.error = result.stderr.strip() or f"unexpected GPU row: {values!r}"
                        return
                    used = int(float(values[2]))
                    self.peak_mib = max(self.peak_mib, used)
                    writer.writerow([utc_now(), *values])
                    stream.flush()
                    self.rows += 1
                    self.stop_event.wait(1)
        except BaseException as exc:
            self.error = repr(exc)

    def stop(self) -> None:
        if self.thread is None:
            return
        self.stop_event.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RunFailure("GPU monitor did not stop")


def compute_processes() -> str:
    """Return active NVIDIA compute processes as raw CSV."""
    return command_output([
        "nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ])


def run_folding(
    launcher: Path,
    attempt: Path,
    repo: Path,
    scripts_dir: Path,
    logs: Path,
    timeout_seconds: float,
) -> tuple[int, float, bool]:
    """Run folding in a new process group and kill the group on timeout."""
    command = [str(launcher), "execute", str(attempt), "--no_subprocess", "--steps", "folding"]
    atomic_write(logs / "folding.command.txt", shlex.join(command) + "\n")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(scripts_dir)
    started = time.monotonic()
    timed_out = False
    with (logs / "folding.stdout.txt").open("wb") as stdout, (
        logs / "folding.stderr.txt"
    ).open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=repo, env=environment, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=max(1.0, timeout_seconds))
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=30)
            return_code = 124
    duration = time.monotonic() - started
    atomic_write(logs / "folding.exit_code.txt", f"{return_code}\n")
    atomic_write(logs / "folding.duration_seconds.txt", f"{duration:.6f}\n")
    return return_code, duration, timed_out


def scan_fatal_logs(logs: Path) -> list[str]:
    """Return bounded logs with actual fatal signatures, not zero-failure summaries."""
    matches: list[str] = []
    for path in (logs / "folding.stdout.txt", logs / "folding.stderr.txt"):
        if path.is_file() and path.stat().st_size <= 100 * 1024 * 1024:
            text = path.read_text(encoding="utf-8", errors="replace")
            if FATAL_LOG_RE.search(text):
                matches.append(path.name)
    return matches


def run_independent_validator(
    python_bin: Path,
    validator: Path,
    attempt: Path,
    logs: Path,
) -> dict[str, Any]:
    """Run the separately tested output validator and persist its stderr."""
    command = [
        str(python_bin), "-I", str(validator), "validate-run", str(attempt),
        "--source-input-manifest", "operator_logs/SOURCE_INPUTS_AFTER.json",
        "--resolved-config", "config/folding.yaml", "--fold-samples", "5",
    ]
    result = subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    atomic_write(logs / "validation.stderr.txt", result.stderr)
    atomic_write(logs / "validation.exit_code.txt", f"{result.returncode}\n")
    if result.returncode != 0:
        raise RunFailure(f"independent T12 validator failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RunFailure("independent T12 validator emitted invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "PASS":
        raise RunFailure("independent T12 validator did not emit PASS")
    return payload


def seal_output_manifest(attempt: Path) -> tuple[int, int, str]:
    """Seal all local attempt files except the manifest itself."""
    manifest = attempt / "operator_logs" / "OUTPUT_SHA256SUMS"
    directories = attempt / "operator_logs" / "OUTPUT_DIRECTORIES.txt"
    records: list[tuple[str, str]] = []
    directory_rows: list[str] = []
    for path in sorted(attempt.rglob("*"), key=lambda p: p.relative_to(attempt).as_posix()):
        relative = path.relative_to(attempt).as_posix()
        if relative == "operator_logs/OUTPUT_SHA256SUMS":
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            directory_rows.append(relative)
            continue
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise RunFailure(f"unsafe output member: {relative}")
        records.append((relative, sha256_file(path)))
    atomic_write(directories, "".join(f"./{row}\n" for row in directory_rows))
    records = []
    for path in sorted(attempt.rglob("*"), key=lambda p: p.relative_to(attempt).as_posix()):
        relative = path.relative_to(attempt).as_posix()
        if relative == "operator_logs/OUTPUT_SHA256SUMS" or path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise RunFailure(f"unsafe output member: {relative}")
        records.append((relative, sha256_file(path)))
    atomic_write(manifest, "".join(f"{digest}  ./{relative}\n" for relative, digest in records))
    verify_manifest(attempt, manifest)
    total_bytes = sum((attempt / relative).stat().st_size for relative, _ in records)
    return len(records), total_bytes, sha256_file(manifest)


def parse_args() -> argparse.Namespace:
    """Parse the intentionally small public runner interface."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-t11", required=True, type=Path)
    parser.add_argument("--hard-timeout-seconds", type=int, default=MAX_TIMEOUT_SECONDS)
    return parser.parse_args()


def main() -> int:  # noqa: PLR0915
    """Execute one non-reused T12 folding transaction."""
    args = parse_args()
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,95}", args.run_id) is None:
        raise SystemExit("unsafe run ID")
    if not 60 <= args.hard_timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise SystemExit("hard timeout must be in [60, 5400] seconds")

    workspace = args.workspace_root.resolve(strict=True)
    if not str(workspace).startswith("/home/"):
        raise SystemExit("workspace root must be under /home")
    repo = workspace / "GLP_"
    scripts_dir = repo / "boltzgen" / "main" / "windows_single_owner_20260831" / "scripts"
    owner_root = workspace / "gpu_work" / "owner_mode"
    run_root = owner_root / "t12_split_template_gpu" / args.run_id
    attempt_id = f"attempt_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    attempt = run_root / attempt_id
    logs = attempt / "operator_logs"
    started_at = utc_now()
    started_monotonic = time.monotonic()
    monitor: GPUMonitor | None = None
    lock_descriptor: int | None = None
    runtime_root: Path | None = None
    expected_runtime: dict[str, str] | None = None
    runtime_before: dict[str, str] | None = None
    source: Path | None = None
    source_hashes: dict[str, str] | None = None
    design_dir: Path | None = None
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA,
        "status": FAILED,
        "exit_code": 1,
        "authority": "WINDOWS_CODEX",
        "scope": "EXPLORATORY_OVERRIDE_AFTER_CPU_GATE_FAIL",
        "cpu_gate_preserved": {"status": "FAIL", "pass_count": 7, "denominator": 30},
        "user_authorization": "EXPLICIT_T12_GPU_OVERRIDE_IN_CURRENT_TASK",
        "run_id": args.run_id,
        "attempt_id": attempt_id,
        "started_at_utc": started_at,
        "hard_timeout_seconds": args.hard_timeout_seconds,
        "candidate_ids": list(CANDIDATE_IDS),
        "candidate_count": len(CANDIDATE_IDS),
        "fold_samples_per_candidate": FOLD_SAMPLES,
        "requested_fold_sample_count": len(CANDIDATE_IDS) * FOLD_SAMPLES,
        "requested_stages": ["folding"],
        "stages_executed": [],
        "forbidden_stages_started": False,
        "no_auto_retry": True,
        "retry_count": 0,
        "bindcraft_started": False,
        "training_performed": False,
        "scientific_claim_boundary": "AI_RESULTS_ARE_NOT_EXPERIMENTAL_BINDING_EVIDENCE",
    }
    failure_reason = "bootstrap failed before terminal validation"
    exit_code = 1

    try:
        validate_owner(workspace / "WINDOWS_OWNER_MODE.json")
        source = args.source_t11.resolve(strict=True)
        source_hashes, source_receipt_sha = validate_source_t11(source)
        commit, tree = validate_repo(repo)
        acceptance_path, acceptance = locate_acceptance(workspace)
        python_bin = Path(str(acceptance["python_bin"]))
        launcher = python_bin.parent / "boltzgen-wsl-sm120"
        if not os.access(python_bin, os.X_OK) or launcher.is_symlink() or not os.access(launcher, os.X_OK):
            raise RunFailure("accepted Python/BoltzGen launcher is unavailable")
        if Path(sys.executable).resolve() != python_bin.resolve():
            raise RunFailure("runner must use the Python from LOCAL_ENV_ACCEPTANCE")
        runtime_root = (
            workspace / "boltzgen" / "data"
            / "boltzgen_v0_3_2_runtime_and_mvp_inputs_20260819" / "runtime_cache"
        )
        expected_runtime = runtime_contract(runtime_root)
        runtime_before = verify_runtime(runtime_root, expected_runtime)
        if shutil.disk_usage(workspace).free < 2 * 1024**3:
            raise RunFailure("less than 2 GiB workspace disk is free")
        lock_descriptor = os.open(Path(f"/run/user/{os.getuid()}"), os.O_RDONLY)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunFailure("shared single-GPU lock is held") from exc
        compute_before = compute_processes()
        if compute_before.strip():
            raise RunFailure("another GPU compute process is active")
        run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if attempt.exists() or attempt.is_symlink():
            raise RunFailure(f"attempt already exists: {attempt}")
        attempt.mkdir(mode=0o700)
        logs.mkdir(mode=0o700)
        design_dir = attempt / "intermediate_designs"
        copied_before = copy_inputs(source, design_dir, source_hashes)
        atomic_write(
            attempt / "INPUT_SHA256SUMS",
            "".join(f"{digest}  ./intermediate_designs/{name}\n" for name, digest in sorted(copied_before.items())),
        )
        config = build_folding_config(design_dir, runtime_root)
        config_checks = validate_config(config)
        (attempt / "config").mkdir(mode=0o700)
        atomic_write(
            attempt / "config" / "folding.yaml",
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        )
        atomic_write(
            attempt / "steps.yaml",
            yaml.safe_dump(
                {"steps": [{"name": "folding", "config_file": "config/folding.yaml"}]},
                sort_keys=False,
            ),
        )
        adapter_path = scripts_dir / "owner_split_template_data.py"
        validator_path = scripts_dir / "validate_t12_split_template_gpu.py"
        if validator_path.is_symlink() or not validator_path.is_file():
            raise RunFailure("independent T12 validator is missing or unsafe")
        preflight = preflight_adapter(attempt / "config" / "folding.yaml", scripts_dir)
        atomic_write(logs / "adapter_preflight.json", json.dumps(preflight, indent=2, sort_keys=True) + "\n")
        atomic_write(logs / "source_commit.txt", commit + "\n")
        atomic_write(logs / "source_tree.txt", tree + "\n")
        atomic_write(logs / "started_at_utc.txt", started_at + "\n")
        atomic_write(logs / "command.txt", shlex.join([sys.executable, *sys.argv]) + "\n")
        atomic_write(logs / "gpu_compute_processes_before.csv", compute_before)
        source_manifest_before = source_input_manifest(source, source_hashes)
        atomic_write(
            logs / "SOURCE_INPUTS_BEFORE.json",
            json.dumps(source_manifest_before, indent=2, sort_keys=True) + "\n",
        )
        atomic_write(
            logs / "runtime_assets_before.json",
            json.dumps(runtime_before, indent=2, sort_keys=True) + "\n",
        )
        receipt.update({
            "source_commit": commit,
            "source_tree": tree,
            "source_t11_attempt": str(source),
            "source_t11_receipt_sha256": source_receipt_sha,
            "source_input_hashes_before": source_hashes,
            "copied_input_hashes_before": copied_before,
            "local_env_acceptance_sha256": sha256_file(acceptance_path),
            "runtime_assets_before": runtime_before,
            "adapter_sha256": sha256_file(adapter_path),
            "validator_sha256": sha256_file(validator_path),
            "resolved_config_sha256": sha256_file(attempt / "config" / "folding.yaml"),
            "resolved_config_contract": config_checks,
            "adapter_preflight": preflight,
        })
        atomic_write(attempt / "STATUS.txt", "RUNNING\n")
        print(f"T12_FOLDING_START path={attempt}", flush=True)
        receipt["stages_executed"] = ["folding"]
        monitor = GPUMonitor(logs / "gpu_monitor.csv")
        monitor.start()
        remaining = args.hard_timeout_seconds - (time.monotonic() - started_monotonic)
        if remaining <= 1:
            raise RunFailure("hard timeout exhausted before folding")
        folding_budget = min(5250.0, remaining)
        receipt["folding_timeout_seconds"] = folding_budget
        stage_code, folding_duration, timed_out = run_folding(
            launcher, attempt, repo, scripts_dir, logs, folding_budget
        )
        monitor.stop()
        receipt.update({
            "folding_exit_code": stage_code,
            "folding_duration_seconds": folding_duration,
            "timed_out": timed_out,
            "gpu_monitor_rows": monitor.rows,
            "gpu_peak_memory_used_mib": monitor.peak_mib,
        })
        if monitor.error is not None or monitor.rows < 2:
            raise RunFailure(f"GPU monitor contract failed: {monitor.error or 'too few rows'}")
        if stage_code != 0:
            raise RunFailure(f"folding exited with code {stage_code}")
        fatal_logs = scan_fatal_logs(logs)
        receipt["fatal_log_matches"] = fatal_logs
        combined_logs = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (logs / "folding.stdout.txt", logs / "folding.stderr.txt")
            if path.is_file() and path.stat().st_size <= 100 * 1024 * 1024
        )
        receipt["oom_detected"] = bool(OOM_RE.search(combined_logs))
        if fatal_logs:
            raise RunFailure(f"fatal signature in logs: {fatal_logs}")
        internal_validation = validate_fold_outputs(design_dir)
        copied_after = design_input_hashes(design_dir)
        source_after, _ = validate_source_t11(source)
        runtime_after = verify_runtime(runtime_root, expected_runtime)
        if copied_after != copied_before or source_after != source_hashes:
            raise RunFailure("T12 copied inputs or sealed T11 inputs changed")
        if runtime_after != runtime_before:
            raise RunFailure("runtime assets changed during T12")
        source_manifest_after = source_input_manifest(source, source_after)
        if source_manifest_after != source_manifest_before:
            raise RunFailure("T11 source manifest changed during T12")
        atomic_write(
            logs / "SOURCE_INPUTS_AFTER.json",
            json.dumps(source_manifest_after, indent=2, sort_keys=True) + "\n",
        )
        validation = run_independent_validator(
            python_bin, validator_path, attempt, logs
        )
        if validation.get("observed_fold_sample_count") != 30:
            raise RunFailure("independent validator denominator mismatch")
        terminal_commit, terminal_tree = validate_repo(repo)
        if (terminal_commit, terminal_tree) != (commit, tree):
            raise RunFailure("repository identity changed during T12")
        compute_after = compute_processes()
        atomic_write(logs / "gpu_compute_processes_after.csv", compute_after)
        if compute_after.strip():
            raise RunFailure("GPU compute process remained after folding")
        validation.update({
            "input_hashes_unchanged": True,
            "source_t11_hashes_unchanged": True,
            "runtime_hashes_unchanged": True,
            "repository_identity_unchanged": True,
            "gpu_compute_processes_after": 0,
            "oom_detected": False,
        })
        atomic_write(
            logs / "T12_VALIDATION.json",
            json.dumps(validation, indent=2, sort_keys=True) + "\n",
        )
        receipt.update({
            "status": COMPLETE,
            "exit_code": 0,
            "fold_sample_count": validation["observed_fold_sample_count"],
            "output_validation": validation,
            "internal_output_validation": internal_validation,
            "copied_input_hashes_after": copied_after,
            "source_input_hashes_after": source_after,
            "runtime_assets_after": runtime_after,
            "oom_detected": False,
            "timed_out": False,
        })
        exit_code = 0
        failure_reason = ""
    except (RunFailure, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        failure_reason = str(exc)
        if monitor is not None:
            try:
                monitor.stop()
            except Exception as monitor_exc:
                failure_reason += f"; monitor_stop={monitor_exc}"
        receipt.update({"status": FAILED, "exit_code": 1, "failure_reason": failure_reason})
        exit_code = 1
    finally:
        if attempt.exists():
            receipt["ended_at_utc"] = utc_now()
            receipt["total_duration_seconds"] = time.monotonic() - started_monotonic
            receipt["hard_timeout_respected"] = (
                receipt["total_duration_seconds"] <= args.hard_timeout_seconds
            )
            if not receipt["hard_timeout_respected"]:
                receipt["status"] = FAILED
                receipt["exit_code"] = 1
                receipt["timed_out"] = True
                receipt["failure_reason"] = "90-minute total hard timeout exceeded"
                exit_code = 1
            if logs.exists():
                fatal = scan_fatal_logs(logs)
                receipt.setdefault("fatal_log_matches", fatal)
                combined_logs = "\n".join(
                    path.read_text(encoding="utf-8", errors="replace")
                    for path in (logs / "folding.stdout.txt", logs / "folding.stderr.txt")
                    if path.is_file() and path.stat().st_size <= 100 * 1024 * 1024
                )
                receipt.setdefault("oom_detected", bool(OOM_RE.search(combined_logs)))
                try:
                    terminal_compute = compute_processes()
                    atomic_write(logs / "gpu_compute_processes_terminal.csv", terminal_compute)
                    receipt["gpu_compute_processes_terminal"] = (
                        0 if not terminal_compute.strip() else len(terminal_compute.splitlines())
                    )
                    if terminal_compute.strip():
                        receipt["status"] = FAILED
                        receipt["exit_code"] = 1
                        receipt["failure_reason"] = "GPU compute process remained at terminal sealing"
                        exit_code = 1
                except Exception as terminal_gpu_exc:
                    receipt["terminal_gpu_check_error"] = str(terminal_gpu_exc)
                    receipt["status"] = FAILED
                    receipt["exit_code"] = 1
                    exit_code = 1
                if (
                    receipt["status"] != COMPLETE
                    and runtime_root is not None
                    and expected_runtime is not None
                ):
                    try:
                        runtime_terminal = verify_runtime(runtime_root, expected_runtime)
                        receipt["runtime_assets_terminal"] = runtime_terminal
                        receipt["runtime_assets_unchanged_at_terminal"] = (
                            runtime_before is not None and runtime_terminal == runtime_before
                        )
                        if not receipt["runtime_assets_unchanged_at_terminal"]:
                            receipt["status"] = FAILED
                            receipt["exit_code"] = 1
                            receipt["failure_reason"] = "runtime assets changed by terminal sealing"
                            exit_code = 1
                    except Exception as terminal_runtime_exc:
                        receipt["terminal_runtime_check_error"] = str(terminal_runtime_exc)
                        receipt["status"] = FAILED
                        receipt["exit_code"] = 1
                        exit_code = 1
                if (
                    receipt["status"] != COMPLETE
                    and source is not None
                    and source_hashes is not None
                ):
                    try:
                        source_terminal, _ = validate_source_t11(source)
                        receipt["source_t11_unchanged_at_terminal"] = (
                            source_terminal == source_hashes
                        )
                        if not receipt["source_t11_unchanged_at_terminal"]:
                            receipt["status"] = FAILED
                            receipt["exit_code"] = 1
                            receipt["failure_reason"] = "sealed T11 source changed"
                            exit_code = 1
                    except Exception as terminal_source_exc:
                        receipt["terminal_source_check_error"] = str(terminal_source_exc)
                        receipt["status"] = FAILED
                        receipt["exit_code"] = 1
                        exit_code = 1
                if receipt["status"] != COMPLETE and design_dir is not None and design_dir.exists():
                    try:
                        receipt["copied_inputs_terminal"] = design_input_hashes(design_dir)
                    except Exception as terminal_input_exc:
                        receipt["terminal_input_check_error"] = str(terminal_input_exc)
                        receipt["status"] = FAILED
                        receipt["exit_code"] = 1
                        exit_code = 1
            atomic_write(
                logs / "T12_SPLIT_TEMPLATE_GPU.json",
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            )
            if not (logs / "T12_VALIDATION.json").exists():
                atomic_write(
                    logs / "T12_VALIDATION.json",
                    json.dumps({
                        "schema_version": VALIDATION_SCHEMA,
                        "status": "FAIL",
                        "failure_reason": failure_reason,
                    }, indent=2, sort_keys=True) + "\n",
                )
            atomic_write(attempt / "STATUS.txt", f"{receipt['status']}\n")
            try:
                file_count, total_bytes, manifest_sha = seal_output_manifest(attempt)
                print(
                    f"T12_SEALED status={receipt['status']} path={attempt} "
                    f"files={file_count} bytes={total_bytes} manifest_sha256={manifest_sha}",
                    flush=True,
                )
            except Exception as seal_exc:
                print(f"T12_SEAL_FAILED: {seal_exc}", file=sys.stderr, flush=True)
                exit_code = 1
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)

    if exit_code != 0:
        print(f"T12_SPLIT_TEMPLATE_FAILED: {failure_reason}", file=sys.stderr, flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
