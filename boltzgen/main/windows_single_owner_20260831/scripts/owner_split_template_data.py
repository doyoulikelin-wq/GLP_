#!/usr/bin/env python3
"""Non-circular target/framework template adapter for owner-mode folding.

The adapter deliberately places the target and the VHH framework in different
template slots. CDR tokens are absent from both slots, so no template contains
the target and VHH in a shared coordinate frame.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import torch
from torch import Tensor
from torch.nn.functional import one_hot
from torch.utils.data import Dataset

from boltzgen.task.predict.data_from_generated import (
    FromGeneratedDataModule,
    template_from_tokens,
)


_TEMPLATE_KEYS = (
    "template_restype",
    "template_frame_rot",
    "template_frame_t",
    "template_cb",
    "template_ca",
    "template_mask_cb",
    "template_mask_frame",
    "template_mask",
    "query_to_template",
    "visibility_ids",
)


def _one_dimensional_bool(name: str, value: Any) -> Tensor:
    tensor = torch.as_tensor(value, dtype=torch.bool).detach().cpu()
    if tensor.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got {tuple(tensor.shape)}")
    return tensor


def split_template_masks(
    design_mask: Any,
    chain_design_mask: Any,
) -> tuple[Tensor, Tensor, Tensor]:
    """Return mutually exclusive target, CDR, and framework token masks."""
    design = _one_dimensional_bool("design_mask", design_mask)
    chain_design = _one_dimensional_bool("chain_design_mask", chain_design_mask)
    if design.shape != chain_design.shape:
        raise ValueError(
            "design_mask and chain_design_mask must have the same shape: "
            f"{tuple(design.shape)} != {tuple(chain_design.shape)}"
        )
    if torch.any(design & ~chain_design):
        raise ValueError("design_mask must be a subset of chain_design_mask")

    target = ~chain_design
    cdr = design
    framework = chain_design & ~design
    masks = {"target": target, "cdr": cdr, "framework": framework}
    for name, mask in masks.items():
        if not torch.any(mask):
            raise ValueError(f"split-template input has no {name} tokens")
    if torch.any(target & cdr) or torch.any(target & framework) or torch.any(cdr & framework):
        raise AssertionError("split-template masks overlap")
    if not torch.all(target | cdr | framework):
        raise AssertionError("split-template masks do not cover every token")
    return target, cdr, framework


def _check_expected_count(name: str, mask: Tensor, expected: int | None) -> None:
    if expected is None:
        return
    observed = int(mask.sum().item())
    if observed != int(expected):
        raise ValueError(f"{name} token count mismatch: expected={expected} observed={observed}")


def _concatenate_template_slots(
    target_features: Mapping[str, Tensor],
    framework_features: Mapping[str, Tensor],
) -> dict[str, Tensor]:
    if set(target_features) != set(_TEMPLATE_KEYS):
        raise AssertionError("unexpected target template feature keys")
    if set(framework_features) != set(_TEMPLATE_KEYS):
        raise AssertionError("unexpected framework template feature keys")
    result: dict[str, Tensor] = {}
    for key in _TEMPLATE_KEYS:
        target_value = target_features[key]
        framework_value = framework_features[key]
        if target_value.shape[0] != 1 or framework_value.shape[0] != 1:
            raise AssertionError(f"{key} inputs must each contain exactly one template slot")
        if target_value.shape[1:] != framework_value.shape[1:]:
            raise AssertionError(f"{key} template slot shapes do not match")
        result[key] = torch.cat((target_value, framework_value), dim=0)
    return result


def validate_split_template_features(
    features: Mapping[str, Tensor],
    target_mask: Tensor,
    cdr_mask: Tensor,
    framework_mask: Tensor,
) -> None:
    """Fail closed if a split template violates the two-slot contract."""
    num_tokens = int(target_mask.numel())
    expected_shapes = {
        "template_restype": (2, num_tokens, 33),
        "template_frame_rot": (2, num_tokens, 3, 3),
        "template_frame_t": (2, num_tokens, 3),
        "template_cb": (2, num_tokens, 3),
        "template_ca": (2, num_tokens, 3),
        "template_mask_cb": (2, num_tokens),
        "template_mask_frame": (2, num_tokens),
        "template_mask": (2, num_tokens),
        "query_to_template": (2, num_tokens),
        "visibility_ids": (2, num_tokens),
    }
    for key, expected_shape in expected_shapes.items():
        if key not in features:
            raise AssertionError(f"missing split-template feature: {key}")
        value = features[key]
        if tuple(value.shape) != expected_shape:
            raise AssertionError(
                f"{key} shape mismatch: expected={expected_shape} observed={tuple(value.shape)}"
            )
        if value.is_floating_point() and not torch.isfinite(value).all():
            raise AssertionError(f"{key} contains NaN or Inf")

    expected_visibility = torch.stack((target_mask, framework_mask), dim=0)
    template_mask = features["template_mask"].bool()
    visibility = features["visibility_ids"].bool()
    if not torch.equal(template_mask, expected_visibility):
        raise AssertionError("template_mask does not match target/framework slot assignment")
    if not torch.equal(visibility, expected_visibility):
        raise AssertionError("visibility_ids do not match target/framework slot assignment")
    if torch.any(template_mask[:, cdr_mask]):
        raise AssertionError("CDR tokens are visible in a template slot")
    for key in ("template_mask_cb", "template_mask_frame"):
        if torch.any(features[key].bool() & ~template_mask):
            raise AssertionError(f"{key} escapes template_mask")
    if torch.any(features["query_to_template"] != 0):
        raise AssertionError("query_to_template unexpectedly contains non-zero indices")


def build_split_template_features(
    tokenized: Any,
    design_mask: Any,
    chain_design_mask: Any,
    *,
    expected_target_tokens: int | None = None,
    expected_cdr_tokens: int | None = None,
    expected_framework_tokens: int | None = None,
) -> dict[str, Tensor]:
    """Build target-only slot 0 and framework-only slot 1 template features."""
    target, cdr, framework = split_template_masks(design_mask, chain_design_mask)
    if len(tokenized.tokens) != target.numel():
        raise ValueError(
            "tokenized input and split masks differ in length: "
            f"{len(tokenized.tokens)} != {target.numel()}"
        )
    _check_expected_count("target", target, expected_target_tokens)
    _check_expected_count("CDR", cdr, expected_cdr_tokens)
    _check_expected_count("framework", framework, expected_framework_tokens)

    target_features = template_from_tokens(tokenized, target.numpy(), tdim=1)
    framework_features = template_from_tokens(tokenized, framework.numpy(), tdim=1)
    features = _concatenate_template_slots(target_features, framework_features)
    validate_split_template_features(features, target, cdr, framework)
    return features


def build_coupled_template_features(
    tokenized: Any,
    design_mask: Any,
    chain_design_mask: Any,
) -> dict[str, Tensor]:
    """Build the deliberately pose-coupled technical control used by CPU tests."""
    target, _, framework = split_template_masks(design_mask, chain_design_mask)
    coupled_mask = (target | framework).numpy()
    return template_from_tokens(tokenized, coupled_mask, tdim=1)


def _batched_template_features(
    features: Mapping[str, Tensor],
) -> tuple[dict[str, Tensor], bool]:
    restype = features["template_restype"]
    if restype.ndim == 3:
        return {key: features[key].unsqueeze(0) for key in _TEMPLATE_KEYS}, True
    if restype.ndim == 4:
        return {key: features[key] for key in _TEMPLATE_KEYS}, False
    raise ValueError(
        "template_restype must have shape [T,N,C] or [B,T,N,C], got "
        f"{tuple(restype.shape)}"
    )


def _geometry_from_batched_templates(
    features: Mapping[str, Tensor],
    *,
    min_dist: float,
    max_dist: float,
    num_bins: int,
) -> Tensor:
    frame_rot = features["template_frame_rot"].float()
    frame_t = features["template_frame_t"].float()
    frame_mask = features["template_mask_frame"].float()
    cb_coords = features["template_cb"].float()
    ca_coords = features["template_ca"].float()
    cb_mask = features["template_mask_cb"].float()
    visibility_ids = features["visibility_ids"]

    cb_pair_mask = (cb_mask[:, :, :, None] * cb_mask[:, :, None, :])[..., None]
    frame_pair_mask = (
        frame_mask[:, :, :, None] * frame_mask[:, :, None, :]
    )[..., None]
    visibility_pair_mask = (
        visibility_ids[:, :, :, None] == visibility_ids[:, :, None, :]
    ).float()

    cb_distances = torch.cdist(cb_coords, cb_coords)
    boundaries = torch.linspace(
        min_dist,
        max_dist,
        num_bins - 1,
        device=cb_distances.device,
        dtype=cb_distances.dtype,
    )
    distogram = one_hot(
        (cb_distances[..., None] > boundaries).sum(dim=-1).long(),
        num_classes=num_bins,
    ).to(cb_distances.dtype)

    inverse_frame_rot = frame_rot.unsqueeze(2).transpose(-1, -2)
    frame_origins = frame_t.unsqueeze(2).unsqueeze(-1)
    ca_columns = ca_coords.unsqueeze(3).unsqueeze(-1)
    vectors = torch.matmul(inverse_frame_rot, ca_columns - frame_origins)
    norms = torch.linalg.vector_norm(vectors, dim=-1, keepdim=True)
    unit_vectors = torch.where(norms > 0, vectors / norms, torch.zeros_like(vectors))
    unit_vectors = unit_vectors.squeeze(-1)

    geometry = torch.cat(
        (distogram, cb_pair_mask, unit_vectors, frame_pair_mask), dim=-1
    )
    return geometry * visibility_pair_mask.unsqueeze(-1)


def template_geometry_pair_features(
    features: Mapping[str, Tensor],
    *,
    min_dist: float = 3.25,
    max_dist: float = 50.75,
    num_bins: int = 38,
) -> Tensor:
    """Mirror BoltzGen trunk geometry channels for a CPU non-leakage audit."""
    batched, was_unbatched = _batched_template_features(features)
    geometry = _geometry_from_batched_templates(
        batched, min_dist=min_dist, max_dist=max_dist, num_bins=num_bins
    )
    return geometry[0] if was_unbatched else geometry


def template_preprojection_pair_features(
    features: Mapping[str, Tensor],
    *,
    min_dist: float = 3.25,
    max_dist: float = 50.75,
    num_bins: int = 38,
) -> Tensor:
    """Mirror all pose-relevant inputs immediately before trunk a_proj."""
    batched, was_unbatched = _batched_template_features(features)
    geometry = _geometry_from_batched_templates(
        batched, min_dist=min_dist, max_dist=max_dist, num_bins=num_bins
    )
    restype = batched["template_restype"].to(geometry.dtype)
    num_tokens = restype.shape[2]
    restype_i = restype[:, :, :, None].expand(-1, -1, -1, num_tokens, -1)
    restype_j = restype[:, :, None, :].expand(-1, -1, num_tokens, -1, -1)
    preprojection = torch.cat((geometry, restype_i, restype_j), dim=-1)
    return preprojection[0] if was_unbatched else preprojection


class SplitTemplateDatasetProxy(Dataset):
    """Replace upstream folding templates without copying its parsing pipeline."""

    def __init__(
        self,
        base_dataset: Dataset,
        *,
        expected_target_tokens: int | None = None,
        expected_cdr_tokens: int | None = None,
        expected_framework_tokens: int | None = None,
        keep_tokenized: bool = False,
    ) -> None:
        self.base_dataset = base_dataset
        self.expected_target_tokens = expected_target_tokens
        self.expected_cdr_tokens = expected_cdr_tokens
        self.expected_framework_tokens = expected_framework_tokens
        self.keep_tokenized = keep_tokenized

    def __len__(self) -> int:
        return len(self.base_dataset)

    def _replace_templates(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(sample)
        if "tokenized" not in result:
            raise KeyError("base dataset did not return the required tokenized feature")
        tokenized = result["tokenized"]
        split_features = build_split_template_features(
            tokenized,
            result["design_mask"],
            result["chain_design_mask"],
            expected_target_tokens=self.expected_target_tokens,
            expected_cdr_tokens=self.expected_cdr_tokens,
            expected_framework_tokens=self.expected_framework_tokens,
        )
        result.update(split_features)
        if not self.keep_tokenized:
            result.pop("tokenized")
        return result

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self._replace_templates(self.base_dataset[index])

    def get_sample(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        get_sample = getattr(self.base_dataset, "get_sample")
        return self._replace_templates(get_sample(*args, **kwargs))


class SplitTemplateFromGeneratedDataModule(FromGeneratedDataModule):
    """Hydra-compatible folding DataModule using non-circular split templates."""

    def __init__(
        self,
        *args: Any,
        target_templates: bool = False,
        design_mask_templates: bool = False,
        extra_features: Sequence[str] | None = None,
        expected_target_tokens: int | None = None,
        expected_cdr_tokens: int | None = None,
        expected_framework_tokens: int | None = None,
        **kwargs: Any,
    ) -> None:
        if target_templates is not True:
            raise ValueError("split-template folding requires target_templates=true")
        if design_mask_templates:
            raise ValueError(
                "split-template folding rejects design_mask_templates=true because it is pose-coupled"
            )
        requested_features = list(extra_features or ())
        keep_tokenized = "tokenized" in requested_features
        if not keep_tokenized:
            requested_features.append("tokenized")

        super().__init__(
            *args,
            target_templates=target_templates,
            design_mask_templates=design_mask_templates,
            extra_features=requested_features,
            **kwargs,
        )
        self.predict_set = SplitTemplateDatasetProxy(
            self.predict_set,
            expected_target_tokens=expected_target_tokens,
            expected_cdr_tokens=expected_cdr_tokens,
            expected_framework_tokens=expected_framework_tokens,
            keep_tokenized=keep_tokenized,
        )


__all__ = [
    "SplitTemplateDatasetProxy",
    "SplitTemplateFromGeneratedDataModule",
    "build_coupled_template_features",
    "build_split_template_features",
    "split_template_masks",
    "template_geometry_pair_features",
    "template_preprojection_pair_features",
    "validate_split_template_features",
]
