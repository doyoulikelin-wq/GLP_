#!/usr/bin/env python3
"""Curate the small GLP-1/VHH inputs used by the BoltzGen MVP.

Run with a Python environment containing gemmi and PyYAML, for example:
  PYTHONPATH=/tmp/codex_mvp_deps python3 metadata/curate_small_sources.py

The script is deliberately conservative: raw downloads are retained for audit,
whereas curated_project_inputs contains only the selected peptide or VHH chain.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gemmi
import yaml


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "raw_sources"
CURATED = ROOT / "curated_project_inputs"
META = ROOT / "metadata"

AA3_TO_1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "SEC": "U", "PYL": "O",
}
AA1_TO_3 = {one: three for three, one in AA3_TO_1.items()}

SOURCES = {
    "uniprot_P01275/P01275.fasta": "https://rest.uniprot.org/uniprotkb/P01275.fasta",
    "uniprot_P01275/P01275.json": "https://rest.uniprot.org/uniprotkb/P01275.json",
    "uniprot_P01275/P01275.xml": "https://rest.uniprot.org/uniprotkb/P01275.xml",
    "uniprot_P01275/P01275.tsv": (
        "https://rest.uniprot.org/uniprotkb/P01275.tsv?"
        "fields=accession,id,protein_name,gene_names,organism_name,length,sequence"
    ),
    "pubchem_CID16133831/CID16133831.sdf": (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/16133831/SDF"
    ),
    "pubchem_CID16133831/CID16133831.json": (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/16133831/JSON"
    ),
    "pubchem_CID16133831/CID16133831.xml": (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/16133831/XML"
    ),
    "rcsb_structures/1D0R.cif": "https://files.rcsb.org/download/1D0R.cif",
    "rcsb_structures/6X18.cif": "https://files.rcsb.org/download/6X18.cif",
    "rcsb_structures/9IVG.cif": "https://files.rcsb.org/download/9IVG.cif",
    "rcsb_structures/7EOW.cif": "https://files.rcsb.org/download/7EOW.cif",
    "rcsb_structures/7XL0.cif": "https://files.rcsb.org/download/7XL0.cif",
    "boltzgen_examples/7eow.yaml": (
        "https://raw.githubusercontent.com/HannesStark/boltzgen/"
        "v0.3.2/example/nanobody_scaffolds/7eow.yaml"
    ),
    "boltzgen_examples/7xl0.yaml": (
        "https://raw.githubusercontent.com/HannesStark/boltzgen/"
        "v0.3.2/example/nanobody_scaffolds/7xl0.yaml"
    ),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_fasta(path: Path, records: list[tuple[str, str]]) -> None:
    lines: list[str] = []
    for header, sequence in records:
        lines.append(f">{header}")
        lines.extend(sequence[i : i + 80] for i in range(0, len(sequence), 80))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def block_value(block: gemmi.cif.Block, tag: str) -> str | None:
    value = block.find_value(tag)
    if value in (None, "", ".", "?"):
        return None
    return gemmi.cif.as_string(value)


def structure_resolution(block: gemmi.cif.Block) -> str | None:
    return (
        block_value(block, "_refine.ls_d_res_high")
        or block_value(block, "_em_3d_reconstruction.resolution")
    )


def sequence_from_residues(residues: list[gemmi.Residue]) -> str:
    return "".join(AA3_TO_1.get(res.name, "X") for res in residues)


def full_sequence_one_letter(entity: gemmi.Entity) -> str:
    return "".join(AA3_TO_1.get(mon, "X") for mon in entity.full_sequence)


def get_entity_by_subchain(structure: gemmi.Structure, subchain: str) -> gemmi.Entity:
    matches = [entity for entity in structure.entities if subchain in entity.subchains]
    if len(matches) != 1:
        raise ValueError(f"Expected one entity for label_asym_id {subchain}, got {len(matches)}")
    return matches[0]


def extract_target_structure(spec: dict[str, Any]) -> dict[str, Any]:
    source = RAW / "rcsb_structures" / f"{spec['pdb_id']}.cif"
    block = gemmi.cif.read(str(source)).sole_block()
    structure = gemmi.make_structure_from_block(block)
    raw_entity = get_entity_by_subchain(structure, spec["label_asym_id"])
    raw_declared_sequence = full_sequence_one_letter(raw_entity)

    if len(structure) != spec["expected_models"]:
        raise ValueError(
            f"{spec['pdb_id']}: expected {spec['expected_models']} models; got {len(structure)}"
        )

    output = gemmi.Structure()
    output.name = spec["output_entry_id"]
    output.cell = structure.cell
    output.spacegroup_hm = structure.spacegroup_hm
    mappings: list[dict[str, Any]] = []
    first_model_sequence: str | None = None
    residue_counts: list[int] = []
    atom_counts: list[int] = []

    for source_model in structure:
        selected: list[gemmi.Residue] = []
        for chain in source_model:
            if chain.name != spec["auth_asym_id"]:
                continue
            for residue in chain:
                if (
                    residue.entity_type == gemmi.EntityType.Polymer
                    and residue.subchain == spec["label_asym_id"]
                ):
                    selected.append(residue)

        if len(selected) != spec["expected_observed_residues"]:
            raise ValueError(
                f"{spec['pdb_id']} model {source_model.num}: expected "
                f"{spec['expected_observed_residues']} selected residues; got {len(selected)}"
            )

        observed_sequence = sequence_from_residues(selected)
        if first_model_sequence is None:
            first_model_sequence = observed_sequence
        elif observed_sequence != first_model_sequence:
            raise ValueError(f"{spec['pdb_id']}: target sequence differs between models")

        new_model = gemmi.Model(source_model.num)
        new_chain = gemmi.Chain(spec["auth_asym_id"])
        for order_index, residue in enumerate(selected, start=1):
            original_label_seq_id = int(residue.label_seq) if residue.label_seq else None
            cloned = residue.clone()
            cloned.entity_id = "1"
            cloned.subchain = spec["label_asym_id"]
            cloned.label_seq = order_index
            new_chain.add_residue(cloned)
            mappings.append(
                {
                    "model_number": int(source_model.num),
                    "curated_order_index": order_index,
                    "curated_label_seq_id": order_index,
                    "residue_name_3": residue.name,
                    "residue_name_1": AA3_TO_1.get(residue.name, "X"),
                    "original_pdb_id": spec["pdb_id"],
                    "original_label_asym_id": spec["label_asym_id"],
                    "original_auth_asym_id": spec["auth_asym_id"],
                    "original_label_seq_id": original_label_seq_id,
                    "original_auth_seq_id": int(residue.seqid.num),
                    "original_insertion_code": str(residue.seqid.icode).strip() or None,
                    "kept_atom_count": None,
                }
            )
        new_model.add_chain(new_chain)
        output.add_model(new_model)
        residue_counts.append(len(selected))

    # BoltzGen uses heavy-atom reference templates and selects a single alternate
    # conformation. Make that cleanup explicit in the derived inputs so that
    # _atom_site row counts match the coordinates supplied at runtime.
    output.remove_hydrogens()
    output.remove_alternative_conformations()
    mapping_index = 0
    for model in output:
        model_atoms = 0
        for chain in model:
            for residue in chain:
                model_atoms += len(residue)
                mappings[mapping_index]["kept_atom_count"] = len(residue)
                mapping_index += 1
        atom_counts.append(model_atoms)
    if mapping_index != len(mappings):
        raise ValueError(f"{spec['pdb_id']}: mapping/coordinate residue count mismatch")

    assert first_model_sequence is not None
    if spec["entity_sequence_policy"] == "declared":
        curated_entity_sequence = raw_declared_sequence
    elif spec["entity_sequence_policy"] == "observed_core":
        curated_entity_sequence = first_model_sequence
    else:
        raise ValueError(f"Unknown entity sequence policy: {spec['entity_sequence_policy']}")

    if not curated_entity_sequence:
        raise ValueError(f"{spec['pdb_id']}: empty curated entity sequence")

    entity = gemmi.Entity("1")
    entity.entity_type = gemmi.EntityType.Polymer
    entity.polymer_type = gemmi.PolymerType.PeptideL
    entity.full_sequence = [
        AA1_TO_3.get(ch, "UNK")
        for ch in curated_entity_sequence
    ]
    entity.subchains = [spec["label_asym_id"]]
    output.entities.append(entity)

    out_path = ROOT / spec["output_relative_path"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output.make_mmcif_document().write_file(str(out_path))

    reread = gemmi.read_structure(str(out_path))
    if len(reread) != spec["expected_models"]:
        raise ValueError(f"{spec['pdb_id']}: exported model count failed round-trip")
    for model in reread:
        polymers = [
            residue
            for chain in model
            for residue in chain
            if residue.entity_type == gemmi.EntityType.Polymer
        ]
        other = [
            residue
            for chain in model
            for residue in chain
            if residue.entity_type != gemmi.EntityType.Polymer
        ]
        if len(polymers) != spec["expected_observed_residues"] or other:
            raise ValueError(f"{spec['pdb_id']}: curated file contains unexpected residues")

    mapping_path = out_path.with_name(out_path.stem + "_residue_mapping.tsv")
    with mapping_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(mappings[0].keys()), delimiter="\t")
        writer.writeheader()
        writer.writerows(mappings)

    method = block_value(block, "_exptl.method")
    resolution = structure_resolution(block)
    excluded = spec["excluded_content"]
    record = {
        "artifact_id": spec["artifact_id"],
        "status": spec["status"],
        "project_role": spec["project_role"],
        "source_pdb_id": spec["pdb_id"],
        "source_path": str(source.relative_to(ROOT)),
        "curated_path": str(out_path.relative_to(ROOT)),
        "mapping_path": str(mapping_path.relative_to(ROOT)),
        "source_sha256": sha256(source),
        "curated_sha256": sha256(out_path),
        "source_chain_mapping": {
            "label_asym_id": spec["label_asym_id"],
            "auth_asym_id": spec["auth_asym_id"],
        },
        "curated_chain_mapping": {
            "label_asym_id": spec["label_asym_id"],
            "auth_asym_id": spec["auth_asym_id"],
            "label_seq_id_policy": "renumbered 1..N in observed coordinate order",
            "auth_seq_id_policy": "preserved from source",
        },
        "experimental_method": method,
        "resolution_angstrom": float(resolution) if resolution else None,
        "model_count": len(output),
        "observed_residue_count_per_model": residue_counts,
        "atom_count_per_model": atom_counts,
        "atom_count_semantics": (
            "number of retained heavy-atom _atom_site rows after removal of explicit "
            "hydrogens and collapse to one alternate conformation"
        ),
        "raw_declared_sequence": raw_declared_sequence,
        "raw_declared_sequence_length": len(raw_declared_sequence),
        "curated_entity_sequence": curated_entity_sequence,
        "curated_entity_sequence_length": len(curated_entity_sequence),
        "observed_coordinate_sequence": first_model_sequence,
        "observed_coordinate_sequence_length": len(first_model_sequence),
        "unresolved_declared_positions": spec.get("unresolved_declared_positions", []),
        "terminal_chemistry": spec["terminal_chemistry"],
        "expression_tag_assessment": spec.get("expression_tag_assessment"),
        "excluded_content": excluded,
        "curation_assertions": [
            "exactly one selected polymer chain per model",
            "no water residues in curated coordinate file",
            "no non-polymer residues in curated coordinate file",
            "no explicit hydrogen atoms in curated coordinate file",
            "one alternate conformation retained per atom name",
            "all selected models retained",
        ],
        "notes": spec["notes"],
    }
    if spec.get("paired_yaml"):
        record["paired_yaml"] = spec["paired_yaml"]
    record_path = out_path.with_name(out_path.stem + "_curation.json")
    write_json(record_path, record)
    record["curation_record_path"] = str(record_path.relative_to(ROOT))
    return record


def count_sdf_records(path: Path) -> int:
    return path.read_text(encoding="utf-8", errors="replace").count("$$$$")


def pubchem_property(compound: dict[str, Any], label: str, name: str | None = None) -> Any:
    for prop in compound.get("props", []):
        urn = prop.get("urn", {})
        if urn.get("label") != label:
            continue
        if name is not None and urn.get("name") != name:
            continue
        value = prop.get("value", {})
        for key in ("sval", "fval", "ival", "binary"):
            if key in value:
                return value[key]
    return None


def build_sequence_and_chemistry_records() -> dict[str, Any]:
    out_dir = CURATED / "sequence_chemistry"
    out_dir.mkdir(parents=True, exist_ok=True)
    uniprot = json.loads((RAW / "uniprot_P01275" / "P01275.json").read_text(encoding="utf-8"))
    precursor = uniprot["sequence"]["value"]
    seq_7_36 = precursor[97:127]
    seq_7_37 = precursor[97:128]
    if seq_7_36 != "HAEGTFTSDVSSYLEGQAAKEFIAWLVKGR":
        raise ValueError("UniProt P01275 positions 98..127 do not match expected GLP-1(7-36)")
    if seq_7_37 != seq_7_36 + "G":
        raise ValueError("UniProt P01275 position 128 is not the glycine extension")

    relevant_features = []
    for feature in uniprot.get("features", []):
        location = feature.get("location", {})
        start = location.get("start", {}).get("value")
        end = location.get("end", {}).get("value")
        if start is not None and end is not None and not (end < 91 or start > 130):
            relevant_features.append(feature)

    variants = [
        {
            "id": "GLP1_7-36_NH2",
            "sequence": seq_7_36,
            "length_residues": len(seq_7_36),
            "precursor_coordinates_1_based": "P01275:98..127",
            "n_terminus": "free amino terminus at His7",
            "c_terminus": "Arg36 amide (NH2)",
            "evidence": [
                "UniProt P01275 peptide feature 98..127",
                "UniProt P01275 modified-residue feature: Arg127 amide",
                "PubChem CID 16133831 chemical record",
            ],
            "project_role": "positive target",
        },
        {
            "id": "GLP1_9-36_terminal_state_not_asserted_by_9IVG",
            "sequence": seq_7_36[2:],
            "length_residues": len(seq_7_36) - 2,
            "derivation": "sequence-only deletion of His7-Ala8 from GLP1_7-36",
            "n_terminus": "free amino terminus at Glu9",
            "c_terminus": "project chemistry must be specified separately; 9IVG does not verify NH2",
            "project_role": "selectivity anti-target/challenge state",
        },
        {
            "id": "GLP1_7-37",
            "sequence": seq_7_37,
            "length_residues": len(seq_7_37),
            "precursor_coordinates_1_based": "P01275:98..128",
            "c_terminus": "Gly37 carboxyl terminus unless a separate modification is supplied",
            "project_role": "challenge state",
        },
        {
            "id": "GLP1_9-37",
            "sequence": seq_7_37[2:],
            "length_residues": len(seq_7_37) - 2,
            "derivation": "sequence-only deletion of His7-Ala8 from GLP1_7-37",
            "c_terminus": "Gly37 carboxyl terminus unless a separate modification is supplied",
            "project_role": "challenge state",
        },
    ]
    write_fasta(
        out_dir / "GLP1_project_variants.fasta",
        [(f"{v['id']} | {v['project_role']}", v["sequence"]) for v in variants],
    )
    write_json(
        out_dir / "GLP1_project_variants.json",
        {
            "source_accession": "UniProtKB P01275",
            "source_organism": "Homo sapiens",
            "source_precursor_length": len(precursor),
            "coordinate_system": "UniProt positions are 1-based and inclusive",
            "variants": variants,
            "relevant_uniprot_features": relevant_features,
            "caution": (
                "FASTA stores residue order only. It cannot encode terminal amidation; "
                "terminal chemistry must be carried in JSON/YAML or another chemical representation."
            ),
        },
    )

    pubchem = json.loads(
        (RAW / "pubchem_CID16133831" / "CID16133831.json").read_text(encoding="utf-8")
    )
    compounds = pubchem.get("PC_Compounds", [])
    if len(compounds) != 1:
        raise ValueError(f"Expected one PubChem compound; got {len(compounds)}")
    compound = compounds[0]
    cid = compound["id"]["id"]["cid"]
    if cid != 16133831:
        raise ValueError(f"Expected CID 16133831; got {cid}")
    filtered_pubchem = {
        "cid": cid,
        "molecular_formula": pubchem_property(compound, "Molecular Formula"),
        "molecular_weight": pubchem_property(compound, "Molecular Weight"),
        "exact_mass": pubchem_property(compound, "Mass", "Exact"),
        "standard_inchi": pubchem_property(compound, "InChI", "Standard"),
        "standard_inchikey": pubchem_property(compound, "InChIKey", "Standard"),
        "isomeric_smiles": pubchem_property(compound, "SMILES", "Absolute"),
        "atom_count": len(compound.get("atoms", {}).get("aid", [])),
        "bond_count": len(compound.get("bonds", {}).get("aid1", [])),
        "coordinate_conformer_count": len(compound.get("coords", [])),
        "formal_charge": compound.get("charge"),
        "source": "PubChem PUG REST",
        "source_url": SOURCES["pubchem_CID16133831/CID16133831.json"],
        "project_interpretation": (
            "Chemical identity record for GLP-1(7-36) amide. Use it to verify terminal "
            "chemistry and composition; do not feed the full 460-atom graph to the "
            "nanobody-anything protein target input unless the protocol explicitly requires it."
        ),
    }
    write_json(out_dir / "PubChem_CID16133831_project_record.json", filtered_pubchem)
    return {
        "variants": variants,
        "uniprot_relevant_feature_count": len(relevant_features),
        "filtered_pubchem": filtered_pubchem,
    }


def raw_file_statistics() -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    for relative, url in SOURCES.items():
        path = RAW / relative
        if not path.exists():
            raise FileNotFoundError(path)
        row: dict[str, Any] = {
            "path": str(path.relative_to(ROOT)),
            "source_url": url,
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
            "retrieved_file_mtime_utc": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "curation_scope": "raw_provenance_only; not a direct runtime input",
        }
        suffix = path.suffix.lower()
        if relative.startswith("uniprot_P01275/"):
            row.update({"dataset": "UniProtKB P01275", "record_count": 1})
            if suffix == ".fasta":
                seq = "".join(
                    line.strip() for line in path.read_text().splitlines() if not line.startswith(">")
                )
                row.update({"format": "FASTA", "sequence_length": len(seq)})
            elif suffix == ".json":
                data = json.loads(path.read_text())
                row.update(
                    {
                        "format": "UniProt JSON",
                        "feature_count": len(data.get("features", [])),
                        "sequence_length": data["sequence"]["length"],
                    }
                )
            elif suffix == ".xml":
                row["format"] = "UniProt XML"
            elif suffix == ".tsv":
                row["format"] = "TSV"
                row["data_row_count"] = max(0, len(path.read_text().splitlines()) - 1)
        elif relative.startswith("pubchem_CID16133831/"):
            row.update({"dataset": "PubChem CID 16133831", "record_count": 1})
            if suffix == ".sdf":
                row.update({"format": "SDF V2000", "sdf_record_count": count_sdf_records(path)})
            elif suffix == ".json":
                data = json.loads(path.read_text())
                compound = data["PC_Compounds"][0]
                row.update(
                    {
                        "format": "PubChem JSON",
                        "atom_count": len(compound.get("atoms", {}).get("aid", [])),
                        "bond_count": len(compound.get("bonds", {}).get("aid1", [])),
                    }
                )
            elif suffix == ".xml":
                row["format"] = "PubChem XML"
        elif relative.startswith("rcsb_structures/"):
            block = gemmi.cif.read(str(path)).sole_block()
            structure = gemmi.make_structure_from_block(block)
            polymer_entities = [
                entity for entity in structure.entities if entity.entity_type == gemmi.EntityType.Polymer
            ]
            residue_types = Counter(
                residue.entity_type.name
                for model in structure
                for chain in model
                for residue in chain
            )
            row.update(
                {
                    "dataset": f"RCSB PDB {path.stem}",
                    "record_count": 1,
                    "format": "PDBx/mmCIF",
                    "model_count": len(structure),
                    "polymer_entity_count": len(polymer_entities),
                    "polymer_chain_instances_first_model": len(
                        {
                            (chain.name, residue.subchain)
                            for chain in structure[0]
                            for residue in chain
                            if residue.entity_type == gemmi.EntityType.Polymer
                        }
                    ),
                    "residue_counts_all_models_by_type": dict(residue_types),
                    "experimental_method": block_value(block, "_exptl.method"),
                    "resolution_angstrom": structure_resolution(block),
                }
            )
        elif relative.startswith("boltzgen_examples/"):
            config = yaml.safe_load(path.read_text())
            row.update(
                {
                    "dataset": "BoltzGen v0.3.2 nanobody scaffold example",
                    "record_count": 1,
                    "format": "YAML",
                    "top_level_keys": list(config.keys()),
                    "status": "provisional_example",
                }
            )
        stats.append(row)
    return stats


def main() -> None:
    required = [RAW / relative for relative in SOURCES]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing raw sources:\n" + "\n".join(missing))

    sequence_records = build_sequence_and_chemistry_records()

    specs = [
        {
            "artifact_id": "GLP1_7-36NH2_1D0R_all_models",
            "status": "project_input_geometry_with_chemistry_caveat",
            "project_role": "positive-target solution NMR conformational ensemble",
            "pdb_id": "1D0R",
            "label_asym_id": "A",
            "auth_asym_id": "A",
            "expected_models": 20,
            "expected_observed_residues": 30,
            "output_entry_id": "1D0R_GLP1_7_36_ALL20",
            "output_relative_path": (
                "curated_project_inputs/glp1_1D0R_all_models/"
                "1D0R_glp1_7-36NH2_all20models.cif"
            ),
            "entity_sequence_policy": "declared",
            "terminal_chemistry": {
                "source_annotation": "entity description calls the peptide 7-36-amide",
                "coordinate_observation": (
                    "C-terminal Arg includes OXT in the deposited coordinates; amidation is "
                    "therefore not faithfully encoded as an atom-level terminal modification"
                ),
                "usage": "use UniProt/PubChem for chemical identity; use 1D0R for geometry",
            },
            "excluded_content": "none; the source already contains only the GLP-1 chain",
            "notes": "All 20 NMR models are retained; no best-model selection was imposed.",
        },
        {
            "artifact_id": "GLP1_7-36NH2_6X18_peptide_only",
            "status": "project_input_geometry_with_chemistry_caveat",
            "project_role": "receptor-bound positive-target geometry",
            "pdb_id": "6X18",
            "label_asym_id": "E",
            "auth_asym_id": "P",
            "expected_models": 1,
            "expected_observed_residues": 30,
            "output_entry_id": "6X18_GLP1_7_36_CHAIN_P",
            "output_relative_path": (
                "curated_project_inputs/glp1_complex_peptides/"
                "6X18_glp1_7-36NH2_labelE_authP.cif"
            ),
            "entity_sequence_policy": "declared",
            "terminal_chemistry": {
                "source_annotation": "entity description explicitly states GLP-1(7-36)NH2",
                "coordinate_observation": (
                    "the standard polymer atom list does not provide an unambiguous explicit "
                    "terminal-amide atom-level encoding"
                ),
                "usage": "use UniProt/PubChem for chemical identity; use 6X18 for geometry",
            },
            "excluded_content": (
                "G-protein alpha/beta/gamma, Nanobody35, GLP-1 receptor, and all waters"
            ),
            "notes": "The selected peptide is label_asym_id E / auth_asym_id P.",
        },
        {
            "artifact_id": "GLP1_9-36_9IVG_peptide_observed_only",
            "status": "project_input_geometry_only_terminal_state_unverified",
            "project_role": "selectivity anti-target/challenge geometry",
            "pdb_id": "9IVG",
            "label_asym_id": "A",
            "auth_asym_id": "P",
            "expected_models": 1,
            "expected_observed_residues": 21,
            "output_entry_id": "9IVG_GLP1_9_36_CHAIN_P",
            "output_relative_path": (
                "curated_project_inputs/glp1_complex_peptides/"
                "9IVG_glp1_9-36_labelA_authP_observed.cif"
            ),
            "entity_sequence_policy": "declared",
            "unresolved_declared_positions": {
                "GLP1_residue_numbers": [30, 31, 32, 33, 34, 35, 36],
                "entity_label_seq_ids": [22, 23, 24, 25, 26, 27, 28],
            },
            "terminal_chemistry": {
                "source_annotation": "entity is named GLP-1(9-36), without NH2 in the record used here",
                "coordinate_observation": (
                    "only residues 9..29 have coordinates; the C terminus 30..36 is unresolved"
                ),
                "usage": (
                    "do not label this coordinate file as NH2-verified; provide project terminal "
                    "chemistry separately before chemistry-aware modeling"
                ),
            },
            "excluded_content": (
                "GLP-1 receptor, engineered G-protein alpha, G-protein beta/gamma, and Nanobody35"
            ),
            "notes": (
                "The selected peptide is label_asym_id A / auth_asym_id P. The declared "
                "28-residue sequence is retained in mmCIF metadata, but only 21 residues are modeled."
            ),
        },
        {
            "artifact_id": "VHH_7EOW_caplacizumab_observed_core",
            "status": "provisional_example",
            "project_role": "temporary BoltzGen nanobody scaffold example; not project-approved",
            "pdb_id": "7EOW",
            "label_asym_id": "B",
            "auth_asym_id": "B",
            "expected_models": 1,
            "expected_observed_residues": 128,
            "output_entry_id": "7EOW_VHH_CORE",
            "output_relative_path": (
                "curated_project_inputs/vhh_provisional_scaffolds/7eow/7eow.cif"
            ),
            "entity_sequence_policy": "observed_core",
            "unresolved_declared_positions": {
                "entity_label_seq_ids": [1, 130, 131, 132, 133, 134, 135, 136, 137],
                "reason": "unobserved initiator Met1 plus unobserved C-terminal expression tag 130..137",
            },
            "terminal_chemistry": {
                "source_annotation": "not a GLP-1 target; VHH scaffold coordinates only",
                "coordinate_observation": "kept observed VHH residues E2..S129",
                "usage": "scaffold demonstration only",
            },
            "expression_tag_assessment": {
                "identified_declared_sequence": "LEHHHHHH",
                "declared_entity_positions": "130..137",
                "coordinate_status": "not observed; therefore absent from curated coordinates",
                "other_unobserved_expression_residue": "initiator Met1 is also unobserved",
                "curated_sequence_policy": "observed E2..S129 only",
            },
            "excluded_content": "von Willebrand factor chain and all waters",
            "paired_yaml": {
                "path": "curated_project_inputs/vhh_provisional_scaffolds/7eow/7eow.yaml",
                "source_yaml": "raw_sources/boltzgen_examples/7eow.yaml",
                "adaptation": (
                    "All design, visibility, exclude, and insertion res_index values are shifted "
                    "by -1 because unresolved source Met1 is removed and curated E2 becomes "
                    "label_seq_id 1."
                ),
                "status": "adapted_for_curated_cif; provisional smoke-test only",
            },
            "notes": (
                "The raw official YAML remains unchanged in raw_sources. The paired curated "
                "YAML is an auditable derivative aligned to the renumbered curated CIF."
            ),
        },
        {
            "artifact_id": "VHH_7XL0_vobarilizumab_observed_core",
            "status": "provisional_example",
            "project_role": "temporary BoltzGen nanobody scaffold example; not project-approved",
            "pdb_id": "7XL0",
            "label_asym_id": "A",
            "auth_asym_id": "A",
            "expected_models": 1,
            "expected_observed_residues": 121,
            "output_entry_id": "7XL0_VHH_CORE",
            "output_relative_path": (
                "curated_project_inputs/vhh_provisional_scaffolds/7xl0/7xl0.cif"
            ),
            "entity_sequence_policy": "observed_core",
            "unresolved_declared_positions": {
                "entity_label_seq_ids": [122, 123, 124, 125, 126, 127, 128, 129, 130],
                "reason": "unobserved C-terminal expression tag 122..130",
            },
            "terminal_chemistry": {
                "source_annotation": "not a GLP-1 target; VHH scaffold coordinates only",
                "coordinate_observation": "kept observed VHH residues E1..S121",
                "usage": "scaffold demonstration only",
            },
            "expression_tag_assessment": {
                "identified_declared_sequence": "AAAHHHHHH",
                "declared_entity_positions": "122..130",
                "coordinate_status": "not observed; therefore absent from curated coordinates",
                "curated_sequence_policy": "observed E1..S121 only",
            },
            "excluded_content": (
                "second VHH crystal copy, sulfate ions, glycerol, and all waters"
            ),
            "notes": (
                "The official BoltzGen v0.3.2 YAML is retained unchanged beside this file. "
                "Chain A is used exactly as in the official example."
            ),
        },
    ]

    curated_records = [extract_target_structure(spec) for spec in specs]

    # Keep raw official examples byte-for-byte in raw_sources. The 7EOW paired
    # YAML must be shifted by one position because the curated CIF removes the
    # unresolved source Met1 and renumbers E2 as label_seq_id 1.
    for name in ("7eow", "7xl0"):
        source_yaml = RAW / "boltzgen_examples" / f"{name}.yaml"
        dest_yaml = CURATED / "vhh_provisional_scaffolds" / name / f"{name}.yaml"
        dest_yaml.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_yaml, dest_yaml)
        if name == "7eow":
            text = dest_yaml.read_text(encoding="utf-8")
            replacements = {
                "26..34,52..59,98..118": "25..33,51..58,97..117",
                "26..28 # take out 3": "25..27 # take out 3; shifted -1 after removal of unresolved Met1",
                "52..54 # take out 3": "51..53 # take out 3; shifted -1 after removal of unresolved Met1",
                "98..104 # take out seven": "97..103 # take out seven; shifted -1 after removal of unresolved Met1",
                "res_index: 26 # The res_index'th residue will be a designed one (starting to count from 1)": "res_index: 25 # shifted -1 after removal of unresolved Met1",
                "res_index: 52 # The res_index'th residue will be a designed one (starting to count from 1)": "res_index: 51 # shifted -1 after removal of unresolved Met1",
                "res_index: 98 # The res_index'th residue will be a designed one (starting to count from 1)": "res_index: 97 # shifted -1 after removal of unresolved Met1",
            }
            for old, new in replacements.items():
                if old not in text:
                    raise ValueError(f"7EOW YAML expected token not found: {old}")
                text = text.replace(old, new)
            dest_yaml.write_text(text, encoding="utf-8")
        elif sha256(source_yaml) != sha256(dest_yaml):
            raise ValueError(f"YAML copy checksum mismatch: {name}")

    raw_stats = raw_file_statistics()
    write_json(META / "raw_file_statistics.json", raw_stats)

    raw_checksum_rows = [
        {
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "path": row["path"],
            "source_url": row["source_url"],
        }
        for row in raw_stats
    ]
    write_json(META / "raw_sha256.json", raw_checksum_rows)
    checksum_text = "\n".join(f"{row['sha256']}  {row['path']}" for row in raw_checksum_rows) + "\n"
    (META / "raw_SHA256SUMS.txt").write_text(checksum_text, encoding="utf-8")

    curated_files = sorted(
        path for path in CURATED.rglob("*") if path.is_file()
    )
    curated_file_stats = [
        {
            "path": str(path.relative_to(ROOT)),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in curated_files
    ]

    manifest = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_release_context": "BoltzGen v0.3.2 MVP",
        "scope_policy": {
            "raw_sources": (
                "immutable provenance copies; may contain unrelated chains and are never passed "
                "directly to the project runtime"
            ),
            "curated_project_inputs": (
                "only selected GLP-1 peptide or VHH chain; no receptor, antigen, G protein, "
                "water, ligand, ion, or expression-tag coordinates"
            ),
            "vhh_status": (
                "7EOW and 7XL0 remain provisional_example and must not be treated as "
                "project-approved scaffolds"
            ),
        },
        "raw_total_bytes": sum(row["size_bytes"] for row in raw_stats),
        "raw_file_count": len(raw_stats),
        "curated_total_bytes": sum(row["size_bytes"] for row in curated_file_stats),
        "curated_file_count": len(curated_file_stats),
        "raw_files": raw_stats,
        "sequence_and_chemistry": sequence_records,
        "curated_structure_records": curated_records,
        "curated_files": curated_file_stats,
        "blocking_or_review_flags": [
            {
                "severity": "BLOCK_FOR_CHEMISTRY_AWARE_USE",
                "artifact_id": "GLP1_9-36_9IVG_peptide_observed_only",
                "reason": (
                    "9IVG does not verify NH2 in this record and lacks coordinates for "
                    "declared residues 30..36"
                ),
            },
            {
                "severity": "NOT_PROJECT_APPROVED",
                "artifact_id": "VHH_7EOW_caplacizumab_observed_core",
                "reason": "official BoltzGen scaffold example only",
            },
            {
                "severity": "NOT_PROJECT_APPROVED",
                "artifact_id": "VHH_7XL0_vobarilizumab_observed_core",
                "reason": "official BoltzGen scaffold example only",
            },
        ],
    }
    write_json(ROOT / "curation_manifest.json", manifest)

    machine_summary = {
        "raw_file_count": manifest["raw_file_count"],
        "raw_total_bytes": manifest["raw_total_bytes"],
        "curated_file_count": manifest["curated_file_count"],
        "curated_total_bytes": manifest["curated_total_bytes"],
        "raw_dataset_record_counts": {
            "UniProt P01275": 1,
            "PubChem CID 16133831": 1,
            "RCSB PDB structures": 5,
            "BoltzGen YAML examples": 2,
        },
        "curated_structure_count": len(curated_records),
        "curated_coordinate_model_count": sum(r["model_count"] for r in curated_records),
        "curated_status_counts": dict(Counter(r["status"] for r in curated_records)),
        "curated_structures": [
            {
                "artifact_id": r["artifact_id"],
                "status": r["status"],
                "path": r["curated_path"],
                "models": r["model_count"],
                "observed_residues_per_model": r["observed_residue_count_per_model"],
                "atoms_per_model": r["atom_count_per_model"],
                "declared_sequence_length": r["raw_declared_sequence_length"],
                "curated_entity_sequence_length": r["curated_entity_sequence_length"],
                "observed_coordinate_sequence_length": r["observed_coordinate_sequence_length"],
            }
            for r in curated_records
        ],
    }
    write_json(META / "machine_readable_summary.json", machine_summary)


if __name__ == "__main__":
    main()
