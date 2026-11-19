"""Read-only SPI setup tests. No actual radio/SDR hardware is required."""
import contextlib
import io
import unittest
from unittest.mock import patch

from keyrfmonitor import __main__
from keyrfmonitor.hardware.cc1101 import probe, wiring_lines, CC1101ProbeError


class FakeSPI:
    def __init__(self, partnum=0x00, version=0x14):
        self.partnum, self.version = partnum, version
        self.commands = []
        self.closed = False
        self.open_args = None
        self.mode = None
        self.max_speed_hz = None
        self.bits_per_word = None

    def open(self, bus, device):
        self.open_args = (bus, device)

    def xfer2(self, payload):
        self.commands.append(payload)
        assert payload in ([0xF0, 0x00], [0xF1, 0x00]), "Unzulässiger SPI-Befehl"
        return [0x00, self.partnum if payload[0] == 0xF0 else self.version]

    def close(self):
        self.closed = True


class CC1101Tests(unittest.TestCase):
    def test_probe_only_reads_two_status_registers(self):
        spi = FakeSPI()
        result = probe(spi_factory=lambda: spi)
        self.assertEqual(spi.open_args, (1, 0))  # SPI1/CE0: SPI0 belongs to Pironman RGB
        self.assertEqual(spi.commands, [[0xF0, 0x00], [0xF1, 0x00]])
        self.assertEqual(spi.max_speed_hz, 500_000)
        self.assertTrue(spi.closed)
        self.assertTrue(result["cc1101_plausible"])
        self.assertFalse(result["tx_implemented"])
        self.assertFalse(result["rx_implemented"])

    def test_disconnected_values_are_not_reported_as_chip(self):
        self.assertFalse(probe(spi_factory=lambda: FakeSPI(0xFF, 0xFF))["cc1101_plausible"])
        self.assertFalse(probe(spi_factory=lambda: FakeSPI(0, 0))["cc1101_plausible"])

    def test_spi_close_on_error(self):
        class BrokenSPI(FakeSPI):
            def xfer2(self, payload):
                raise OSError("EIO")
        spi = BrokenSPI()
        with self.assertRaises(CC1101ProbeError):
            probe(spi_factory=lambda: spi)
        self.assertTrue(spi.closed)

    def test_reject_invalid_spi_coordinates(self):
        for b, d, speed in ((-1, 0, 500000), (0, 2, 500000), (0, 0, 0)):
            with self.assertRaises(ValueError):
                probe(bus=b, device=d, speed_hz=speed, spi_factory=FakeSPI)

    def test_wiring_is_passive_and_identifies_3v3(self):
        wiring = " ".join(wiring_lines())
        self.assertIn("3V3", wiring)
        for pin in ("Pin 40", "Pin 38", "Pin 35", "Pin 12", "spi1-1cs"):
            self.assertIn(pin, wiring)
        self.assertNotIn("SPI0", wiring)
        self.assertIn("Pin 22", wiring)
        self.assertIn("Pin 18", wiring)

    def test_cli_wiring_without_spidev_or_probe(self):
        out = io.StringIO()
        with patch("keyrfmonitor.hardware.cc1101.probe") as mocked, contextlib.redirect_stdout(out):
            code = __main__.main(["cc1101", "wiring"])
        self.assertEqual(code, 0)
        self.assertIn("SPI1 CE0", out.getvalue())
        mocked.assert_not_called()

    def test_cli_probe_reporting(self):
        fake = {"cc1101_plausible": True, "version": "0x14", "tx_implemented": False}
        out = io.StringIO()
        with patch("keyrfmonitor.hardware.cc1101.probe", return_value=fake), contextlib.redirect_stdout(out):
            code = __main__.main(["cc1101", "probe"])
        self.assertEqual(code, 0)
        self.assertIn("0x14", out.getvalue())

    def test_cli_probe_defaults_to_spi1(self):
        fake = {"cc1101_plausible": True}
        with patch("keyrfmonitor.hardware.cc1101.probe", return_value=fake) as mocked, \
                contextlib.redirect_stdout(io.StringIO()):
            __main__.main(["cc1101", "probe"])
        mocked.assert_called_once_with(bus=1, device=0)

    def test_cli_refuses_spi0_while_pironman_rgb_uses_it(self):
        err = io.StringIO()
        with patch("keyrfmonitor.hardware.cc1101.probe") as mocked, \
                patch("keyrfmonitor.hardware.cc1101.spi0_used_by_pironman", return_value=True), \
                contextlib.redirect_stderr(err):
            code = __main__.main(["cc1101", "probe", "--bus", "0"])
        self.assertEqual(code, 2)
        self.assertIn("Pironman", err.getvalue())
        mocked.assert_not_called()
        with patch("keyrfmonitor.hardware.cc1101.probe", return_value={"cc1101_plausible": True}) as mocked, \
                patch("keyrfmonitor.hardware.cc1101.spi0_used_by_pironman", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            code = __main__.main(["cc1101", "probe", "--bus", "0", "--allow-spi0"])
        self.assertEqual(code, 0)
        mocked.assert_called_once_with(bus=0, device=0)

    def test_pironman_config_detection(self):
        import json, tempfile
        from pathlib import Path
        from keyrfmonitor.hardware.cc1101 import spi0_used_by_pironman
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            self.assertFalse(spi0_used_by_pironman(config))  # missing file
            config.write_text(json.dumps({"system": {"rgb_enable": True}}))
            self.assertTrue(spi0_used_by_pironman(config))
            config.write_text(json.dumps({"system": {"rgb_enable": False}}))
            self.assertFalse(spi0_used_by_pironman(config))
            config.write_text("[1]")
            self.assertFalse(spi0_used_by_pironman(config))

    def test_missing_spi_node_explains_overlay(self):
        from pathlib import Path
        with patch("keyrfmonitor.hardware.cc1101.spi_device_path",
                   return_value=Path("/nonexistent/spidev1.0")), \
                patch.dict("sys.modules", {"spidev": None}):
            with self.assertRaises(CC1101ProbeError) as ctx:
                probe()
        self.assertIn("spi1-1cs", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
