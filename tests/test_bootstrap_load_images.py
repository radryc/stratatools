from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stratatools.bootstrap import guardian, storage


class BootstrapLoadImagesTests(unittest.TestCase):
    def test_storage_load_images_uses_cluster_loader_on_kind(self) -> None:
        with mock.patch("stratatools.image.kubectl_context", return_value="kind-strata"), mock.patch(
            "stratatools.image._cluster_load_mode", return_value=True
        ), mock.patch("stratatools.image._cluster_load") as cluster_load, mock.patch.object(
            storage, "info"
        ):
            storage.load_images(dry_run=False)

        self.assertEqual(
            [call.args[0] for call in cluster_load.call_args_list],
            [
                storage.MONOFS_SERVER_IMAGE,
                storage.MONOFS_ROUTER_IMAGE,
                storage.MONOFS_FETCHER_IMAGE,
                storage.MONOFS_SEARCH_IMAGE,
            ],
        )

    def test_guardian_load_images_uses_cluster_loader_on_kind(self) -> None:
        with mock.patch("stratatools.image.kubectl_context", return_value="kind-strata"), mock.patch(
            "stratatools.image._cluster_load_mode", return_value=True
        ), mock.patch("stratatools.image._cluster_load") as cluster_load, mock.patch.object(
            guardian, "info"
        ):
            guardian.load_images(dry_run=False)

        self.assertEqual(
            [call.args[0] for call in cluster_load.call_args_list],
            [guardian.GUARDIAN_IMAGE, guardian.GUARDIAN_PUSHER_IMAGE],
        )


if __name__ == "__main__":
    unittest.main()