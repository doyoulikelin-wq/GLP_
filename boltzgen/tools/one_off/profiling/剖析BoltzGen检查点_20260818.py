#!/usr/bin/env python3
"""Safely profile verified official PyTorch checkpoints without executing globals.

The official BoltzGen checkpoints use pickle protocol 5, which PyTorch 2.9's
weights_only unpickler does not currently accept. This inspector reads only the
small ``data.pkl`` member from the torch ZIP container and uses a restricted
metadata unpickler. It is suitable for keys, shapes and counts, not numerical
weight values.
"""

from __future__ import annotations

import builtins
import collections
import hashlib
import io
import json
import pickle
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime_cache"
OUT = ROOT / "metadata" / "checkpoint_profile.json"
EXPECTED = {
    "boltzgen1_diverse.ckpt": "360af8bd6e59527ff6ec25dd81253967f3bd3567d200053b10680634751f8e3c",
    "boltzgen1_adherence.ckpt": "ac7078b3dc13064c68e0c3fd542e5bc538c33558bf6607f65e499eb336ca5e5d",
    "boltzgen1_ifold.ckpt": "dd4cf108c94471bdc3a326b7b180fa3854dc019110fae780208c30b50bd56578",
    "boltz2_conf_final.ckpt": "525a51ef306da7282a54d23a4a5b91212fc60d0ff6b23b56dd6351de3b387530",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Opaque:
    """Inert stand-in for every non-allowlisted pickle global."""

    def __new__(cls, *args, **kwargs):
        obj = super().__new__(cls)
        obj.args = args
        obj.kwargs = kwargs
        obj.state = None
        return obj

    def __init__(self, *args, **kwargs):
        del args, kwargs

    def __setstate__(self, state):
        self.state = state


class StorageProxy:
    def __init__(self, dtype: str, key: str, elements: int, location: str):
        self.dtype = dtype
        self.key = key
        self.elements = elements
        self.location = location


class TensorProxy:
    def __init__(self, storage, offset, size, stride, *metadata):
        self.storage = storage
        self.offset = offset
        self.shape = tuple(int(value) for value in size)
        self.stride = tuple(int(value) for value in stride)
        self.metadata = metadata

    @property
    def dtype(self) -> str:
        return getattr(self.storage, "dtype", "unknown")

    def numel(self) -> int:
        result = 1
        for value in self.shape:
            result *= value
        return result


def rebuild_tensor(storage, offset, size, stride, *metadata):
    return TensorProxy(storage, offset, size, stride, *metadata)


def rebuild_parameter(tensor, *metadata):
    del metadata
    return tensor


class SafeMetadataUnpickler(pickle.Unpickler):
    """Restricted unpickler that creates only inert metadata objects."""

    _SAFE_BUILTINS = {"dict", "list", "int", "str", "tuple", "set", "float", "bool"}

    def __init__(self, stream):
        super().__init__(stream)
        self.global_names: set[str] = set()

    def find_class(self, module: str, name: str):
        self.global_names.add(f"{module}.{name}")
        if (module, name) == ("collections", "OrderedDict"):
            return collections.OrderedDict
        if (module, name) == ("collections", "defaultdict"):
            return collections.defaultdict
        if module == "builtins" and name in self._SAFE_BUILTINS:
            return getattr(builtins, name)
        if (module, name) == ("torch", "Size"):
            return tuple
        if name.startswith("_rebuild_tensor"):
            return rebuild_tensor
        if name.startswith("_rebuild_parameter"):
            return rebuild_parameter
        if name.endswith("Storage"):
            return type(name, (Opaque,), {"storage_name": name})
        safe_name = "".join(character if character.isalnum() else "_" for character in name)
        return type(safe_name or "OpaqueGlobal", (Opaque,), {"global_name": f"{module}.{name}"})

    def persistent_load(self, persistent_id):
        if isinstance(persistent_id, tuple) and persistent_id and persistent_id[0] == "storage":
            _, storage_type, key, location, elements, *_ = persistent_id
            storage_name = getattr(
                storage_type,
                "storage_name",
                getattr(storage_type, "__name__", "UnknownStorage"),
            )
            dtype = {
                "FloatStorage": "float32",
                "DoubleStorage": "float64",
                "HalfStorage": "float16",
                "BFloat16Storage": "bfloat16",
                "LongStorage": "int64",
                "IntStorage": "int32",
                "ShortStorage": "int16",
                "CharStorage": "int8",
                "ByteStorage": "uint8",
                "BoolStorage": "bool",
            }.get(storage_name, storage_name.removesuffix("Storage").lower())
            return StorageProxy(dtype, str(key), int(elements), str(location))
        return Opaque(persistent_id)


def safe_archive_metadata(path: Path):
    with zipfile.ZipFile(path) as archive:
        candidates = [name for name in archive.namelist() if name.endswith("/data.pkl")]
        if len(candidates) != 1:
            raise RuntimeError(f"Expected one data.pkl in {path.name}; found {candidates}")
        data_member = candidates[0]
        payload = archive.read(data_member)
        unpickler = SafeMetadataUnpickler(io.BytesIO(payload))
        result = unpickler.load()
        protocol = payload[1] if len(payload) >= 2 and payload[0] == 0x80 else None
        return result, {
            "data_pickle_member": data_member,
            "data_pickle_bytes": len(payload),
            "pickle_protocol": protocol,
            "referenced_globals": sorted(unpickler.global_names),
            "zip_member_count": len(archive.infolist()),
            "zip_uncompressed_member_bytes": sum(item.file_size for item in archive.infolist()),
        }


def tensor_group(key: str) -> str:
    parts = key.split(".")
    if parts and parts[0] == "model" and len(parts) > 1:
        return ".".join(parts[:2])
    return parts[0] if parts else "unknown"


def describe_top_value(value) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return f"mapping[{len(value)}]"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}[{len(value)}]"
    return type(value).__name__


