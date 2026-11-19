"""Safety regression tests for the conducted CC1101 laboratory one-shot mode.

All tests use a fake SPI device. NO radio or antenna can be activated.
"""
import contextlib
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from keyrfmonitor.hardware import cc1101_bus as rxbus
from keyrfmonitor.hardware import cc1101_tx_lab as tx


class FakeSpi:
    def __init__(self, *, version=0x14, bad_reg=None, stuck=False, underflow=False):
        self.version = version
        self.bad_reg = bad_reg
        self.stuck = stuck
        self.underflow = underflow
        self.writes = {}
        self.transfers = []
        self.strobes = []
        self.packet = None
        self.state = tx.STATE_IDLE
        self.tx_reads = 0
        self.closed = False

    def open(self, bus, device):
        assert (bus, device) == (1, 0)

    def xfer2(self, msg):
        self.transfers.append(tuple(msg))
        h = msg[0]
        if len(msg) == 1:
            self.strobes.append(h)
            if h == tx.SRES:
                self.writes.clear()
                self.state = tx.STATE_IDLE
            elif h == tx.STX:
                self.state = tx.STATE_TX
                self.tx_reads = 0
            elif h in (tx.SIDLE, tx.SPWD):
                self.state = tx.STATE_IDLE
            return [0] * len(msg)
        if h == 0x7F:
            self.packet = bytes(msg[1:])
            return [0] * len(msg)
        if h in (0xF0, 0xF1, 0xF5):
            if h == 0xF0:
                return [0, 0x00]
            if h == 0xF1:
                return [0, self.version]
            if self.state == tx.STATE_TX:
                if self.underflow:
                    return [0, tx.STATE_UNDERFLOW]
                self.tx_reads += 1
                if not self.stuck and self.tx_reads >= 3:
                    self.state = tx.STATE_IDLE
            return [0, self.state]
        if h & 0x80:
            addr = h & 0x3F
            value = self.writes.get(addr, 0)
            if self.bad_reg == addr:
                value ^= 1
            return [0, value]
        if len(msg) == 2:
            self.writes[h] = msg[1]
            return [0, 0]
        raise AssertionError(f"unexpected transfer {msg}")

    def close(self):
        self.closed = True


