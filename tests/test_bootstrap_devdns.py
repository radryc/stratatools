from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stratatools.bootstrap import devdns


class DevDNSBootstrapTests(unittest.TestCase):
    def test_daemon_command_includes_server_ip_override(self) -> None:
        with mock.patch.object(devdns, "DEVDNS_SERVER_IP", "192.168.1.50"):
            command = devdns._daemon_command("0.0.0.0:80")

        self.assertIn("--server-ip", command)
        self.assertIn("192.168.1.50", command)

    def test_startup_proxy_addrs_adds_fallback_for_default_port_80(self) -> None:
        addrs = devdns._startup_proxy_addrs("127.0.0.1:80", explicit=False)

        self.assertEqual(addrs, ["127.0.0.1:80", "127.0.0.1:18080"])

    def test_startup_proxy_addrs_respects_explicit_proxy_addr(self) -> None:
        addrs = devdns._startup_proxy_addrs("127.0.0.1:80", explicit=True)

        self.assertEqual(addrs, ["127.0.0.1:80"])

    def test_route_url_uses_active_proxy_addr_from_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "daemon.json"
            state_path.write_text('{"proxy_addr": "127.0.0.1:18080"}', encoding="utf-8")
            with mock.patch.object(devdns, "DAEMON_STATE_FILE", state_path):
                self.assertEqual(devdns.route_url("doctor.strata"), "http://doctor.strata:18080")


if __name__ == "__main__":
    unittest.main()