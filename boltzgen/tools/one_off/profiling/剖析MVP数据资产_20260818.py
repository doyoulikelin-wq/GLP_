#!/usr/bin/env python3
"""Profile verified BoltzGen MVP assets without modifying source files."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import pickle
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import gemmi
import yaml
from rdkit import Chem


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "raw_sources"
CURATED = ROOT / "curated_project_inputs"
RUNTIME = ROOT / "runtime_cache"
OUT = ROOT / "metadata" / "asset_profile.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sample(path: Path, max_lines: int = 18, max_chars: int = 5000) -> str:
    text = path.read_text("utf-8", errors="replace")
    return "\n".join(text.splitlines()[:max_lines])[:max_chars]


def file_info(path: Path, with_hash: bool = False) -> dict:
    item = {
        "path": str(path.relative_to(ROOT)),
        "bytes": path.stat().st_size,
        "format": path.suffix.lower().lstrip(".") or "none",
    }
    if with_hash:
        item["sha256"] = sha256(path)
    return item


def cif_profile(path: Path) -> dict:
    document = gemmi.cif.read_file(str(path))
    block = document.sole_block()
    tags = [
        "_atom_site.pdbx_PDB_model_num",
        "_atom_site.label_asym_id",
        "_atom_site.auth_asym_id",
        "_atom_site.label_seq_id",
        "_atom_site.auth_seq_id",
        "_atom_site.label_comp_id",
        "_atom_site.label_atom_id",
        "_atom_site.type_symbol",
        "_atom_site.Cartn_x",
        "_atom_site.Cartn_y",
        "_atom_site.Cartn_z",
    ]
    table = block.find(tags)
    rows = [list(row) for row in table]
    chain_residues: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    chain_atoms: Counter[tuple[str, str]] = Counter()
    models = set()
    auth_map: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        model, label_chain, auth_chain, label_seq, _auth_seq, comp, *_ = row
        models.add(model)
        auth_map[label_chain].add(auth_chain)
        chain_atoms[(model, label_chain)] += 1
        if label_seq not in {".", "?", ""}:
            chain_residues[(model, label_chain)].add((label_seq, comp))
    chain_summary = []
    for key in sorted(chain_atoms):
        model, label_chain = key
        chain_summary.append({
            "model": model,
            "label_asym_id": label_chain,
            "auth_asym_ids": sorted(auth_map[label_chain]),
            "polymer_residue_count": len(chain_residues.get(key, set())),
            "atom_site_row_count": chain_atoms[key],
        })
    structure = gemmi.read_structure(str(path))
    return {
        **file_info(path),
        "data_block": block.name,
        "model_count": len(models),
        "atom_site_row_count": len(rows),
        "label_chain_count": len({row[1] for row in rows}),
        "gemmi_model_count": len(structure),
        "chain_summary": chain_summary,
        "sample_columns": [tag.removeprefix("_atom_site.") for tag in tags],
        "sample_rows": rows[:6],
    }


def fasta_profile(path: Path) -> dict:
    lines = path.read_text("utf-8").splitlines()
    records = []
    header = None
    sequence = []
    for line in lines:
        if line.startswith(">"):
            if header is not None:
                records.append({"header": header, "sequence": "".join(sequence)})
            header, sequence = line[1:], []
        else:
            sequence.append(line.strip())
    if header is not None:
        records.append({"header": header, "sequence": "".join(sequence)})
    return {
        **file_info(path),
        "record_count": len(records),
        "records": [{"header": r["header"], "length": len(r["sequence"]), "sequence": r["sequence"]} for r in records],
        "sample": text_sample(path),
    }


def uniprot_json_profile(path: Path) -> dict:
    data = json.loads(path.read_text("utf-8"))
    features = data.get("features", [])
    type_counts = Counter(item.get("type", "unknown") for item in features)
    return {
        **file_info(path),
        "record_count": 1,
        "primary_accession": data.get("primaryAccession"),
        "sequence_length": data.get("sequence", {}).get("length"),
        "feature_count": len(features),
        "feature_type_counts": dict(type_counts),
        "top_level_keys": list(data.keys()),
        "sample": {
            "primaryAccession": data.get("primaryAccession"),
            "uniProtkbId": data.get("uniProtkbId"),
            "organism": data.get("organism"),
            "sequence": data.get("sequence"),
            "features": features[:3],
        },
    }


def delimited_profile(path: Path, delimiter: str = "\t") -> dict:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=delimiter))
    return {
        **file_info(path),
        "row_count_including_header": len(rows),
        "data_row_count": max(0, len(rows) - 1),
        "column_count": len(rows[0]) if rows else 0,
        "columns": rows[0] if rows else [],
        "sample_rows": rows[:3],
    }


def xml_profile(path: Path) -> dict:
    root = ET.parse(path).getroot()
    tag_counts = Counter(node.tag.split("}")[-1] for node in root.iter())
    return {
        **file_info(path),
        "root_tag": root.tag.split("}")[-1],
        "element_count": sum(tag_counts.values()),
        "common_tags": tag_counts.most_common(12),
        "sample": text_sample(path, max_lines=16),
    }


def pubchem_json_profile(path: Path) -> dict:
    data = json.loads(path.read_text("utf-8"))
    compounds = data.get("PC_Compounds", [])
    sample = compounds[0] if compounds else {}
    atoms = sample.get("atoms", {})
    bonds = sample.get("bonds", {})
    return {
        **file_info(path),
        "compound_count": len(compounds),
        "atom_count": len(atoms.get("aid", [])),
        "bond_count": len(bonds.get("aid1", [])),
        "coordinate_set_count": len(sample.get("coords", [])),
        "property_count": len(sample.get("props", [])),
        "top_level_keys": list(data.keys()),
        "compound_keys": list(sample.keys()),
        "sample": {
            "id": sample.get("id"),
            "atoms": {key: value[:8] if isinstance(value, list) else value for key, value in atoms.items()},
            "bonds": {key: value[:8] if isinstance(value, list) else value for key, value in bonds.items()},
            "charge": sample.get("charge"),
            "count": sample.get("count"),
        },
    }


def sdf_profile(path: Path) -> dict:
    supplier = Chem.SDMolSupplier(str(path), removeHs=False)
    molecules = [mol for mol in supplier if mol is not None]
    rows = []
    for mol in molecules:
        rows.append({
            "name": mol.GetProp("_Name") if mol.HasProp("_Name") else None,
            "atom_count": mol.GetNumAtoms(),
            "bond_count": mol.GetNumBonds(),
            "conformer_count": mol.GetNumConformers(),
            "property_names": list(mol.GetPropNames())[:20],
            "isomeric_smiles": Chem.MolToSmiles(mol, isomericSmiles=True),
        })
    return {
        **file_info(path),
        "molecule_count": len(molecules),
        "molecules": rows,
        "sample": text_sample(path, max_lines=24),
    }


def yaml_profile(path: Path) -> dict:
    data = yaml.safe_load(path.read_text("utf-8"))
    return {
        **file_info(path),
        "top_level_type": type(data).__name__,
        "top_level_keys": list(data.keys()) if isinstance(data, dict) else [],
        "sample": text_sample(path, max_lines=40),
    }


def mols_zip_profile(path: Path) -> dict:
    expected = "3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53"
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(f"mols.zip SHA-256 mismatch: {actual}")
    with zipfile.ZipFile(path) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        names = [item.filename for item in members]
        selected = []
        for component in ["ALA", "GLY", "HIS", "ARG", "HOH"]:
            candidates = [name for name in names if Path(name).stem.upper() == component]
            if candidates:
                name = candidates[0]
                obj = pickle.loads(archive.read(name))
                selected.append({
                    "member": name,
                    "bytes": archive.getinfo(name).file_size,
                    "object_type": f"{type(obj).__module__}.{type(obj).__name__}",
                    "atom_count": obj.GetNumAtoms() if isinstance(obj, Chem.Mol) else None,
                    "bond_count": obj.GetNumBonds() if isinstance(obj, Chem.Mol) else None,
                    "conformer_count": obj.GetNumConformers() if isinstance(obj, Chem.Mol) else None,
                    "smiles": Chem.MolToSmiles(obj, isomericSmiles=True) if isinstance(obj, Chem.Mol) else None,
                })
        return {
            **file_info(path),
            "sha256": actual,
            "member_count": len(members),
            "uncompressed_bytes": sum(item.file_size for item in members),
            "compressed_payload_bytes": sum(item.compress_size for item in members),
            "member_suffix_counts": dict(Counter(Path(name).suffix.lower() for name in names)),
            "sample_member_names": names[:12],
            "project_relevant_samples": selected,
        }


def main() -> None:
    result: dict = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": ROOT.name,
        "profiles": {},
        "files": [],
    }
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and not path.name.endswith(".part") and path != OUT:
            result["files"].append(file_info(path))

    paths = {
        "uniprot_fasta": RAW / "uniprot_P01275" / "P01275.fasta",
        "uniprot_json": RAW / "uniprot_P01275" / "P01275.json",
        "uniprot_xml": RAW / "uniprot_P01275" / "P01275.xml",
        "uniprot_tsv": RAW / "uniprot_P01275" / "P01275.tsv",
        "pubchem_sdf": RAW / "pubchem_CID16133831" / "CID16133831.sdf",
        "pubchem_json": RAW / "pubchem_CID16133831" / "CID16133831.json",
        "pubchem_xml": RAW / "pubchem_CID16133831" / "CID16133831.xml",
    }
    handlers = {
        "uniprot_fasta": fasta_profile,
        "uniprot_json": uniprot_json_profile,
        "uniprot_xml": xml_profile,
        "uniprot_tsv": delimited_profile,
        "pubchem_sdf": sdf_profile,
        "pubchem_json": pubchem_json_profile,
        "pubchem_xml": xml_profile,
    }
    for key, path in paths.items():
        if path.exists():
            result["profiles"][key] = handlers[key](path)

    for path in sorted((RAW / "rcsb_structures").glob("*.cif")):
        result["profiles"][f"raw_cif_{path.stem.lower()}"] = cif_profile(path)
    for path in sorted(CURATED.rglob("*.cif")):
        result["profiles"][f"curated_cif_{path.relative_to(CURATED).as_posix()}"] = cif_profile(path)
    for path in sorted((RAW / "boltzgen_examples").glob("*.yaml")):
        result["profiles"][f"example_yaml_{path.stem.lower()}"] = yaml_profile(path)
    mols = RUNTIME / "mols.zip"
    if mols.exists():
        result["profiles"]["mols_zip"] = mols_zip_profile(mols)

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(OUT),
        "file_count": len(result["files"]),
        "profile_count": len(result["profiles"]),
        "total_bytes": sum(item["bytes"] for item in result["files"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
