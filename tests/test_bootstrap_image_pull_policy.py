from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stratatools.bootstrap import guardian, storage


class BootstrapImagePullPolicyTests(unittest.TestCase):
    def test_storage_render_sets_if_not_present_on_local_images(self) -> None:
        fetcher_yaml = storage._render("deploy-fetcher.yaml", {"SUFFIX": "a"})
        router_yaml = storage._render("deploy-router.yaml", storage._router_vars("a"))
        search_yaml = storage._render("deploy-search-index.yaml")

        self.assertIn("imagePullPolicy: IfNotPresent", fetcher_yaml)
        self.assertIn("imagePullPolicy: IfNotPresent", router_yaml)
        self.assertIn("imagePullPolicy: IfNotPresent", search_yaml)

    def test_guardian_render_sets_if_not_present_on_local_images(self) -> None:
        with unittest.mock.patch.object(
            guardian, "_guardian_monofs_client_api_endpoint", return_value="127.0.0.1:9090"
        ):
            guardiand_yaml = guardian._render("deploy-guardiand.yaml")
            pusher_yaml = guardian._render("deploy-pusher-k8s.yaml")

        self.assertIn("imagePullPolicy: IfNotPresent", guardiand_yaml)
        self.assertIn("imagePullPolicy: IfNotPresent", pusher_yaml)


if __name__ == "__main__":
    unittest.main()