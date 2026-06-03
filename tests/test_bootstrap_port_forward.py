from __future__ import annotations

import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stratatools.bootstrap import storage


class BootstrapPortForwardTests(unittest.TestCase):
    def test_local_port_forward_command_uses_expected_ports(self) -> None:
        with mock.patch.object(storage, "_is_wsl", return_value=False), mock.patch.dict(
            storage.os.environ, {}, clear=False
        ):
            self.assertEqual(
                storage._local_port_forward_command(),
                [
                    "kubectl",
                    "-n",
                    storage.NAMESPACE,
                    "port-forward",
                    "svc/monofs-external",
                    f"{storage.MONOFS_LOCAL_HTTP_PORT}:8080",
                    f"{storage.MONOFS_LOCAL_GRPC_PORT}:9090",
                ],
            )

    def test_local_port_forward_command_binds_all_interfaces_on_wsl(self) -> None:
        with mock.patch.object(storage, "_is_wsl", return_value=True), mock.patch.dict(
            storage.os.environ, {}, clear=False
        ):
            self.assertEqual(
                storage._local_port_forward_command(),
                [
                    "kubectl",
                    "-n",
                    storage.NAMESPACE,
                    "port-forward",
                    "--address",
                    "0.0.0.0",
                    "svc/monofs-external",
                    f"{storage.MONOFS_LOCAL_HTTP_PORT}:8080",
                    f"{storage.MONOFS_LOCAL_GRPC_PORT}:9090",
                ],
            )

    def test_deploy_ensures_local_port_forward(self) -> None:
        with mock.patch.object(storage, "_apply_manifests"), mock.patch.object(
            storage, "_wait_rollouts"
        ), mock.patch.object(
            storage, "_reconfigure_router_external_addresses"
        ), mock.patch.object(storage, "ensure_local_port_forward") as ensure:
            storage.deploy(False)

        ensure.assert_called_once_with(False)

    def test_rollout_ensures_local_port_forward(self) -> None:
        with mock.patch.object(storage, "_apply_manifests"), mock.patch.object(
            storage, "run"
        ), mock.patch.object(storage, "_wait_rollouts"), mock.patch.object(
            storage, "_reconfigure_router_external_addresses"
        ), mock.patch.object(storage, "ensure_local_port_forward") as ensure:
            storage.rollout(False)

        ensure.assert_called_once_with(False)

    def test_ensure_local_port_forward_restarts_managed_forward_when_command_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)
            pid_file = state_dir / "monofs-external.pid"
            log_file = state_dir / "monofs-external.log"
            cmd_file = state_dir / "monofs-external.cmd"
            pid_file.write_text("123\n", encoding="utf-8")
            desired_command = [
                "kubectl",
                "-n",
                storage.NAMESPACE,
                "port-forward",
                "--address",
                "0.0.0.0",
                "svc/monofs-external",
                f"{storage.MONOFS_LOCAL_HTTP_PORT}:8080",
                f"{storage.MONOFS_LOCAL_GRPC_PORT}:9090",
            ]

            proc = mock.Mock()
            proc.pid = 456
            proc.poll.side_effect = [None, None]

            with mock.patch.object(storage, "PORT_FORWARD_STATE_DIR", state_dir), mock.patch.object(
                storage, "MONOFS_PORT_FORWARD_PID_FILE", pid_file
            ), mock.patch.object(
                storage, "MONOFS_PORT_FORWARD_LOG_FILE", log_file
            ), mock.patch.object(
                storage, "MONOFS_PORT_FORWARD_CMD_FILE", cmd_file
            ), mock.patch.object(
                storage, "_local_port_forward_command", return_value=desired_command
            ), mock.patch.object(
                storage, "_managed_port_forward_pid", return_value=123
            ), mock.patch.object(
                storage, "_managed_port_forward_command", return_value=None
            ), mock.patch.object(
                storage, "_pid_alive", return_value=True
            ), mock.patch.object(
                storage, "_local_ports_open", side_effect=[False, True]
            ), mock.patch.object(
                storage, "stop_local_port_forward"
            ) as stop_forward, mock.patch.object(
                storage.subprocess, "Popen", return_value=proc
            ), mock.patch.object(storage, "info"):
                storage.ensure_local_port_forward(False)

            stop_forward.assert_called_once_with(False)
            self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), "456")
            self.assertEqual(json.loads(cmd_file.read_text(encoding="utf-8")), desired_command)


if __name__ == "__main__":
    unittest.main()