def inspect_checkpoint(path: Path) -> dict:
    actual = sha256(path)
    if actual != EXPECTED[path.name]:
        raise RuntimeError(f"SHA-256 mismatch for {path.name}: {actual}")
    archive, container = safe_archive_metadata(path)
    if isinstance(archive, dict) and isinstance(archive.get("state_dict"), dict):
        state = archive["state_dict"]
        state_location = "state_dict"
    elif isinstance(archive, dict) and all(isinstance(value, TensorProxy) for value in archive.values()):
        state = archive
        state_location = "root"
    else:
        state = {}
        state_location = "not_found"
    tensors = []
    group_counts = Counter()
    dtype_counts = Counter()
    total_elements = 0
    for key, value in state.items():
        if not isinstance(value, TensorProxy):
            continue
        elements = value.numel()
        total_elements += elements
        dtype_counts[value.dtype] += 1
        group_counts[tensor_group(key)] += 1
        tensors.append({
            "key": key,
            "shape": list(value.shape),
            "stride": list(value.stride),
            "rank": len(value.shape),
            "dtype": value.dtype,
            "elements": elements,
            "storage_key": value.storage.key,
        })
    top_level = {}
    if isinstance(archive, dict):
        for key, value in archive.items():
            top_level[key] = describe_top_value(value)
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": path.stat().st_size,
        "sha256": actual,
        "load_policy": (
            "pinned SHA-256 + ZIP data.pkl only + restricted metadata unpickler; "
            "no module imports, no project globals executed, no tensor storage loaded"
        ),
        "container_type": type(archive).__name__,
        "state_location": state_location,
        "top_level_fields": top_level,
        "tensor_count": len(tensors),
        "total_tensor_elements": total_elements,
        "dtype_tensor_counts": dict(dtype_counts),
        "module_prefix_tensor_counts": dict(group_counts.most_common()),
        "sample_tensors": sorted(tensors, key=lambda item: item["key"])[:16],
        "largest_tensors": sorted(tensors, key=lambda item: item["elements"], reverse=True)[:12],
        **container,
    }


def main() -> None:
    profiles = []
    for name in EXPECTED:
        path = RUNTIME / name
        if not path.exists():
            raise FileNotFoundError(path)
        profiles.append(inspect_checkpoint(path))
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inspector": "Python stdlib restricted metadata unpickler",
        "weights_only_compatibility_note": (
            "PyTorch 2.9 weights_only rejected these protocol-5 checkpoints with "
            "Unsupported operand 149; this report did not fall back to unrestricted torch.load."
        ),
        "profiles": profiles,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUT), "checkpoint_count": len(profiles)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
