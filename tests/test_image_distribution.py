from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stratatools import image


class ImageDistributionTests(unittest.TestCase):
    def test_cmd_push_keeps_docker_partitions_local_on_kind(self) -> None:
        with mock.patch.object(
            image, "BUILD_RECIPES", {"lolipop": [("demo:latest", [], Path("/tmp"))]}
        ), mock.patch.object(
            image, "_partition_mapping", return_value={"demo:latest": "demo:sha256-1234"}
        ), mock.patch.object(
            image, "kubectl_context", return_value="kind-strata"
        ), mock.patch.object(
            image, "run"
        ) as run_mock, mock.patch.object(
            image, "_cluster_load"
        ) as cluster_load_mock, mock.patch.object(
            image, "info"
        ):
            image.cmd_push(["lolipop"], registry="localhost:5000", dry_run=False)

        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertIn(["docker", "tag", "demo:latest", "demo:sha256-1234"], commands)
        self.assertNotIn(["docker", "push", "demo:sha256-1234"], commands)
        cluster_load_mock.assert_not_called()

    def test_partition_image_target_nodes_uses_payload_node_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            partitions = root / "partitions"
            part_dir = partitions / "demo"
            (part_dir / "intents").mkdir(parents=True)
            (part_dir / "payloads").mkdir(parents=True)

            intent = {
                "apiVersion": "guardian/v1alpha1",
                "kind": "Intent",
                "metadata": {"name": "demo"},
                "spec": {
                    "target": {"cluster": "k8s-main"},
                    "assets": [
                        {
                            "type": "Compute",
                            "name": "demo",
                            "payload": {"k8s": "/partitions/demo/payloads/demo.k8s.yaml"},
                            "properties": {"image": "demo:latest"},
                        }
                    ],
                },
            }
            payload = {
                "nodeSelector": {
                    "node-role.kubernetes.io/control-plane": "",
                }
            }
            (part_dir / "intents" / "demo.yaml").write_text(yaml.safe_dump(intent, sort_keys=False))
            (part_dir / "payloads" / "demo.k8s.yaml").write_text(yaml.safe_dump(payload, sort_keys=False))

            with mock.patch.object(image, "PARTITIONS", partitions), mock.patch.object(
                image, "ROOT", root
            ), mock.patch.object(
                image,
                "_kind_nodes_with_labels",
                return_value=[
                    ("strata-control-plane", {"node-role.kubernetes.io/control-plane": ""}),
                    ("strata-worker", {"node-role.kubernetes.io/worker": ""}),
                ],
            ):
                nodes = image._partition_image_target_nodes("demo", "demo:latest", dry_run=False)

        self.assertEqual(nodes, ["strata-control-plane"])

    def test_cmd_push_handles_mixed_partition_per_image(self) -> None:
        intents = [
            {
                "spec": {
                    "target": {"cluster": "k8s-main"},
                    "assets": [
                        {"properties": {"image": "guardian:sha256-old"}},
                    ],
                }
            },
            {
                "spec": {
                    "target": {"cluster": "docker-main"},
                    "assets": [
                        {"properties": {"image": "guardian-pusher-docker:sha256-old"}},
                    ],
                }
            },
        ]
        recipes = {
            "guardian-configs": [
                ("guardian:latest", [], Path("/tmp")),
                ("guardian-pusher-docker:latest", [], Path("/tmp")),
            ]
        }
        mapping = {
            "guardian:latest": "guardian:sha256-1111",
            "guardian-pusher-docker:latest": "guardian-pusher-docker:sha256-2222",
        }
        with mock.patch.object(image, "BUILD_RECIPES", recipes), mock.patch.object(
            image, "_partition_intents", return_value=intents
        ), mock.patch.object(
            image, "_partition_mapping", return_value=mapping
        ), mock.patch.object(
            image, "kubectl_context", return_value="kind-strata"
        ), mock.patch.object(
            image, "run"
        ) as run_mock, mock.patch.object(
            image, "_cluster_load"
        ) as cluster_load_mock, mock.patch.object(
            image, "_partition_image_target_nodes", return_value=["strata-control-plane"]
        ), mock.patch.object(
            image, "info"
        ):
            image.cmd_push(["guardian-configs"], registry="localhost:5000", dry_run=False)

        cluster_load_mock.assert_called_once_with(
            "guardian:sha256-1111", False, nodes=["strata-control-plane"]
        )
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertIn(
            ["docker", "tag", "guardian-pusher-docker:latest", "guardian-pusher-docker:sha256-2222"],
            commands,
        )
        self.assertNotIn(["docker", "push", "guardian-pusher-docker:sha256-2222"], commands)

    def test_cluster_load_force_reloads_even_when_present(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        try:
            with mock.patch.object(
                image,
                "_kind_nodes_with_labels",
                return_value=[("strata-control-plane", {})],
            ), mock.patch.object(
                image.subprocess,
                "run",
                side_effect=[
                    mock.Mock(returncode=0),  # docker save
                ],
            ) as run_mock, mock.patch.object(
                image.subprocess,
                "Popen",
                return_value=mock.Mock(wait=mock.Mock(), returncode=0),
            ) as popen_mock:
                with mock.patch.object(image.tempfile, "NamedTemporaryFile") as ntf:
                    ntf.return_value.__enter__.return_value.name = temp_path
                    ntf.return_value.__exit__.return_value = False
                    image._cluster_load("guardian:latest", dry_run=False, force=True)

                commands = [call.args[0] for call in run_mock.call_args_list]
                self.assertEqual(commands, [["docker", "save", "-o", temp_path, "guardian:latest"]])
                popen_mock.assert_called_once()
        finally:
            Path(temp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
