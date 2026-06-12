from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stratatools.bootstrap import guardian, storage


class BootstrapExternalServiceIPTests(unittest.TestCase):
    def test_storage_external_service_spec_renders_external_ips(self) -> None:
        with mock.patch.dict(
            storage.os.environ, {"EXTERNAL_SERVICE_IPS": "172.21.63.46,172.21.63.47"}, clear=False
        ):
            self.assertEqual(
                storage._external_service_spec_yaml(),
                "\n  externalIPs:\n    - 172.21.63.46\n    - 172.21.63.47",
            )

    def test_storage_external_endpoint_prefers_explicit_ip(self) -> None:
        with mock.patch.object(storage, "_service_explicit_host", return_value="172.21.63.46"), mock.patch.object(
            storage, "_service_type"
        ) as service_type:
            self.assertEqual(
                storage._service_external_endpoint("node-a-external", "9006"),
                "172.21.63.46:9006",
            )
        service_type.assert_not_called()

    def test_guardian_external_endpoint_prefers_explicit_ip(self) -> None:
        with mock.patch.object(
            guardian, "_service_explicit_host", return_value="172.21.63.46"
        ), mock.patch.object(guardian, "_service_type") as service_type:
            self.assertEqual(
                guardian._service_external_endpoint("lb-edge", "monofs-external", "grpc", "9090"),
                "172.21.63.46:9090",
            )
        service_type.assert_not_called()

    def test_lb_edge_host_prefers_configured_external_service_ip(self) -> None:
        with mock.patch.dict(
            guardian.os.environ, {"EXTERNAL_SERVICE_IP": "172.21.63.46"}, clear=False
        ):
            self.assertEqual(guardian._lb_edge_host(), "172.21.63.46")

    def test_lb_edge_host_ignores_port_forward_override(self) -> None:
        with mock.patch.dict(
            guardian.os.environ,
            {
                "EXTERNAL_SERVICE_IP": "172.21.63.46",
                "MONOFS_PORT_FORWARD_ADDRESS": "127.0.0.1",
            },
            clear=False,
        ):
            self.assertEqual(guardian._lb_edge_host(), "172.21.63.46")

    def test_guardian_ui_url_uses_lb_service_endpoint(self) -> None:
        with mock.patch.dict(
            guardian.os.environ,
            {"EXTERNAL_SERVICE_IP": "", "EXTERNAL_SERVICE_IPS": ""},
            clear=False,
        ), mock.patch.object(
            guardian, "_resolve_service_external_endpoint", return_value="172.21.63.46:8090"
        ):
            self.assertEqual(guardian.guardian_ui_url(), "http://172.21.63.46:8090")


if __name__ == "__main__":
    unittest.main()
