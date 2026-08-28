"""Contract and adversarial tests for the deterministic AIV1 input gate."""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_ai_validation_matrix import (  # noqa: E402
    ANCHOR_FIELDS,
    STATE_FIELDS,
    ContractViolation,
    build_task_rows,
    canonical_json,
    candidate_id_set_sha256,
    materialize_matrix_bundle,
    sha256_file,
    sha256_text,
    validate_inputs,
)


CANONICAL_CONTRACT_DIRECTORY = Path("boltzgen/resources/data/AIV1技术门合同_20260828")

INVENTORY_FIELDS = (
    "cohort_id",
    "source_id",
    "source_pdb",
    "relative_path",
    "sha256",
    "coordinate_sha256",
    "status",
    "active_for_ai",
    "parse_status",
    "geometry_complete",
    "independence_group",
    "experimental_negative",
    "binding_label",
)

FOLD_SCORE_FIELDS = (
    "iptm",
    "ptm",
    "design_to_target_iptm",
    "design_ptm",
    "min_design_to_target_pae",
    "min_interaction_pae",
)


def write_tsv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def minimal_candidate_mmcif(candidate_id: str, sequence: str) -> str:
    """Return a small, standards-shaped mmCIF carrying one canonical polymer."""

    return (
        f"data_{candidate_id}\n"
        "#\n"
        "_entity.id 1\n"
        "_entity.type polymer\n"
        "#\n"
        "_entity_poly.entity_id 1\n"
        "_entity_poly.type 'polypeptide(L)'\n"
        f"_entity_poly.pdbx_seq_one_letter_code_can {sequence}\n"
        "#\n"
        "_struct_asym.id A\n"
        "_struct_asym.entity_id 1\n"
        "#\n"
    )


