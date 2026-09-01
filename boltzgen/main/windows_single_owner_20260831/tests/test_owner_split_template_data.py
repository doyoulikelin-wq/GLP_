from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import torch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "owner_split_template_data.py"
SPEC = importlib.util.spec_from_file_location("owner_split_template_data", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


TARGET_COUNT = 30
CDR_COUNT = 30
FRAMEWORK_COUNT = 91
NUM_TOKENS = TARGET_COUNT + CDR_COUNT + FRAMEWORK_COUNT


def _synthetic_tokenized() -> SimpleNamespace:
    token_dtype = np.dtype(
        [
            ("res_type", "i8"),
            ("frame_rot", "f4", (9,)),
            ("frame_t", "f4", (3,)),
            ("disto_coords", "f4", (3,)),
            ("center_coords", "f4", (3,)),
            ("disto_mask", "f4"),
            ("frame_mask", "f4"),
        ]
    )
    tokens = np.zeros(NUM_TOKENS, dtype=token_dtype)
    indices = np.arange(NUM_TOKENS, dtype=np.float32)
    origins = np.stack(
        (1.25 * indices, np.sin(indices / 7.0), np.cos(indices / 11.0)), axis=1
    ).astype(np.float32)
    tokens["res_type"] = np.arange(NUM_TOKENS) % 20
    tokens["frame_rot"] = np.broadcast_to(
        np.eye(3, dtype=np.float32).reshape(9), (NUM_TOKENS, 9)
    )
    tokens["frame_t"] = origins
    tokens["center_coords"] = origins + np.array([0.3, 0.4, 0.5], dtype=np.float32)
    tokens["disto_coords"] = origins + np.array([-0.2, 0.1, 0.35], dtype=np.float32)
    tokens["disto_mask"] = 1.0
    tokens["frame_mask"] = 1.0
    return SimpleNamespace(tokens=tokens)


def _masks() -> tuple[torch.Tensor, torch.Tensor]:
    design = torch.zeros(NUM_TOKENS, dtype=torch.bool)
    chain_design = torch.zeros(NUM_TOKENS, dtype=torch.bool)
    chain_design[TARGET_COUNT:] = True
    design[TARGET_COUNT : TARGET_COUNT + CDR_COUNT] = True
    return design, chain_design


def _rigidly_move_vhh(
    tokenized: SimpleNamespace,
    chain_design_mask: torch.Tensor,
) -> SimpleNamespace:
    moved = copy.deepcopy(tokenized)
    mask = chain_design_mask.numpy()
    rotation = np.array(
        [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float32,
    )
    translation = np.array([67.0, -31.0, 19.0], dtype=np.float32)
    frames = moved.tokens["frame_rot"][mask].reshape(-1, 3, 3)
    moved.tokens["frame_rot"][mask] = np.einsum(
        "ij,njk->nik", rotation, frames
    ).reshape(-1, 9)
    for field in ("frame_t", "center_coords", "disto_coords"):
        moved.tokens[field][mask] = moved.tokens[field][mask] @ rotation.T + translation
    return moved


def _build_split(tokenized: SimpleNamespace) -> dict[str, torch.Tensor]:
    design, chain_design = _masks()
    return MODULE.build_split_template_features(
        tokenized,
        design,
        chain_design,
        expected_target_tokens=TARGET_COUNT,
        expected_cdr_tokens=CDR_COUNT,
        expected_framework_tokens=FRAMEWORK_COUNT,
    )


def test_split_masks_and_feature_shapes_match_30_30_91_contract() -> None:
    tokenized = _synthetic_tokenized()
    design, chain_design = _masks()
    target, cdr, framework = MODULE.split_template_masks(design, chain_design)

    assert (int(target.sum()), int(cdr.sum()), int(framework.sum())) == (30, 30, 91)
    assert not torch.any(target & cdr)
    assert not torch.any(target & framework)
    assert not torch.any(cdr & framework)
    assert torch.all(target | cdr | framework)

    features = _build_split(tokenized)
    assert features["template_restype"].shape == (2, NUM_TOKENS, 33)
    assert features["template_frame_rot"].shape == (2, NUM_TOKENS, 3, 3)
    assert features["template_frame_t"].shape == (2, NUM_TOKENS, 3)
    for key in (
        "template_mask_cb",
        "template_mask_frame",
        "template_mask",
        "query_to_template",
        "visibility_ids",
    ):
        assert features[key].shape == (2, NUM_TOKENS)

    expected_visibility = torch.stack((target, framework))
    assert torch.equal(features["template_mask"].bool(), expected_visibility)
    assert torch.equal(features["visibility_ids"].bool(), expected_visibility)
    assert not torch.any(features["template_mask"][:, cdr].bool())


def test_split_rejects_design_tokens_outside_design_chain() -> None:
    design, chain_design = _masks()
    design[0] = True
    with pytest.raises(ValueError, match="subset"):
        MODULE.split_template_masks(design, chain_design)


def test_split_cross_geometry_is_zero_and_full_preprojection_is_rigid_invariant() -> None:
    tokenized = _synthetic_tokenized()
    design, chain_design = _masks()
    target, _, framework = MODULE.split_template_masks(design, chain_design)
    moved = _rigidly_move_vhh(tokenized, chain_design)

    before = _build_split(tokenized)
    after = _build_split(moved)
    geometry = MODULE.template_geometry_pair_features(before)
    cross_target_framework = geometry[:, target][:, :, framework]
    cross_framework_target = geometry[:, framework][:, :, target]
    assert torch.count_nonzero(cross_target_framework) == 0
    assert torch.count_nonzero(cross_framework_target) == 0

    before_preprojection = MODULE.template_preprojection_pair_features(before)
    after_preprojection = MODULE.template_preprojection_pair_features(after)
    torch.testing.assert_close(
        before_preprojection,
        after_preprojection,
        rtol=0.0,
        atol=2e-5,
    )


def test_pose_coupled_control_changes_after_relative_rigid_transform() -> None:
    tokenized = _synthetic_tokenized()
    design, chain_design = _masks()
    target, _, framework = MODULE.split_template_masks(design, chain_design)
    moved = _rigidly_move_vhh(tokenized, chain_design)

    before = MODULE.build_coupled_template_features(tokenized, design, chain_design)
    after = MODULE.build_coupled_template_features(moved, design, chain_design)
    before_geometry = MODULE.template_geometry_pair_features(before)
    after_geometry = MODULE.template_geometry_pair_features(after)
    cross_before = before_geometry[:, target][:, :, framework]
    cross_after = after_geometry[:, target][:, :, framework]

    assert torch.count_nonzero(cross_before - cross_after) > 0
    assert not torch.allclose(
        MODULE.template_preprojection_pair_features(before),
        MODULE.template_preprojection_pair_features(after),
    )


class _FakeDataset:
    def __init__(self, sample: dict[str, object]) -> None:
        self.sample = sample

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int) -> dict[str, object]:
        assert index == 0
        return dict(self.sample)


def test_dataset_proxy_replaces_upstream_template_and_removes_injected_tokenized() -> None:
    tokenized = _synthetic_tokenized()
    design, chain_design = _masks()
    sample = {
        "tokenized": tokenized,
        "design_mask": design,
        "chain_design_mask": chain_design,
        "sentinel": "preserved",
    }
    proxy = MODULE.SplitTemplateDatasetProxy(
        _FakeDataset(sample),
        expected_target_tokens=TARGET_COUNT,
        expected_cdr_tokens=CDR_COUNT,
        expected_framework_tokens=FRAMEWORK_COUNT,
    )

    result = proxy[0]
    assert result["sentinel"] == "preserved"
    assert "tokenized" not in result
    assert result["template_mask"].shape == (2, NUM_TOKENS)
