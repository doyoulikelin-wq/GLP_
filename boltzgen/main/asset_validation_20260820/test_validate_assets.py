#!/usr/bin/env python3
"""Contract tests for logical asset identity and migrated symlink mounts.

These tests intentionally exercise only tiny temporary trees.  They define the
public API needed by the migration-safe validator without depending on the real
177/112-file registries or on mmCIF parsing.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).with_name("validate_assets.py")
MODULE_NAME = "asset_validation_under_test"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import bootstrap
    raise RuntimeError(f"cannot load validator module: {MODULE_PATH}")
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)


class MigrationSafeAssetContractTests(unittest.TestCase):
    """Tests for the migration-safe public API expected from validate_assets."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def require_api(self, name: str) -> Any:
        try:
            return getattr(VALIDATOR, name)
        except AttributeError:
            self.fail(
                f"待实现公开 API：validate_assets.{name}；"
                "具体签名见本测试中的调用。"
            )

    def make_file(self, relative_path: str, text: str = "fixture\n") -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def make_mount(
        self,
        *,
        mount_id: str,
        logical_path: str,
        canonical_relative_path: str,
        expected_file_count: int,
        expected_bytes: int,
        include_in_source_inventory: bool = True,
    ) -> Any:
        asset_mount = self.require_api("AssetMount")
        return asset_mount(
            mount_id=mount_id,
            logical_path=logical_path,
            canonical_uri=f"workspace://{canonical_relative_path}",
            asset_kind="tree",
            inventory_scope="contract_test",
            include_in_source_inventory=include_in_source_inventory,
            expected_file_count=expected_file_count,
            expected_bytes=expected_bytes,
        )

    def write_alias_contract(
        self,
        *,
        legacy_relative_path: str,
        target_relative_path: str,
        relative_link_text: str,
    ) -> Path:
        return self.make_file(
            "contracts/compatibility_aliases.tsv",
            "legacy_uri\ttarget_uri\trelative_link_text\n"
            f"workspace://{legacy_relative_path}\t"
            f"workspace://{target_relative_path}\t"
            f"{relative_link_text}\n",
        )

    def test_workspace_uri_parser_accepts_relative_posix_path(self) -> None:
        workspace_uri_to_lexical_path = self.require_api(
            "workspace_uri_to_lexical_path"
        )

        parsed = workspace_uri_to_lexical_path(
            "workspace://shared/data/glp1_positive/model_01.cif",
            self.root,
        )

        self.assertEqual(
            parsed.relative_to(self.root).as_posix(),
            "shared/data/glp1_positive/model_01.cif",
        )

    def test_workspace_uri_parser_rejects_absolute_and_parent_traversal(self) -> None:
        workspace_uri_to_lexical_path = self.require_api(
            "workspace_uri_to_lexical_path"
        )
        invalid_uris = (
            "/tmp/outside.cif",
            "file:///tmp/outside.cif",
            "workspace:///tmp/outside.cif",
            "workspace://../outside.cif",
            "workspace://data/../outside.cif",
        )

        for uri in invalid_uris:
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                workspace_uri_to_lexical_path(uri, self.root)

    def test_asset_ref_keeps_logical_path_when_physical_path_is_a_symlink(self) -> None:
        asset_ref = self.require_api("AssetRef")
        resolve_logical_path = self.require_api("resolve_logical_path")
        canonical_file = self.make_file("shared/canonical/sample.cif")
        alias_root = self.root / "shared/alias"
        alias_root.parent.mkdir(parents=True, exist_ok=True)
        alias_root.symlink_to("canonical", target_is_directory=True)
        mount = self.make_mount(
            mount_id="primary",
            logical_path="data/primary",
            canonical_relative_path="shared/alias",
            expected_file_count=1,
            expected_bytes=canonical_file.stat().st_size,
            include_in_source_inventory=False,
        )

        ref = resolve_logical_path(
            "data/primary/sample.cif",
            self.root,
            [mount],
        )

        self.assertIsInstance(ref, asset_ref)
        self.assertEqual(ref.logical_relative_path, "data/primary/sample.cif")
        self.assertEqual(ref.physical_path.resolve(), canonical_file.resolve())
        self.assertNotEqual(
            ref.logical_relative_path,
            canonical_file.relative_to(self.root).as_posix(),
            "logical identity must not be recomputed from Path.resolve()",
        )

    def test_explicit_mount_inventory_keeps_both_logical_mirrors(self) -> None:
        inventory_source_files = self.require_api("inventory_source_files")
        canonical_root = self.root / "shared/canonical"
        first = self.make_file("shared/canonical/first.cif", "first fixture\n")
        second = self.make_file(
            "shared/canonical/nested/second.txt", "second fixture\n"
        )
        total_bytes = first.stat().st_size + second.stat().st_size
        mounts = (
            self.make_mount(
                mount_id="primary",
                logical_path="data/primary",
                canonical_relative_path="shared/canonical",
                expected_file_count=2,
                expected_bytes=total_bytes,
            ),
            self.make_mount(
                mount_id="binding_mirror",
                logical_path="data/样本数据/binding-多构象",
                canonical_relative_path="shared/canonical",
                expected_file_count=2,
                expected_bytes=total_bytes,
            ),
        )
        errors: list[str] = []

        rows = inventory_source_files(self.root, list(mounts), errors)

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(row["relative_path"] for row in rows),
            [
                "data/primary/first.cif",
                "data/primary/nested/second.txt",
                "data/样本数据/binding-多构象/first.cif",
                "data/样本数据/binding-多构象/nested/second.txt",
            ],
        )
        digest_counts = Counter(row["sha256"] for row in rows)
        self.assertEqual(sorted(digest_counts.values()), [2, 2])
        self.assertEqual(len(rows), 4)

    def test_relative_compatibility_symlink_to_expected_target_is_valid(self) -> None:
        validate_aliases = self.require_api("validate_compatibility_aliases")
        self.make_file("shared/data/expected.cif")
        link = self.root / "legacy/expected.cif"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to("../shared/data/expected.cif")
        contract = self.write_alias_contract(
            legacy_relative_path="legacy/expected.cif",
            target_relative_path="shared/data/expected.cif",
            relative_link_text="../shared/data/expected.cif",
        )
        errors: list[str] = []

        verified = validate_aliases(contract, self.root, errors)

        self.assertEqual(verified, 1)
        self.assertEqual(errors, [])

    def test_tampered_compatibility_symlink_is_rejected(self) -> None:
        validate_aliases = self.require_api("validate_compatibility_aliases")
        self.make_file("shared/data/expected.cif")
        self.make_file("shared/data/tampered.cif")
        link = self.root / "legacy/expected.cif"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to("../shared/data/tampered.cif")
        contract = self.write_alias_contract(
            legacy_relative_path="legacy/expected.cif",
            target_relative_path="shared/data/expected.cif",
            relative_link_text="../shared/data/expected.cif",
        )
        errors: list[str] = []

        verified = validate_aliases(contract, self.root, errors)

        self.assertEqual(verified, 0)
        self.assertTrue(errors)

    def test_absolute_compatibility_symlink_is_rejected(self) -> None:
        validate_aliases = self.require_api("validate_compatibility_aliases")
        target = self.make_file("shared/data/expected.cif")
        link = self.root / "legacy/expected.cif"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target.resolve())
        contract = self.write_alias_contract(
            legacy_relative_path="legacy/expected.cif",
            target_relative_path="shared/data/expected.cif",
            relative_link_text="../shared/data/expected.cif",
        )
        errors: list[str] = []

        verified = validate_aliases(contract, self.root, errors)

        self.assertEqual(verified, 0)
        self.assertTrue(errors)

    def test_dangling_compatibility_symlink_is_rejected(self) -> None:
        validate_aliases = self.require_api("validate_compatibility_aliases")
        link = self.root / "legacy/missing.cif"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to("../shared/data/missing.cif")
        contract = self.write_alias_contract(
            legacy_relative_path="legacy/missing.cif",
            target_relative_path="shared/data/missing.cif",
            relative_link_text="../shared/data/missing.cif",
        )
        errors: list[str] = []

        verified = validate_aliases(contract, self.root, errors)

        self.assertEqual(verified, 0)
        self.assertTrue(errors)

    def test_hard_gates_enforce_historical_counts_and_logical_uniqueness(self) -> None:
        assert_hard_gates = self.require_api("assert_inventory_hard_gates")
        source_inventory = [
            {"relative_path": f"source/{index:03d}.dat"}
            for index in range(177)
        ]
        structures = [
            {
                "relative_path": f"structure/{index:03d}.cif",
                "parse_status": "PASS",
            }
            for index in range(112)
        ]

        assert_hard_gates(
            source_inventory=source_inventory,
            structures=structures,
        )

        with self.subTest(reason="wrong row count"), self.assertRaises(ValueError):
            assert_hard_gates(
                source_inventory=source_inventory[:-1],
                structures=structures,
            )

        with self.subTest(reason="wrong structure count"), self.assertRaises(
            ValueError
        ):
            assert_hard_gates(
                source_inventory=source_inventory,
                structures=structures[:-1],
            )

        duplicate_source_inventory = source_inventory[:-1] + [source_inventory[0]]
        with self.subTest(reason="duplicate logical identity"), self.assertRaises(
            ValueError
        ):
            assert_hard_gates(
                source_inventory=duplicate_source_inventory,
                structures=structures,
            )

        duplicate_structures = structures[:-1] + [structures[0]]
        with self.subTest(reason="duplicate structure identity"), self.assertRaises(
            ValueError
        ):
            assert_hard_gates(
                source_inventory=source_inventory,
                structures=duplicate_structures,
            )

        failed_parse_structures = [dict(row) for row in structures]
        failed_parse_structures[-1]["parse_status"] = "FAIL"
        with self.subTest(reason="structure parse-pass count"), self.assertRaises(
            ValueError
        ):
            assert_hard_gates(
                source_inventory=source_inventory,
                structures=failed_parse_structures,
            )

    def test_scaffold_checksum_contract_requires_exact_unique_verified_count(self) -> None:
        verify_checksums = self.require_api("verify_checksum_manifest")
        library_root = self.root / "scaffolds"
        first = self.make_file("scaffolds/first.cif", "first\n")
        second = self.make_file("scaffolds/second.yaml", "second\n")

        def digest(path: Path) -> str:
            return self.require_api("sha256_file")(path)

        manifest = self.make_file(
            "scaffolds/checksums.sha256",
            f"{digest(first)}  first.cif\n{digest(second)}  second.yaml\n",
        )
        errors: list[str] = []
        total, verified = verify_checksums(
            manifest,
            library_root,
            errors,
            expected_count=2,
        )
        self.assertEqual((total, verified), (2, 2))
        self.assertEqual(errors, [])

        manifest.write_text(
            f"{digest(first)}  first.cif\n{digest(first)}  first.cif\n",
            encoding="utf-8",
        )
        errors = []
        total, verified = verify_checksums(
            manifest,
            library_root,
            errors,
            expected_count=2,
        )
        self.assertEqual((total, verified), (2, 1))
        self.assertTrue(any("duplicate" in error for error in errors))
        self.assertTrue(any("expected 2 verified" in error for error in errors))

        manifest.write_text(f"{digest(first)}  first.cif\n", encoding="utf-8")
        errors = []
        total, verified = verify_checksums(
            manifest,
            library_root,
            errors,
            expected_count=2,
        )
        self.assertEqual((total, verified), (1, 1))
        self.assertTrue(any("expected 2 entries" in error for error in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