class SyntheticAIV1Fixture:
    """A complete synthetic analogue of the canonical G2-to-AIV1 handoff."""

    def __init__(self, root: Path, *, include_unselected_lockbox: bool = False):
        self.root = root
        self.repo = root / "repo"
        self.workspace = root / "workspace"
        self.project = self.workspace / "boltzgen"
        self.repo.mkdir()
        self.project.mkdir(parents=True)

        self.contract_directory = self.repo / CANONICAL_CONTRACT_DIRECTORY
        self.contract_directory.mkdir(parents=True)
        self.contract_path = self.contract_directory / "aiv1_input_contract.json"
        self.state_path = self.contract_directory / "development_state_contract.tsv"
        self.registry_schema_path = (
            self.contract_directory / "aiv1_experience_registry_schema.sql"
        )

        self.aiv0_data_directory = self.project / "data/aiv0_fixture"
        self.aiv0_data_directory.mkdir(parents=True)
        self.inventory_path = self.aiv0_data_directory / "structure_inventory.tsv"
        self.aiv0_attempt_directory = (
            self.project / "runs/glp1_vhh_formal_campaign_20260828/logs/stages/"
            "aiv0_asset_validation/attempt_007"
        )
        self.aiv0_attempt_directory.mkdir(parents=True)
        self.aiv0_receipt_path = self.aiv0_attempt_directory / "receipt.json"
        self.aiv0_derived_manifest_path = (
            self.aiv0_attempt_directory / "derived_outputs.SHA256SUMS"
        )
        self.aiv0_summary_path = (
            self.repo / "boltzgen/resources/data/AI结构资产验证登记册_20260828/"
            "AIV0验证摘要_20260828.json"
        )
        self.aiv0_summary_path.parent.mkdir(parents=True)

        self.acceptance_root = self.project / "runs/acceptance"
        self.acceptance_directory = self.acceptance_root / "7xl0_adherence__attempt_001"
        self.acceptance_log_directory = self.acceptance_directory / "operator_logs"
        self.acceptance_log_directory.mkdir(parents=True)
        self.aggregate_path = (
            self.acceptance_directory
            / "intermediate_designs_inverse_folded/aggregate_metrics_analyze.csv"
        )
        self.output_manifest_path = self.acceptance_log_directory / "output_SHA256SUMS"
        self.acceptance_contract_path = (
            self.acceptance_log_directory / "cell_contract.json"
        )
        self.acceptance_config_manifest_path = (
            self.acceptance_log_directory / "resolved_config_SHA256SUMS"
        )
        self.acceptance_success_path = (
            self.acceptance_log_directory / "cell.SUCCESS.json"
        )

        self.g2_gate_path = self.acceptance_root / "G2_acceptance_gate.json"
        self.g2_resource_status_path = (
            self.acceptance_root / "G2_resource_probe.status.txt"
        )
        self.resource_summary_path = (
            self.acceptance_root / "6xym_batch5_resource_summary.txt"
        )
        self.anchor_release_directory = self.acceptance_root / "aiv1_anchor_release_v1"
        self.anchor_release_directory.mkdir(parents=True)
        self.anchor_path = self.anchor_release_directory / "anchor_candidate_set_v1.tsv"
        self.g2_path = self.anchor_release_directory / "g2_anchor_release_receipt.json"

        self.provenance_directory = self.project / "provenance"
        self.provenance_directory.mkdir()
        self.platform_path = self.provenance_directory / "platform_evidence.json"
        self.spec_gate_bundle_path = self.provenance_directory / "spec_gate_bundle.tar"
        self.model_inputs_manifest_path = (
            self.provenance_directory / "model_inputs_SHA256SUMS"
        )
        self.runtime_scripts_manifest_path = (
            self.provenance_directory / "gpu_runtime_scripts_SHA256SUMS"
        )
        self.environment_manifest_path = (
            self.project / "environment_provenance.SHA256SUMS"
        )

        self.generation = {
            "source_stage": "STEP8_G2_ACCEPTANCE",
            "generation_cell_id": "7xl0_adherence__attempt_001",
            "shard_id": "acceptance",
            "scaffold_id": "01_pdb_00007xl0-A",
            "scaffold_sha256": (
                "68a4c9545a51c56f652c503c94e572e0" "35556998bb3a83d78b99ad80ae1a97d2"
            ),
            "checkpoint_id": "adherence",
            "checkpoint_sha256": (
                "ac7078b3dc13064c68e0c3fd542e5bc5" "38c33558bf6607f65e499eb336ca5e5d"
            ),
            "platform_class": "LINUX_NVIDIA",
        }
        self.config_sha = sha256_text("resolved-g2-config")
        self.code_sha = sha256_text("formal-g2-code")
        self.environment_sha = sha256_text("formal-linux-nvidia-environment")
        self.probe_paths: dict[str, dict[str, Path]] = {}
        self.candidate_paths: list[Path] = []

        self._build(include_unselected_lockbox=include_unselected_lockbox)

    def workspace_uri(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.workspace.resolve())
        return f"workspace://{relative.as_posix()}"

    def repo_uri(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.repo.resolve())
        return f"repo://{relative.as_posix()}"

    def _build(self, *, include_unselected_lockbox: bool) -> None:
        self._build_states_and_aiv0(
            include_unselected_lockbox=include_unselected_lockbox
        )
        self._build_provenance()
        self._build_candidates_and_aggregate()
        self._build_acceptance_contract()
        self._build_resource_probes()
        self.g2_resource_status_path.write_text("PASS\n", encoding="utf-8")
        self.resource_summary_path.write_text(
            "diverse peak_memory_fraction=0.50\n"
            "adherence peak_memory_fraction=0.50\n",
            encoding="utf-8",
        )
        self.write_acceptance_output_manifest()
        self.rebind_g2()

    def _build_states_and_aiv0(self, *, include_unselected_lockbox: bool) -> None:
        state_rows: list[dict[str, str]] = []
        inventory_rows: list[dict[str, str]] = []
        roles = (
            ["positive_primary"]
            + ["positive_fixed_control"]
            + ["positive_compact_medoid"] * 3
            + ["tuning_primary_truncation"]
            + ["tuning_family_glp2"] * 10
        )
        cohorts = (
            ["positive_6x18_reference"]
            + ["positive_1d0r_all"] * 4
            + ["countertarget_glp1_9_36_9ivm"]
            + ["challenge_glp2_2l63"] * 10
        )
        statuses = (
            ["USE_PRIMARY"]
            + ["USE_POSITIVE_FIXED_CONTROL"]
            + ["USE_POSITIVE_COMPACT"] * 3
            + ["USE_TUNING_CHALLENGE"] * 11
        )
        conformers = [
            "6X18_model01",
            "1D0R_model10",
            "1D0R_model12",
            "1D0R_model19",
            "1D0R_model20",
            "9IVM_model01",
        ] + [f"2L63_model{i:02d}" for i in range(1, 11)]
        identities = (
            ["GLP1_7_36_6X18"]
            + ["GLP1_7_36_1D0R"] * 4
            + ["GLP1_9_36_9IVM"]
            + ["GLP2_1_33_2L63"] * 10
        )
        weights = ["", "", "6", "10", "4"] + [""] * 11

        registered_aliases = (
            (
                "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820",
                "boltzgen/runs/old12_glp1_mac_enhanced_20260820",
                "../../boltzgen/runs/old12_glp1_mac_enhanced_20260820",
            ),
            (
                "data/样本数据/binding-多构象",
                "shared/data/glp1_positive_conformer_ensemble_20260819",
                "../../shared/data/glp1_positive_conformer_ensemble_20260819",
            ),
            (
                "data/not_binding",
                "shared/data/glp2_tuning_countertargets_20260824",
                "../shared/data/glp2_tuning_countertargets_20260824",
            ),
        )
        for alias_name, target_name, link_text in registered_aliases:
            alias = self.workspace / alias_name
            target_root = self.workspace / target_name
            alias.parent.mkdir(parents=True, exist_ok=True)
            target_root.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(link_text)

        for index in range(16):
            if index == 0:
                relative = (
                    "data/boltzgen_data/"
                    "boltzgen_mac_enhanced_old12_glp1_20260820/inputs/target/"
                    "6X18_GLP1_7-36_geometry.cif"
                )
            elif index < 5:
                relative = (
                    "data/样本数据/binding-多构象/all_conformers/"
                    f"{conformers[index]}.cif"
                )
            elif index == 5:
                relative = "data/not_binding/GLP1_9_36/GLP1_9_36_reference_conf01.cif"
            else:
                relative = (
                    "data/not_binding/GLP2_1_33/" f"GLP2_1_33_conf{index - 5:02d}.cif"
                )
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"synthetic-target-{index}".encode())
            source_id = (
                "6X18"
                if index == 0
                else "1D0R" if index < 5 else "9IVM" if index == 5 else "2L63"
            )
            source_pdb = "" if index < 5 else source_id
            independence = f"PDB:{source_id}"
            file_sha = sha256_file(target)
            coordinate_sha = sha256_text(f"coordinates-{index}")
            state_rows.append(
                {
                    "state_order": str(index),
                    "target_state_id": f"DEV_{index:02d}",
                    "panel_role": roles[index],
                    "target_identity": identities[index],
                    "source_id": source_id,
                    "source_pdb": source_pdb,
                    "cohort_id": cohorts[index],
                    "conformer_id": conformers[index],
                    "independence_group": independence,
                    "relative_path": relative,
                    "sha256": file_sha,
                    "coordinate_sha256": coordinate_sha,
                    "required_status": statuses[index],
                    "required_active_for_ai": "true",
                    "required_parse_status": "PASS",
                    "required_geometry_complete": "true",
                    "compact_cluster_weight": weights[index],
                }
            )
            inventory_rows.append(
                {
                    "cohort_id": cohorts[index],
                    "source_id": source_id,
                    "source_pdb": source_pdb,
                    "relative_path": relative,
                    "sha256": file_sha,
                    "coordinate_sha256": coordinate_sha,
                    "status": statuses[index],
                    "active_for_ai": "true",
                    "parse_status": "PASS",
                    "geometry_complete": "true",
                    "independence_group": independence,
                    "experimental_negative": "false",
                    "binding_label": "unknown_or_not_applicable",
                }
            )

        if include_unselected_lockbox:
            lockbox_path = self.workspace / "data/unselected_lockbox.cif"
            lockbox_path.write_text("data_unselected_lockbox\n#\n", encoding="utf-8")
            inventory_rows.append(
                {
                    "cohort_id": "lockbox_gip_2b4n",
                    "source_id": "2B4N",
                    "source_pdb": "2B4N",
                    "relative_path": "data/unselected_lockbox.cif",
                    "sha256": sha256_file(lockbox_path),
                    "coordinate_sha256": sha256_text("unselected-lockbox-coordinates"),
                    "status": "USE_LOCKBOX",
                    "active_for_ai": "true",
                    "parse_status": "PASS",
                    "geometry_complete": "true",
                    "independence_group": "PDB:2B4N",
                    "experimental_negative": "false",
                    "binding_label": "unknown_or_not_applicable",
                }
            )

        write_tsv(self.state_path, STATE_FIELDS, state_rows)
        write_tsv(self.inventory_path, INVENTORY_FIELDS, inventory_rows)
        self.registry_schema_path.write_text(
            "PRAGMA foreign_keys = ON;\n-- synthetic canonical registry fixture\n",
            encoding="utf-8",
        )
        contract = {
            "schema_version": "AIV1_INPUT_CONTRACT_V1",
            "campaign_type": "AIV1_TECHNICAL_GATE",
            "development_state_contract_sha256": sha256_file(self.state_path),
            "experience_registry_schema_sha256": sha256_file(self.registry_schema_path),
            "candidate_count": 10,
            "logical_states_per_candidate": 16,
            "fold_run": 1,
            "samples_per_task": 5,
            "expected_logical_tasks": 160,
            "expected_sample_rows": 800,
            "generation_contract": self.generation,
            "state_role_counts": {
                "positive_primary": 1,
                "positive_fixed_control": 1,
                "positive_compact_medoid": 3,
                "tuning_primary_truncation": 1,
                "tuning_family_glp2": 10,
            },
            "allowed_cohorts": [
                "positive_6x18_reference",
                "positive_1d0r_all",
                "countertarget_glp1_9_36_9ivm",
                "challenge_glp2_2l63",
            ],
            "forbidden_cohort_markers": ["lockbox", "quarantine"],
            "forbidden_target_markers": ["2B4N", "6LMK", "GIP", "GLUCAGON"],
            "execution_mode": "REFOLD_REQUIRED",
            "scientific_boundary": {
                "proves_only": (
                    "the AIV1 input and execution contract is technically replayable"
                ),
                "does_not_prove": [
                    "binding",
                    "non-binding",
                    "affinity or KD",
                    "selectivity",
                    "cross-scaffold generalization",
                    "experimental success",
                ],
            },
        }
        self.contract_path.write_text(canonical_json(contract), encoding="utf-8")
        self.rebind_aiv0()

    def _build_provenance(self) -> None:
        self.spec_gate_bundle_path.write_bytes(b"synthetic-spec-gate-bundle")
        self.model_inputs_manifest_path.write_text(
            f"{sha256_text('model-input')}  model-input.fixture\n", encoding="utf-8"
        )
        self.runtime_scripts_manifest_path.write_text(
            f"{sha256_text('runtime-script')}  runtime-script.fixture\n",
            encoding="utf-8",
        )
        self.environment_manifest_path.write_text(
            f"{sha256_text('environment-member')}  environment-member.fixture\n",
            encoding="utf-8",
        )
        self.acceptance_config_manifest_path.write_text(
            f"{sha256_text('resolved-config-member')}  config/resolved.yaml\n",
            encoding="utf-8",
        )
        self.config_sha = sha256_file(self.acceptance_config_manifest_path)
        self.code_sha = sha256_file(self.runtime_scripts_manifest_path)
        self.environment_sha = sha256_file(self.environment_manifest_path)
        platform_evidence = {
            "schema_version": "AIV1_PLATFORM_EVIDENCE_V1",
            "os_family": "Linux",
            "architecture": "x86_64",
            "accelerator_vendor": "NVIDIA",
            "gpu_name": "NVIDIA A100-SXM4-80GB",
            "driver_version": "570.86.15",
            "cuda_runtime_version": "12.6",
            "gpu_compute_capability": "8.0",
            "bfloat16_supported": True,
            "cuda_available": True,
            "nvidia_smi_exit_code": 0,
            "environment_sha256": self.environment_sha,
            "collected_at_utc": "2026-08-28T00:00:00Z",
        }
        self.platform_path.write_text(
            canonical_json(platform_evidence), encoding="utf-8"
        )

    def _write_candidate_output_set(
        self,
        *,
        output_directory: Path,
        candidate_ids: list[str],
        sequences: list[str],
    ) -> tuple[Path, list[Path]]:
        design_directory = output_directory / "intermediate_designs"
        inverse_directory = output_directory / "intermediate_designs_inverse_folded"
        fold_directory = inverse_directory / "fold_out_npz"
        refold_directory = inverse_directory / "refold_cif"
        for directory in (
            design_directory,
            inverse_directory,
            fold_directory,
            refold_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        aggregate_rows: list[dict[str, str]] = []
        refold_paths: list[Path] = []
        for index, (candidate_id, sequence) in enumerate(
            zip(candidate_ids, sequences, strict=True)
        ):
            cif_payload = minimal_candidate_mmcif(candidate_id, sequence)
            design_cif = design_directory / f"{candidate_id}.cif"
            inverse_cif = inverse_directory / f"{candidate_id}.cif"
            refold_cif = refold_directory / f"{candidate_id}.cif"
            for path in (design_cif, inverse_cif, refold_cif):
                path.write_text(cif_payload, encoding="utf-8")

            design_npz = design_directory / f"{candidate_id}.npz"
            inverse_npz = inverse_directory / f"{candidate_id}.npz"
            np.savez(design_npz, candidate_index=np.asarray([index], dtype=np.int64))
            np.savez(
                inverse_npz,
                candidate_index=np.asarray([index], dtype=np.int64),
            )

            atom_count = 4 + index
            fold_arrays = {
                "coords": np.full(
                    (5, atom_count, 3),
                    fill_value=float(index + 1) / 10.0,
                    dtype=np.float32,
                )
            }
            for score_index, field in enumerate(FOLD_SCORE_FIELDS):
                fold_arrays[field] = np.linspace(
                    0.1 + score_index / 100.0,
                    0.5 + score_index / 100.0,
                    num=5,
                    dtype=np.float32,
                )
            np.savez(fold_directory / f"{candidate_id}.npz", **fold_arrays)

            refold_paths.append(refold_cif)
            aggregate_rows.append(
                {
                    "id": candidate_id,
                    "file_name": refold_cif.name,
                    "designed_chain_sequence": sequence,
                }
            )

        aggregate_path = inverse_directory / "aggregate_metrics_analyze.csv"
        write_csv(
            aggregate_path,
            ("id", "file_name", "designed_chain_sequence"),
            aggregate_rows,
        )
        return aggregate_path, refold_paths

    def _build_candidates_and_aggregate(self) -> None:
        anchor_rows: list[dict[str, str]] = []
        candidate_ids = [f"candidate_{i:02d}" for i in range(10)]
        sequence_prefix = "ACDEFGHIKLMNPQRSTVWY" * 5 + "ACDEFGHIKL"
        sequence_suffixes = "ACDEFGHIKL"
        sequences = [sequence_prefix + sequence_suffixes[index] for index in range(10)]
        self.aggregate_path, self.candidate_paths = self._write_candidate_output_set(
            output_directory=self.acceptance_directory,
            candidate_ids=candidate_ids,
            sequences=sequences,
        )
        for index, (candidate_id, sequence, artifact) in enumerate(
            zip(candidate_ids, sequences, self.candidate_paths, strict=True)
        ):
            anchor_rows.append(
                {
                    "anchor_order": str(index),
                    "candidate_id": candidate_id,
                    "full_sequence": sequence,
                    "full_sequence_sha256": sha256_text(sequence),
                    "generation_cell_id": self.generation["generation_cell_id"],
                    "shard_id": "acceptance",
                    "scaffold_id": self.generation["scaffold_id"],
                    "checkpoint_id": self.generation["checkpoint_id"],
                    "candidate_artifact_uri": self.workspace_uri(artifact),
                    "candidate_artifact_sha256": sha256_file(artifact),
                    "config_sha256": self.config_sha,
                    "code_sha256": self.code_sha,
                    "environment_sha256": self.environment_sha,
                    "rng_seed_status": "NOT_EXPOSED_BY_CLI",
                    "rng_seed": "",
                }
            )
        write_tsv(self.anchor_path, ANCHOR_FIELDS, anchor_rows)

    def _build_acceptance_contract(self) -> None:
        self.acceptance_contract_path.write_text(
            canonical_json(
                {
                    "status": "PASS",
                    "expected_designs": 10,
                    "observed_unique_ids": 10,
                    "fold_samples_per_candidate": 5,
                    "resolved_design_diffusion_samples": 1,
                    "resolved_design_multiplicity": 10,
                }
            ),
            encoding="utf-8",
        )

    def _build_resource_probes(self) -> None:
        for name in ("diverse", "adherence"):
            probe_directory = self.acceptance_root / f"6xym_{name}_batch5__attempt_001"
            log_directory = probe_directory / "operator_logs"
            log_directory.mkdir(parents=True)
            probe_candidate_ids = [f"6xym_{name}_{index:02d}" for index in range(10)]
            probe_sequences = [
                "ACDEFGHIKLMNPQRSTVWY" * 4 + f"ACDEFGHIK{suffix}"
                for suffix in "ACDEFGHIKL"
            ]
            aggregate_path, _ = self._write_candidate_output_set(
                output_directory=probe_directory,
                candidate_ids=probe_candidate_ids,
                sequences=probe_sequences,
            )
            output_manifest_path = log_directory / "output_SHA256SUMS"
            resolved_config_path = log_directory / "resolved_config_SHA256SUMS"
            resolved_config_path.write_text(
                f"{self.config_sha}  resolved_config.json\n", encoding="utf-8"
            )
            cell_contract_path = log_directory / "cell_contract.json"
            cell_contract_path.write_text(
                canonical_json(
                    {
                        "status": "PASS",
                        "expected_designs": 10,
                        "observed_unique_ids": 10,
                        "fold_samples_per_candidate": 5,
                        "resolved_design_diffusion_samples": 5,
                        "resolved_design_multiplicity": 2,
                    }
                ),
                encoding="utf-8",
            )
            peak_path = log_directory / "peak_memory_fraction.txt"
            peak_path.write_text("0.5\n", encoding="utf-8")
            telemetry_path = log_directory / "nvidia_smi.csv"
            telemetry_path.write_text(
                "memory.used [MiB], memory.total [MiB]\n40000,80000\n",
                encoding="utf-8",
            )
            success_path = log_directory / "probe.SUCCESS.json"
            self.probe_paths[name] = {
                "root": probe_directory,
                "success": success_path,
                "output_manifest": output_manifest_path,
                "resolved_config": resolved_config_path,
                "cell_contract": cell_contract_path,
                "peak": peak_path,
                "telemetry": telemetry_path,
                "aggregate": aggregate_path,
            }
            self.rewrite_probe_output_manifest(name)
            success_path.write_text(
                canonical_json(
                    {
                        "status": "SUCCESS",
                        "pipeline_exit_code": 0,
                        "probe_id": f"6xym_{name}_batch5",
                        "checkpoint_name": name,
                        "checkpoint_sha256": (
                            self.generation["checkpoint_sha256"]
                            if name == "adherence"
                            else (
                                "360af8bd6e59527ff6ec25dd81253967"
                                "f3bd3567d200053b10680634751f8e3c"
                            )
                        ),
                        "num_designs": 10,
                        "diffusion_batch_size": 5,
                        "fold_samples": 5,
                        "peak_memory_fraction": 0.5,
                        "model_inputs_manifest_sha256": sha256_file(
                            self.model_inputs_manifest_path
                        ),
                        "runtime_scripts_manifest_sha256": sha256_file(
                            self.runtime_scripts_manifest_path
                        ),
                        "spec_gate_bundle_sha256": sha256_file(
                            self.spec_gate_bundle_path
                        ),
                        "resolved_config_manifest_sha256": sha256_file(
                            resolved_config_path
                        ),
                        "cell_contract_sha256": sha256_file(cell_contract_path),
                        "output_manifest_sha256": sha256_file(output_manifest_path),
                    }
                ),
                encoding="utf-8",
            )

    def rewrite_probe_output_manifest(self, name: str) -> None:
        paths = self.probe_paths[name]
        probe_directory = paths["root"]
        output_manifest_path = paths["output_manifest"]
        success_path = paths["success"]
        members = [
            path
            for path in probe_directory.rglob("*")
            if path.is_file() and path not in {output_manifest_path, success_path}
        ]
        output_manifest_path.write_text(
            "\n".join(
                sorted(
                    f"{sha256_file(path)}  "
                    f"./{path.relative_to(probe_directory).as_posix()}"
                    for path in members
                )
            )
            + "\n",
            encoding="utf-8",
        )

    def rebind_probe_output(self, name: str) -> None:
        self.rewrite_probe_output_manifest(name)
        success_path = self.probe_paths[name]["success"]
        payload = json.loads(success_path.read_text(encoding="utf-8"))
        payload["output_manifest_sha256"] = sha256_file(
            self.probe_paths[name]["output_manifest"]
        )
        success_path.write_text(canonical_json(payload), encoding="utf-8")
        self.rebind_g2()

    def write_acceptance_output_manifest(
        self, *, excluded: set[Path] | None = None
    ) -> None:
        excluded_resolved = {path.resolve() for path in (excluded or set())}
        members = [
            path
            for path in self.acceptance_directory.rglob("*")
            if path.is_file()
            and path
            not in {
                self.output_manifest_path,
                self.acceptance_success_path,
            }
        ]
        lines = []
        for path in members:
            if path.resolve() in excluded_resolved:
                continue
            relative = path.resolve().relative_to(self.acceptance_directory.resolve())
            lines.append(f"{sha256_file(path)}  ./{relative.as_posix()}")
        self.output_manifest_path.write_text(
            "\n".join(sorted(lines)) + "\n", encoding="utf-8"
        )

    def _write_acceptance_success(self) -> None:
        payload = {
            "status": "SUCCESS",
            "pipeline_exit_code": 0,
            "spec_gate_bundle_sha256": sha256_file(self.spec_gate_bundle_path),
            "model_inputs_manifest_sha256": sha256_file(
                self.model_inputs_manifest_path
            ),
            "runtime_scripts_manifest_sha256": sha256_file(
                self.runtime_scripts_manifest_path
            ),
            "cell_contract_sha256": sha256_file(self.acceptance_contract_path),
            "output_manifest_sha256": sha256_file(self.output_manifest_path),
        }
        self.acceptance_success_path.write_text(
            canonical_json(payload), encoding="utf-8"
        )

    def rebind_g2(self, *, rewrite_acceptance_manifest: bool = False) -> None:
        if rewrite_acceptance_manifest:
            self.write_acceptance_output_manifest()
        self._write_acceptance_success()
        gate_payload = {
            "status": "PASS",
            "spec_gate_bundle_sha256": sha256_file(self.spec_gate_bundle_path),
            "acceptance_success_sha256": sha256_file(self.acceptance_success_path),
            "probe_success_sha256": {
                name: sha256_file(self.probe_paths[name]["success"])
                for name in ("diverse", "adherence")
            },
            "output_manifest_sha256": {
                "7xl0_adherence": sha256_file(self.output_manifest_path),
                **{
                    f"6xym_{name}": sha256_file(
                        self.probe_paths[name]["output_manifest"]
                    )
                    for name in ("diverse", "adherence")
                },
            },
            "resolved_config_manifest_sha256": {
                f"6xym_{name}": sha256_file(self.probe_paths[name]["resolved_config"])
                for name in ("diverse", "adherence")
            },
            "peak_memory_fraction": {"diverse": 0.5, "adherence": 0.5},
            "resource_summary_sha256": sha256_file(self.resource_summary_path),
        }
        self.g2_gate_path.write_text(canonical_json(gate_payload), encoding="utf-8")

        anchor_rows = read_tsv(self.anchor_path)
        candidate_ids = [row["candidate_id"] for row in anchor_rows]
        receipt = {
            "schema_version": "AIV1_G2_ANCHOR_RELEASE_V1",
            "gate_id": "G2",
            "status": "PASS",
            **self.generation,
            "platform_evidence_uri": self.workspace_uri(self.platform_path),
            "platform_evidence_sha256": sha256_file(self.platform_path),
            "g2_acceptance_gate_uri": self.workspace_uri(self.g2_gate_path),
            "g2_acceptance_gate_sha256": sha256_file(self.g2_gate_path),
            "g2_resource_probe_status_uri": self.workspace_uri(
                self.g2_resource_status_path
            ),
            "g2_resource_probe_status_sha256": sha256_file(
                self.g2_resource_status_path
            ),
            "aggregate_metrics_uri": self.workspace_uri(self.aggregate_path),
            "aggregate_metrics_sha256": sha256_file(self.aggregate_path),
            "candidate_count": 10,
            "candidate_id_set_sha256": candidate_id_set_sha256(candidate_ids),
            "anchor_manifest_sha256": sha256_file(self.anchor_path),
            "aiv0_final_check_receipt_sha256": sha256_file(self.aiv0_receipt_path),
            "config_sha256": self.config_sha,
            "code_sha256": self.code_sha,
            "environment_sha256": self.environment_sha,
            "output_manifest_uri": self.workspace_uri(self.output_manifest_path),
            "output_manifest_sha256": sha256_file(self.output_manifest_path),
            "completed_at_utc": "2026-08-28T00:00:00Z",
        }
        self.g2_path.write_text(canonical_json(receipt), encoding="utf-8")

    def rebind_contract(self) -> None:
        contract = json.loads(self.contract_path.read_text(encoding="utf-8"))
        contract["development_state_contract_sha256"] = sha256_file(self.state_path)
        contract["experience_registry_schema_sha256"] = sha256_file(
            self.registry_schema_path
        )
        self.contract_path.write_text(canonical_json(contract), encoding="utf-8")

    def rebind_aiv0(self) -> None:
        self.aiv0_derived_manifest_path.write_text(
            f"{sha256_file(self.inventory_path)}  structure_inventory.tsv\n",
            encoding="utf-8",
        )
        aiv0_receipt = {
            "schema_version": "AIV0_STAGE_RECEIPT_V1",
            "status": "PASS",
            "validator_mode": "check",
            "exit_code": 0,
            "derived_outputs_manifest_sha256": sha256_file(
                self.aiv0_derived_manifest_path
            ),
        }
        self.aiv0_receipt_path.write_text(
            canonical_json(aiv0_receipt), encoding="utf-8"
        )
        aiv0_summary = {
            "schema_version": "AIV0_M0_REPOSITORY_SUMMARY_V1",
            "status": "M0_PASS_ASSET_AND_SEMANTIC_READINESS",
            "authoritative_evidence": {
                "final_check_receipt_sha256": sha256_file(self.aiv0_receipt_path),
                "final_derived_manifest_sha256": sha256_file(
                    self.aiv0_derived_manifest_path
                ),
            },
            "gates": {"experimental_negative_labels": 0},
        }
        self.aiv0_summary_path.write_text(
            canonical_json(aiv0_summary), encoding="utf-8"
        )

    def replace_state_path(
        self, *, state_index: int, relative_path: str, target_path: Path
    ) -> None:
        target_sha = sha256_file(target_path)
        state_rows = read_tsv(self.state_path)
        old_relative = state_rows[state_index]["relative_path"]
        state_rows[state_index]["relative_path"] = relative_path
        state_rows[state_index]["sha256"] = target_sha
        write_tsv(self.state_path, STATE_FIELDS, state_rows)

        inventory_rows = read_tsv(self.inventory_path)
        inventory_row = next(
            row for row in inventory_rows if row["relative_path"] == old_relative
        )
        inventory_row["relative_path"] = relative_path
        inventory_row["sha256"] = target_sha
        write_tsv(self.inventory_path, INVENTORY_FIELDS, inventory_rows)

        self.rebind_contract()
        self.rebind_aiv0()
        self.rebind_g2()

    def validated(self):
        return validate_inputs(
            repo_root=self.repo,
            workspace_root=self.workspace,
            input_contract_path=self.contract_path,
            state_contract_path=self.state_path,
            inventory_path=self.inventory_path,
            aiv0_summary_path=self.aiv0_summary_path,
            aiv0_receipt_path=self.aiv0_receipt_path,
            aiv0_derived_manifest_path=self.aiv0_derived_manifest_path,
            anchor_manifest_path=self.anchor_path,
            g2_receipt_path=self.g2_path,
        )


class AIV1MatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = SyntheticAIV1Fixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_violation(self, code: str, function) -> ContractViolation:
        with self.assertRaises(ContractViolation) as caught:
            function()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_fixture_uses_canonical_contract_paths_and_complete_g2_gate(self) -> None:
        self.assertEqual(
            self.fixture.contract_path.relative_to(self.fixture.repo),
            CANONICAL_CONTRACT_DIRECTORY / "aiv1_input_contract.json",
        )
        contract = json.loads(self.fixture.contract_path.read_text(encoding="utf-8"))
        self.assertEqual(
            contract["development_state_contract_sha256"],
            sha256_file(self.fixture.state_path),
        )
        gate = json.loads(self.fixture.g2_gate_path.read_text(encoding="utf-8"))
        self.assertEqual(gate["status"], "PASS")
        self.assertEqual(set(gate["probe_success_sha256"]), {"diverse", "adherence"})
        self.assertEqual(self.fixture.g2_resource_status_path.read_text(), "PASS\n")
        self.assertEqual(len(self.fixture.validated().anchors), 10)

    def test_manifest_bound_native_files_are_excluded_from_candidate_count(
        self,
    ) -> None:
        for directory in (
            self.fixture.acceptance_directory / "intermediate_designs",
            self.fixture.acceptance_directory / "intermediate_designs_inverse_folded",
        ):
            (directory / "reference_native.cif").write_text(
                minimal_candidate_mmcif("reference_native", "ACDEFGHIKL"),
                encoding="utf-8",
            )
            np.savez(
                directory / "reference_native.npz",
                native=np.asarray([1], dtype=np.int64),
            )
        self.fixture.rebind_g2(rewrite_acceptance_manifest=True)
        validated = self.fixture.validated()
        self.assertEqual(len(validated.anchors), 10)
        manifest = self.fixture.output_manifest_path.read_text(encoding="utf-8")
        self.assertIn("reference_native.cif", manifest)
        self.assertIn("reference_native.npz", manifest)

    def test_exact_matrix_has_160_tasks_and_800_sample_slots(self) -> None:
        validated = self.fixture.validated()
        rows = build_task_rows(validated, campaign_id="AIV1_SYNTHETIC_TEST")
        self.assertEqual(len(rows), 160)
        self.assertEqual(sum(int(row["sample_count"]) for row in rows), 800)
        self.assertEqual(len({row["candidate_id"] for row in rows}), 10)
        self.assertTrue(all(row["execution_mode"] == "REFOLD_REQUIRED" for row in rows))
        self.assertTrue(all(row["lockbox_access"] == "false" for row in rows))
        self.assertEqual(
            sum(row["data_partition"] == "positive_compact" for row in rows), 50
        )
        self.assertEqual(
            sum(row["data_partition"] == "tuning_challenge" for row in rows), 110
        )

    def test_second_campaign_task_ids_do_not_collide(self) -> None:
        validated = self.fixture.validated()
        first = build_task_rows(validated, campaign_id="AIV1_CAMPAIGN_ONE")
        second = build_task_rows(validated, campaign_id="AIV1_CAMPAIGN_TWO")
        first_ids = {row["task_id"] for row in first}
        second_ids = {row["task_id"] for row in second}
        self.assertEqual(len(first_ids), 160)
        self.assertEqual(len(second_ids), 160)
        self.assertTrue(first_ids.isdisjoint(second_ids))

    def test_distinct_candidate_instances_may_share_a_full_sequence(self) -> None:
        anchors = read_tsv(self.fixture.anchor_path)
        shared_sequence = anchors[0]["full_sequence"]
        candidate_path = self.fixture.candidate_paths[1]
        candidate_path.write_text(
            minimal_candidate_mmcif("candidate_01", shared_sequence),
            encoding="utf-8",
        )
        anchors[1]["full_sequence"] = shared_sequence
        anchors[1]["full_sequence_sha256"] = sha256_text(shared_sequence)
        anchors[1]["candidate_artifact_sha256"] = sha256_file(candidate_path)
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, anchors)
        aggregate_rows = read_csv(self.fixture.aggregate_path)
        aggregate_rows[1]["designed_chain_sequence"] = shared_sequence
        write_csv(
            self.fixture.aggregate_path,
            ("id", "file_name", "designed_chain_sequence"),
            aggregate_rows,
        )
        self.fixture.rebind_g2(rewrite_acceptance_manifest=True)
        validated = self.fixture.validated()
        self.assertEqual(len(validated.anchors), 10)
        self.assertEqual(
            len(build_task_rows(validated, campaign_id="AIV1_DUPLICATE_SEQUENCE_OK")),
            160,
        )

    def test_distinct_candidate_paths_may_have_identical_bytes(self) -> None:
        anchors = read_tsv(self.fixture.anchor_path)
        aggregate_rows = read_csv(self.fixture.aggregate_path)
        source_path = self.fixture.candidate_paths[0]
        destination_path = self.fixture.candidate_paths[1]
        destination_path.write_bytes(source_path.read_bytes())
        anchors[1]["full_sequence"] = anchors[0]["full_sequence"]
        anchors[1]["full_sequence_sha256"] = anchors[0]["full_sequence_sha256"]
        anchors[1]["candidate_artifact_sha256"] = sha256_file(destination_path)
        aggregate_rows[1]["designed_chain_sequence"] = anchors[0]["full_sequence"]
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, anchors)
        write_csv(
            self.fixture.aggregate_path,
            ("id", "file_name", "designed_chain_sequence"),
            aggregate_rows,
        )
        self.fixture.rebind_g2(rewrite_acceptance_manifest=True)
        validated = self.fixture.validated()
        self.assertEqual(len(validated.anchors), 10)

    def test_materialized_bundle_binds_matrix_and_input_snapshot(self) -> None:
        validated = self.fixture.validated()
        output_dir = self.fixture.workspace / "boltzgen/runs/aiv1_fixture/matrix_bundle"
        output_dir.parent.mkdir(parents=True)
        synthetic_builder = self.fixture.repo / "build_ai_validation_matrix.py"
        synthetic_builder.write_text("# synthetic bound builder\n", encoding="utf-8")
        summary = materialize_matrix_bundle(
            validated=validated,
            output_dir=output_dir,
            campaign_id="AIV1_SYNTHETIC_MATERIALIZED",
            input_paths=(
                self.fixture.contract_path,
                self.fixture.state_path,
                self.fixture.inventory_path,
                self.fixture.aiv0_summary_path,
                self.fixture.aiv0_receipt_path,
                self.fixture.aiv0_derived_manifest_path,
                self.fixture.anchor_path,
                self.fixture.g2_path,
            ),
            repo_root=self.fixture.repo.resolve(strict=True),
            workspace_root=self.fixture.workspace.resolve(strict=True),
            builder_path=synthetic_builder,
        )
        self.assertEqual(summary["logical_task_count"], 160)
        self.assertEqual(summary["expected_sample_result_rows"], 800)
        self.assertEqual(summary["lockbox_task_count"], 0)
        self.assertTrue((output_dir / "task_matrix.tsv").is_file())
        self.assertTrue((output_dir / "input_snapshot.json").is_file())
        bound_inputs = (output_dir / "inputs.SHA256SUMS").read_text(encoding="utf-8")
        self.assertEqual(bound_inputs.count("nvidia_smi.csv"), 2)

    def test_unselected_lockbox_present_before_aiv0_freeze_is_not_discovered(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = SyntheticAIV1Fixture(
                Path(directory), include_unselected_lockbox=True
            )
            validated = fixture.validated()
            task_rows = build_task_rows(validated, campaign_id="AIV1_ALLOWLIST_TEST")
        self.assertEqual(len(task_rows), 160)
        self.assertFalse(any("2B4N" in "|".join(row.values()) for row in task_rows))

    def test_rejects_aiv0_inventory_change_after_freeze(self) -> None:
        inventory_rows = read_tsv(self.fixture.inventory_path)
        inventory_rows[0]["status"] = "CHANGED_AFTER_AIV0_FREEZE"
        write_tsv(self.fixture.inventory_path, INVENTORY_FIELDS, inventory_rows)
        self.assert_violation("BLOCKED_AIV0_HANDOFF", self.fixture.validated)

    def test_rejects_nine_anchors(self) -> None:
        rows = read_tsv(self.fixture.anchor_path)[:-1]
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, rows)
        self.assert_violation("BLOCKED_MISSING_G2_ANCHORS", self.fixture.validated)

    def test_rejects_duplicate_anchor_identity(self) -> None:
        rows = read_tsv(self.fixture.anchor_path)
        rows[1]["candidate_id"] = rows[0]["candidate_id"]
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, rows)
        self.assert_violation("BLOCKED_DUPLICATE_ANCHOR", self.fixture.validated)

    def test_rejects_duplicate_candidate_artifact(self) -> None:
        rows = read_tsv(self.fixture.anchor_path)
        rows[1]["candidate_artifact_uri"] = rows[0]["candidate_artifact_uri"]
        rows[1]["candidate_artifact_sha256"] = rows[0]["candidate_artifact_sha256"]
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, rows)
        aggregate_rows = read_csv(self.fixture.aggregate_path)
        aggregate_rows[1]["file_name"] = aggregate_rows[0]["file_name"]
        write_csv(
            self.fixture.aggregate_path,
            ("id", "file_name", "designed_chain_sequence"),
            aggregate_rows,
        )
        self.fixture.rebind_g2(rewrite_acceptance_manifest=True)
        self.assert_violation("BLOCKED_DUPLICATE_ANCHOR", self.fixture.validated)

    def test_rejects_wrong_generation_origin(self) -> None:
        rows = read_tsv(self.fixture.anchor_path)
        rows[0]["checkpoint_id"] = "diverse"
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, rows)
        self.assert_violation("BLOCKED_WRONG_ANCHOR_ORIGIN", self.fixture.validated)

    def test_rejects_unbound_shard_id(self) -> None:
        rows = read_tsv(self.fixture.anchor_path)
        rows[0]["shard_id"] = "invented_shard"
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, rows)
        self.fixture.rebind_g2()
        self.assert_violation("BLOCKED_WRONG_ANCHOR_ORIGIN", self.fixture.validated)

    def test_rejects_exposed_rng_seed(self) -> None:
        rows = read_tsv(self.fixture.anchor_path)
        rows[0]["rng_seed_status"] = "EXPOSED_BY_CLI"
        rows[0]["rng_seed"] = "20260828"
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, rows)
        self.fixture.rebind_g2()
        self.assert_violation("BLOCKED_ANCHOR_IDENTITY", self.fixture.validated)

    def test_rejects_lockbox_marker_in_anchor_artifact_uri(self) -> None:
        rows = read_tsv(self.fixture.anchor_path)
        rows[0][
            "candidate_artifact_uri"
        ] = "workspace://boltzgen/lockbox/GIP_candidate.cif"
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, rows)
        self.assert_violation("BLOCKED_LOCKBOX_LEAK", self.fixture.validated)

    def test_rejects_aggregate_sequence_mismatch(self) -> None:
        rows = read_csv(self.fixture.aggregate_path)
        rows[0]["designed_chain_sequence"] = "A" * len(
            rows[0]["designed_chain_sequence"]
        )
        write_csv(
            self.fixture.aggregate_path,
            ("id", "file_name", "designed_chain_sequence"),
            rows,
        )
        self.fixture.rebind_g2(rewrite_acceptance_manifest=True)
        self.assert_violation(
            "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH", self.fixture.validated
        )

    def test_rejects_mmcif_sequence_mismatch(self) -> None:
        anchor_rows = read_tsv(self.fixture.anchor_path)
        candidate_path = self.fixture.candidate_paths[0]
        wrong_sequence = "A" * len(anchor_rows[0]["full_sequence"])
        candidate_path.write_text(
            minimal_candidate_mmcif("candidate_00", wrong_sequence),
            encoding="utf-8",
        )
        anchor_rows[0]["candidate_artifact_sha256"] = sha256_file(candidate_path)
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, anchor_rows)
        self.fixture.rebind_g2(rewrite_acceptance_manifest=True)
        self.assert_violation(
            "BLOCKED_CANDIDATE_SEQUENCE_MISMATCH", self.fixture.validated
        )

    def test_rejects_candidate_artifact_absent_from_g2_output_manifest(self) -> None:
        self.fixture.write_acceptance_output_manifest(
            excluded={self.fixture.candidate_paths[0]}
        )
        self.fixture.rebind_g2()
        with self.assertRaises(ContractViolation) as caught:
            self.fixture.validated()
        self.assertIn("manifest", caught.exception.message.casefold())

    def test_rejects_macos_mps_as_g2_evidence(self) -> None:
        platform_payload = json.loads(
            self.fixture.platform_path.read_text(encoding="utf-8")
        )
        platform_payload.update(
            {
                "os_family": "Darwin",
                "architecture": "arm64",
                "accelerator_vendor": "Apple",
                "gpu_name": "Apple M-series",
                "driver_version": "NOT_APPLICABLE",
                "cuda_runtime_version": "NOT_AVAILABLE",
                "gpu_compute_capability": "NOT_APPLICABLE",
                "bfloat16_supported": False,
                "cuda_available": False,
                "nvidia_smi_exit_code": 127,
            }
        )
        self.fixture.platform_path.write_text(
            canonical_json(platform_payload), encoding="utf-8"
        )
        self.fixture.rebind_g2()
        self.assert_violation("BLOCKED_UNSUPPORTED_RUNTIME", self.fixture.validated)

    def test_rejects_missing_6xym_probe_success(self) -> None:
        self.fixture.probe_paths["diverse"]["success"].unlink()
        self.assert_violation("BLOCKED_MISSING_INPUT", self.fixture.validated)

    def test_rejects_failed_7xl0_acceptance_contract_status(self) -> None:
        contract = json.loads(
            self.fixture.acceptance_contract_path.read_text(encoding="utf-8")
        )
        contract["status"] = "FAIL"
        self.fixture.acceptance_contract_path.write_text(
            canonical_json(contract), encoding="utf-8"
        )
        self.fixture.rebind_g2(rewrite_acceptance_manifest=True)
        self.assert_violation("BLOCKED_G2_EVIDENCE", self.fixture.validated)

    def test_rejects_g2_peak_memory_above_contract(self) -> None:
        gate = json.loads(self.fixture.g2_gate_path.read_text(encoding="utf-8"))
        gate["peak_memory_fraction"]["diverse"] = 0.95
        self.fixture.g2_gate_path.write_text(canonical_json(gate), encoding="utf-8")
        receipt = json.loads(self.fixture.g2_path.read_text(encoding="utf-8"))
        receipt["g2_acceptance_gate_sha256"] = sha256_file(self.fixture.g2_gate_path)
        self.fixture.g2_path.write_text(canonical_json(receipt), encoding="utf-8")
        self.assert_violation("BLOCKED_GPU_MEMORY", self.fixture.validated)

    def test_rejects_forged_peak_below_raw_nvidia_telemetry(self) -> None:
        telemetry_path = self.fixture.probe_paths["diverse"]["telemetry"]
        telemetry_path.write_text(
            "memory.used [MiB], memory.total [MiB]\n79000,80000\n",
            encoding="utf-8",
        )
        self.fixture.rebind_probe_output("diverse")
        self.assert_violation("BLOCKED_GPU_MEMORY", self.fixture.validated)

    def test_rejects_missing_probe_design_even_when_cell_contract_says_ten(
        self,
    ) -> None:
        missing = (
            self.fixture.probe_paths["diverse"]["root"]
            / "intermediate_designs/6xym_diverse_00.cif"
        )
        missing.unlink()
        self.fixture.rebind_probe_output("diverse")
        self.assert_violation("BLOCKED_G2_OUTPUT_SET", self.fixture.validated)

    def test_rejects_candidate_filename_set_not_aligned_to_aggregate(self) -> None:
        design_npz = (
            self.fixture.acceptance_directory / "intermediate_designs/candidate_00.npz"
        )
        design_npz.rename(design_npz.with_name("unexpected_candidate.npz"))
        self.fixture.rebind_g2(rewrite_acceptance_manifest=True)
        self.assert_violation("BLOCKED_G2_OUTPUT_SET", self.fixture.validated)

    def test_rejects_fold_npz_with_wrong_sample_axis(self) -> None:
        fold_path = (
            self.fixture.acceptance_directory
            / "intermediate_designs_inverse_folded/fold_out_npz/candidate_00.npz"
        )
        invalid = {
            "coords": np.zeros((4, 8, 3), dtype=np.float32),
            **{field: np.zeros((5,), dtype=np.float32) for field in FOLD_SCORE_FIELDS},
        }
        np.savez(fold_path, **invalid)
        self.fixture.rebind_g2(rewrite_acceptance_manifest=True)
        self.assert_violation("BLOCKED_G2_FOLD_NPZ", self.fixture.validated)

    def test_rejects_fold_npz_with_nonfinite_score(self) -> None:
        fold_path = (
            self.fixture.probe_paths["adherence"]["root"]
            / "intermediate_designs_inverse_folded/fold_out_npz/6xym_adherence_00.npz"
        )
        invalid = {
            "coords": np.zeros((5, 8, 3), dtype=np.float32),
            **{field: np.zeros((5,), dtype=np.float32) for field in FOLD_SCORE_FIELDS},
        }
        invalid["design_to_target_iptm"][2] = np.nan
        np.savez(fold_path, **invalid)
        self.fixture.rebind_probe_output("adherence")
        self.assert_violation("BLOCKED_G2_FOLD_NPZ", self.fixture.validated)

    def test_rejects_release_config_hash_not_bound_to_actual_manifest(self) -> None:
        fake_config_sha = sha256_text("self-declared-config-without-source")
        anchors = read_tsv(self.fixture.anchor_path)
        for anchor in anchors:
            anchor["config_sha256"] = fake_config_sha
        write_tsv(self.fixture.anchor_path, ANCHOR_FIELDS, anchors)
        self.fixture.config_sha = fake_config_sha
        self.fixture.rebind_g2()
        self.assert_violation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", self.fixture.validated
        )

    def test_rejects_actual_model_inputs_manifest_drift(self) -> None:
        self.fixture.model_inputs_manifest_path.write_text(
            f"{sha256_text('drifted-model-input')}  model-input.fixture\n",
            encoding="utf-8",
        )
        self.assert_violation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", self.fixture.validated
        )

    def test_rejects_actual_spec_gate_bundle_drift(self) -> None:
        self.fixture.spec_gate_bundle_path.write_bytes(b"drifted-spec-gate-bundle")
        self.assert_violation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", self.fixture.validated
        )

    def test_rejects_probe_runtime_manifest_mismatch(self) -> None:
        success_path = self.fixture.probe_paths["diverse"]["success"]
        payload = json.loads(success_path.read_text(encoding="utf-8"))
        payload["runtime_scripts_manifest_sha256"] = sha256_text(
            "wrong-runtime-manifest"
        )
        success_path.write_text(canonical_json(payload), encoding="utf-8")
        self.fixture.rebind_g2()
        self.assert_violation("BLOCKED_G2_EVIDENCE", self.fixture.validated)

    def test_rejects_lockbox_marker_in_development_panel(self) -> None:
        rows = read_tsv(self.fixture.state_path)
        rows[0]["target_identity"] = "GIP_2B4N_LOCKBOX"
        write_tsv(self.fixture.state_path, STATE_FIELDS, rows)
        self.fixture.rebind_contract()
        self.assert_violation("BLOCKED_LOCKBOX_LEAK", self.fixture.validated)

    def test_rejects_blank_binding_label_even_after_resigning_aiv0(self) -> None:
        rows = read_tsv(self.fixture.inventory_path)
        rows[0]["binding_label"] = ""
        write_tsv(self.fixture.inventory_path, INVENTORY_FIELDS, rows)
        self.fixture.rebind_aiv0()
        self.fixture.rebind_g2()
        self.assert_violation("BLOCKED_LABEL_SEMANTICS", self.fixture.validated)

    def test_rejects_static_generation_contract_drift(self) -> None:
        contract = json.loads(self.fixture.contract_path.read_text(encoding="utf-8"))
        contract["generation_contract"]["checkpoint_id"] = "diverse"
        self.fixture.contract_path.write_text(
            canonical_json(contract), encoding="utf-8"
        )
        self.assert_violation("BLOCKED_CONTRACT_DRIFT", self.fixture.validated)

    def test_rejects_target_byte_drift(self) -> None:
        target = (
            self.fixture.workspace
            / read_tsv(self.fixture.state_path)[0]["relative_path"]
        )
        target.write_bytes(b"drift")
        self.assert_violation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", self.fixture.validated
        )

    def test_rejects_registered_state_alias_drift(self) -> None:
        alias = (
            self.fixture.workspace
            / "data/boltzgen_data/boltzgen_mac_enhanced_old12_glp1_20260820"
        )
        alias.unlink()
        alias.symlink_to("../../boltzgen/runs/wrong_target")
        self.assert_violation(
            "BLOCKED_INPUT_OR_ENVIRONMENT_DRIFT", self.fixture.validated
        )

    def test_rejects_duplicate_state_path(self) -> None:
        rows = read_tsv(self.fixture.state_path)
        rows[1]["relative_path"] = rows[0]["relative_path"]
        write_tsv(self.fixture.state_path, STATE_FIELDS, rows)
        self.fixture.rebind_contract()
        self.assert_violation("BLOCKED_DUPLICATE_STATE", self.fixture.validated)

    def test_rejects_absolute_target_path(self) -> None:
        target = self.fixture.root / "absolute_target.cif"
        target.write_bytes(b"absolute-target")
        self.fixture.replace_state_path(
            state_index=0,
            relative_path=str(target.resolve()),
            target_path=target,
        )
        with self.assertRaises(ContractViolation):
            self.fixture.validated()

    def test_rejects_parent_traversal_target_path(self) -> None:
        target = self.fixture.workspace.parent / "escaped_target.cif"
        target.write_bytes(b"escaped-target")
        self.fixture.replace_state_path(
            state_index=0,
            relative_path="../escaped_target.cif",
            target_path=target,
        )
        with self.assertRaises(ContractViolation):
            self.fixture.validated()


