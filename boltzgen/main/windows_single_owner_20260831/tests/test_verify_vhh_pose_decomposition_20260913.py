"""Independent rotation checks for retrospective VHH pose verification."""
from pathlib import Path
import sys

import numpy as np
from scipy.spatial.transform import Rotation

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_vhh_pose_decomposition_20260913 as V


def test_global_rotation_translation_are_removed():
    points = np.random.default_rng(42).normal(size=(151, 3))
    moved = Rotation.from_euler("xyz", [21, 43, -17], degrees=True).apply(points) + [30, -5, 7]
    fitted = V.align_all(moved, points, np.arange(2, 28))
    assert np.allclose(fitted, points, atol=1e-10)


def test_binder_translation_is_not_removed_by_target_alignment():
    points = np.random.default_rng(17).normal(size=(151, 3))
    moved = points.copy()
    moved[30:] += [3, 0, 0]
    fitted = V.align_all(moved, points, np.arange(2, 28))
    assert V.rmsd(fitted[:30], points[:30]) < 1e-10
    assert abs(V.rmsd(fitted[30:], points[30:]) - 3) < 1e-10


def test_binder_self_fit_separates_pose_from_shape():
    points = np.random.default_rng(73).normal(size=(151, 3))
    moved = points.copy()
    moved[30:] = Rotation.from_euler("z", 78, degrees=True).apply(points[30:]) + [9, 15, 2]
    fitted = V.align_all(moved, points, np.arange(30, 151))
    assert V.rmsd(fitted[30:], points[30:]) < 1e-10
