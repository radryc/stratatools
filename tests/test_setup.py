from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stratatools import setup


class SetupTests(unittest.TestCase):
    def test_check_cluster_reachability_provisions_kind_when_unreachable(self) -> None:
        with mock.patch.object(
            setup, "_try_version", side_effect=[(False, ""), (True, "")]
        ) as try_version, mock.patch.object(
            setup, "_ensure_kind_cluster", return_value=True
        ) as ensure_kind, mock.patch.object(setup, "info"):
            cluster_ok = setup._check_cluster_reachability(
                [],
                auto_kind=True,
                kind_name="strata",
                kind_workers=3,
                dry_run=False,
                allow_create=True,
            )

        self.assertTrue(cluster_ok)
        ensure_kind.assert_called_once_with("strata", 3, dry_run=False)
        self.assertEqual(try_version.call_args_list[0].args[0], ["kubectl", "cluster-info"])
        self.assertEqual(try_version.call_args_list[1].args[0], ["kubectl", "cluster-info"])

    def test_kind_config_includes_requested_workers(self) -> None:
        config = setup._kind_config(3)

        self.assertIn("kind: Cluster", config)
        self.assertIn("  - role: control-plane", config)
        self.assertEqual(config.count("  - role: worker"), 3)


if __name__ == "__main__":
    unittest.main()