class ExperienceRegistrySchemaTests(unittest.TestCase):
    def test_schema_separates_sample_denominator_and_forbids_updates(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "resources/data/AIV1技术门合同_20260828/aiv1_experience_registry_schema.sql"
        )
        connection = sqlite3.connect(":memory:")
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("sample_result", tables)
        self.assertIn("metric_sample", tables)
        connection.execute(
            "INSERT INTO campaign VALUES (?,?,?,?,?,?,?)",
            (
                "c1",
                None,
                "AIV1_TECHNICAL_GATE",
                "AIV1",
                "0" * 64,
                "1" * 64,
                "2026-08-28T00:00:00Z",
            ),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE campaign SET campaign_id='c2' WHERE campaign_id='c1'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM campaign WHERE campaign_id='c1'")

    def test_schema_accepts_two_campaigns_and_rejects_incomplete_success(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "resources/data/AIV1技术门合同_20260828/aiv1_experience_registry_schema.sql"
        )
        connection = sqlite3.connect(":memory:")
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        for campaign_id in ("c1", "c2"):
            connection.execute(
                "INSERT INTO campaign VALUES (?,?,?,?,?,?,?)",
                (
                    campaign_id,
                    None,
                    "AIV1_TECHNICAL_GATE",
                    "AIV1",
                    "0" * 64,
                    "1" * 64,
                    "2026-08-28T00:00:00Z",
                ),
            )
            task_id = f"{campaign_id}__AIV1_C00_S00"
            connection.execute(
                """INSERT INTO task (
                    task_id, campaign_id, generation_cell_id, shard_id,
                    candidate_id, full_sequence_sha256, target_state_id,
                    target_identity, independence_group, conformer_id,
                    data_partition, panel_role, compact_cluster_weight,
                    fold_run, expected, execution_mode, expected_sample_count,
                    task_contract_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    campaign_id,
                    "7xl0_adherence__attempt_001",
                    "acceptance",
                    "candidate_00",
                    "2" * 64,
                    "DEV_00",
                    "GLP1_7_36_6X18",
                    "PDB:6X18",
                    "6X18_model01",
                    "positive_compact",
                    "positive_primary",
                    None,
                    1,
                    1,
                    "REFOLD_REQUIRED",
                    5,
                    "3" * 64,
                ),
            )
        self.assertEqual(
            connection.execute("SELECT count(*) FROM task").fetchone()[0], 2
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO sample_result VALUES (?,?,?,?,?,?,?,?)",
                (
                    "sample-incomplete",
                    "c1__AIV1_C00_S00",
                    1,
                    0,
                    "SUCCESS",
                    None,
                    None,
                    "2026-08-28T00:00:00Z",
                ),
            )


if __name__ == "__main__":
    unittest.main()