class TestConductedTx(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.device = Path(self.temp.name) / "spidev1.0"
        self.device.touch()
        self.spi = FakeSpi()

    @staticmethod
    def fast_sleep(_):
        pass

    def clock(self, step=0.003):
        current = [0.0]
        def tick():
            current[0] += step
            return current[0]
        return tick

    def one(self, spi=None, confirmed=True, clock=None):
        obj = self.spi if spi is None else spi
        return tx.run_one_shot(
            confirmed=confirmed, device_path=self.device,
            spi_factory=lambda: obj, sleep=self.fast_sleep,
            monotonic=clock or self.clock(),
        )

    def test_plan_is_inert_and_fixed(self):
        result = tx.plan()
        self.assertEqual(result["frequency_hz"], 433_400_000)
        self.assertEqual(result["fixed_payload_ascii"], "MBRF-LAB-ONLY-01")
        self.assertEqual(result["repetition_count"], 1)
        self.assertFalse(result["hardware_tx_verified"])
        self.assertLess(result["expected_packet_airtime_ms_approx"], 50)

    def test_confirmation_required_before_spi(self):
        with self.assertRaises(tx.LabTxError):
            self.one(confirmed=False)
        self.assertFalse(self.spi.transfers)

    def test_missing_device_blocks_before_spi(self):
        self.device.unlink()
        with self.assertRaises(tx.LabTxError):
            self.one()
        self.assertFalse(self.spi.transfers)

    def test_fixed_one_shot_completes_and_powers_down(self):
        result = self.one()
        self.assertEqual(result["packets_requested"], 1)
        self.assertEqual(self.spi.strobes.count(tx.STX), 1)
        self.assertEqual(self.spi.packet, tx.PAYLOAD)
        self.assertEqual(self.spi.writes[0x3E], 0)
        self.assertEqual(self.spi.strobes[-1], tx.SPWD)
        self.assertTrue(self.spi.closed)

    def test_wrong_chip_never_transmits(self):
        spi = FakeSpi(version=0xFF)
        with self.assertRaisesRegex(tx.LabTxError, "nicht plausibel"):
            self.one(spi)
        self.assertNotIn(tx.STX, spi.strobes)
        self.assertIn(tx.SPWD, spi.strobes)
        self.assertTrue(spi.closed)

    def test_register_readback_mismatch_never_transmits(self):
        spi = FakeSpi(bad_reg=0x0F)
        with self.assertRaisesRegex(tx.LabTxError, "Readback"):
            self.one(spi)
        self.assertNotIn(tx.STX, spi.strobes)
        self.assertEqual(spi.writes[0x3E], 0)

    def test_stuck_transmitter_has_bounded_timeout_and_cleanup(self):
        spi = FakeSpi(stuck=True)
        with self.assertRaisesRegex(tx.LabTxError, "Timeout"):
            self.one(spi, clock=self.clock(step=0.02))
        self.assertEqual(spi.strobes.count(tx.STX), 1)
        self.assertIn(tx.SIDLE, spi.strobes)
        self.assertEqual(spi.writes[0x3E], 0)
        self.assertEqual(spi.strobes[-1], tx.SPWD)

    def test_tx_underflow_triggers_cleanup(self):
        spi = FakeSpi(underflow=True)
        with self.assertRaisesRegex(tx.LabTxError, "Unterlauf"):
            self.one(spi)
        self.assertEqual(spi.writes[0x3E], 0)
        self.assertEqual(spi.strobes[-1], tx.SPWD)

    def test_rx_bus_still_rejects_every_tx_primitive(self):
        for command in ([rxbus.STX], [rxbus.SFTX], [0x7F, 0xAA],
                        [0x3E, tx.PA_BYTE]):
            with self.subTest(command=command):
                with self.assertRaises(rxbus.UnsafeCC1101Command):
                    rxbus.check_payload(command)

    def test_tx_bus_blocks_unapproved_power_register_and_second_stx(self):
        spi = FakeSpi()
        bus = tx._FixedPacketBus(lambda: spi)
        with self.assertRaises(tx.LabTxError):
            bus._write(0x3E, 0xC0)
        with self.assertRaises(tx.LabTxError):
            bus._write(0x0F, 0xFF)
        bus._strobe(tx.STX)
        with self.assertRaisesRegex(tx.LabTxError, "Mehr als"):
            bus._strobe(tx.STX)
        bus.shutdown()
        self.assertEqual(spi.strobes.count(tx.STX), 1)

    def test_cross_process_lock_prevents_tx_while_rx_owns_spi(self):
        with rxbus.cc1101_device_lock(self.device):
            with self.assertRaisesRegex(tx.LabTxError, "belegt"):
                self.one()
        self.assertFalse(self.spi.transfers)

    def test_cli_preview_cannot_touch_radio(self):
        from keyrfmonitor.__main__ import main
        with contextlib.redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["cc1101", "lab-tx"]), 0)
        self.assertIn("CONDUCTED_LAB_ONLY", output.getvalue())

    def test_cli_requires_explicit_confirmation(self):
        from keyrfmonitor.__main__ import main
        with contextlib.redirect_stderr(StringIO()):
            self.assertEqual(main(["cc1101", "lab-tx", "--execute"]), 2)

    def test_cli_rejects_non_interactive_execution(self):
        from keyrfmonitor.__main__ import main
        with patch("sys.stdin", new=SimpleNamespace(isatty=lambda: False)), contextlib.redirect_stderr(StringIO()):
            self.assertEqual(main(["cc1101", "lab-tx", "--execute", "--confirm-conducted"]), 2)

    def test_cli_wrong_manual_phrase_never_calls_driver(self):
        from keyrfmonitor.__main__ import main
        with (patch("sys.stdin", new=SimpleNamespace(isatty=lambda: True)),
              patch("builtins.input", return_value="NO"),
              patch.object(tx, "run_one_shot") as run,
              contextlib.redirect_stdout(StringIO()),
              contextlib.redirect_stderr(StringIO())):
            self.assertEqual(main(["cc1101", "lab-tx", "--execute", "--confirm-conducted"]), 2)
            run.assert_not_called()

    def test_cli_manual_phrase_calls_one_shot_once(self):
        from keyrfmonitor.__main__ import main
        with (patch("sys.stdin", new=SimpleNamespace(isatty=lambda: True)),
              patch("builtins.input", return_value="SEND 1 LAB PACKET"),
              patch.object(tx, "run_one_shot", return_value={"status": "simulated"}) as run,
              contextlib.redirect_stdout(StringIO()),
              contextlib.redirect_stderr(StringIO())):
            self.assertEqual(main(["cc1101", "lab-tx", "--execute", "--confirm-conducted"]), 0)
            run.assert_called_once_with(confirmed=True)


if __name__ == "__main__":
    unittest.main()
