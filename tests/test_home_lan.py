"""Restricted dual-bind home-LAN tests; no access to real network hardware."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from keyrfmonitor import __main__, server


class HomeNetworkTests(unittest.TestCase):
    def test_peer_cidr_rejects_external_and_other_private_networks(self):
        allowed = ("127.0.0.1", "::1", "192.168.178.1", "192.168.178.63",
                   "192.168.178.254")
        for peer in allowed:
            with self.subTest(allowed=peer):
                self.assertTrue(server.client_allowed(peer))
        blocked = ("8.8.8.8", "10.0.0.3", "192.168.177.63",
                   "192.168.179.1", "192.168.178.1.evil",
                   "172.20.5.1", "2001:db8::1", "0.0.0.0")
        for peer in blocked:
            with self.subTest(blocked=peer):
                self.assertFalse(server.client_allowed(peer))

    def test_serve_rejects_any_other_bind_address(self):
        for host in ("0.0.0.0", "::", "192.168.178.63", "192.168.178.64"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                server.serve(host, 8890)
        for lan_host in ("0.0.0.0", "192.168.178.64", "10.0.0.1", "::"):
            with self.subTest(lan_host=lan_host), self.assertRaises(ValueError):
                server.serve("127.0.0.1", 8890, lan_host=lan_host)

    def test_dual_listener_uses_only_two_exact_ips(self):
        class DummyServer:
            def __init__(self, address, handler):
                self.address = address
            def serve_forever(self, **kwargs):
                return
            def shutdown(self):
                return
            def server_close(self):
                return

        with tempfile.TemporaryDirectory() as td:
            addresses = []
            def create(address, handler):
                addresses.append(address)
                return DummyServer(address, handler)
            with patch.object(server, "ThreadingHTTPServer", side_effect=create):
                server.serve(
                    "127.0.0.1", 8890, str(Path(td) / "events.sqlite3"),
                    lan_host="192.168.178.63")
        self.assertEqual(addresses,
                         [("127.0.0.1", 8890), ("192.168.178.63", 8890)])

    def test_serve_cli_accepts_only_configured_lan_address(self):
        with patch.object(__main__, "serve") as mocked:
            self.assertEqual(__main__.main(
                ["serve", "--host", "127.0.0.1", "--lan-host",
                 "192.168.178.63", "--port", "8890"]), 0)
            mocked.assert_called_once_with(
                "127.0.0.1", 8890, None, lan_host="192.168.178.63")
        with self.assertRaises(SystemExit) as e:
            __main__.main(["serve", "--lan-host", "192.168.178.64"])
        self.assertEqual(e.exception.code, 2)

    def test_installer_preflight_no_wildcard_no_adsb_stop(self):
        script = (Path(__file__).resolve().parents[1]
                  / "scripts/pi/enable-lan-dashboard.sh").read_text()
        self.assertIn("ip -4 -o address show", script)
        self.assertIn("systemctl is-active --quiet dump1090.service", script)
        self.assertIn("http://127.0.0.1:$PORT/api/status", script)
        self.assertIn("http://$LAN:$PORT/api/status", script)
        self.assertIn("--lan-host $LAN", script)
        self.assertNotIn("systemctl stop dump1090.service", script)
        self.assertNotIn("0.0.0.0 --port", script)


if __name__ == "__main__":
    unittest.main()
