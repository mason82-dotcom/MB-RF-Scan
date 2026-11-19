"""Receive-only CC1101 tests with a simulated chip; no SPI hardware, no RF."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keyrfmonitor import __main__
from keyrfmonitor.hardware import cc1101_bus as b
from keyrfmonitor.hardware.cc1101_rx import (
    CC1101Receiver, CC1101SetupError, RX_REGISTERS, RssiBurstDetector,
    RssiDetectorConfig, carrier_frequency_hz, configure_rx, rssi_to_dbm,
)
from keyrfmonitor.storage import EventStore, _ALLOWED

import test_summary_http as _http_base


class FakeChip:
    """Minimal CC1101 register/state model behind a spidev-like interface."""

    def __init__(self, version=0x14, rssi=lambda: 0x00, accept_writes=True):
        self.regs = {}
        self.version = version
        self.rssi = rssi
        self.accept_writes = accept_writes
        self.state = b.MARCSTATE_IDLE
        self.log = []
        self.closed = False
        self.mode = self.max_speed_hz = self.bits_per_word = None

    def open(self, bus, device):
        self.opened = (bus, device)

    def close(self):
        self.closed = True

    def xfer2(self, payload):
        self.log.append(list(payload))
        header = payload[0]
        address = header & 0x3F
        if header & b.READ:
            if header & b.BURST:
                value = {b.PARTNUM: 0x00, b.VERSION: self.version,
                         b.MARCSTATE: self.state, b.RSSI: self.rssi()}.get(address, 0)
            else:
                value = self.regs.get(address, 0)
            return [0x0F, value]
        if 0x30 <= address <= 0x3D:
            self.state = {b.SRES: b.MARCSTATE_IDLE, b.SIDLE: b.MARCSTATE_IDLE,
                          b.SCAL: b.MARCSTATE_IDLE, b.SRX: b.MARCSTATE_RX,
                          b.SPWD: 0x00, b.SFRX: self.state, b.SNOP: self.state}[address]
            return [0x0F]
        if self.accept_writes:
            self.regs[address] = payload[1]
        return [0x0F, 0x0F]


def bus_for(chip):
    return b.SafeCC1101Bus(1, 0, spi_factory=lambda: chip)


class SafetyLayerTests(unittest.TestCase):
    def test_transmit_paths_are_rejected(self):
        forbidden = [
            [b.STX], [b.STX | b.BURST], [b.SFSTXON], [b.SXOFF], [b.SWOR],
            [b.SFTX], [b.SWORRST],
            [b.FIFO, 0x55], [b.FIFO | b.BURST, 0x55],          # TX FIFO write
            [b.READ | b.FIFO, 0x00], [b.READ | b.BURST | b.FIFO, 0x00],  # RX payload
            [b.PATABLE, 0xC0], [b.PATABLE, 0x01],               # output power
            [b.PATABLE | b.BURST, 0x00],                        # burst write
            [b.READ | b.BURST | b.PATABLE, 0x00],
            [b.IOCFG0, 0x0D], [b.IOCFG2, 0x0D], [b.IOCFG1, 0x0C], [b.IOCFG0, 0x4B],
            [b.IOCFG2, 0x16], [b.IOCFG2, 0x17], [b.IOCFG0, 0x1D],  # RX_HARD_DATA, symbol tick
            [b.IOCFG0, 0x3F], [b.IOCFG0, 0x8E],                     # clock out, temp sensor bit
            [b.READ | b.BURST | b.PKTSTATUS, 0x00],                  # mirrors GDO levels
            [b.READ | b.BURST | b.LQI, 0x00], [b.READ | b.BURST | b.FREQEST, 0x00],
            [b.READ | b.MARCSTATE, 0x00],                            # no burst bit = strobe
            [b.MCSM1, 0x08],                                     # RXOFF -> TX
            [0x2F, 0x00], [0x0D | b.BURST, 0x10],
            [b.READ | b.RSSI, 0x01],                             # non-zero dummy
            [0x0D, 0x10, 0xB0], [], [0x100], ["x"],
        ]
        for payload in forbidden:
            with self.subTest(payload=payload), self.assertRaises(b.UnsafeCC1101Command):
                b.check_payload(payload)

    def test_exhaustive_single_and_two_byte_space(self):
        """No allowed transfer can strobe TX, touch the FIFO or set PA power."""
        allowed_strobes = []
        for header in range(256):
            try:
                b.check_payload([header])
                allowed_strobes.append(header & 0x3F)
            except b.UnsafeCC1101Command:
                pass
            for value in range(256):
                try:
                    b.check_payload([header, value])
                except b.UnsafeCC1101Command:
                    continue
                address = header & 0x3F
                self.assertNotEqual(address, b.FIFO)
                if header & b.READ and header & b.BURST:
                    self.assertIn(address, b.READABLE_STATUS)
                if not header & b.READ and address in (b.IOCFG0, b.IOCFG1, b.IOCFG2):
                    self.assertIn(value & 0xBF, b.ALLOWED_GDO_VALUES)
                if not header & b.READ:
                    self.assertLessEqual(address, b.PATABLE)
                    self.assertFalse(0x30 <= address <= 0x3D)
                    if address == b.PATABLE:
                        self.assertEqual(value, 0)
        self.assertEqual(set(allowed_strobes), set(b.ALLOWED_STROBES))
        self.assertNotIn(b.STX, allowed_strobes)

    def test_rx_configuration_passes_the_allowlist(self):
        for address, value, _ in RX_REGISTERS:
            b.check_payload([address, value])
        self.assertEqual(dict((a, v) for a, v, _ in RX_REGISTERS)[b.IOCFG0], 0x0E)

    def test_bus_never_reaches_spidev_with_forbidden_bytes(self):
        chip = FakeChip()
        bus = bus_for(chip)
        with self.assertRaises(b.UnsafeCC1101Command):
            bus.strobe(b.STX)
        with self.assertRaises(b.UnsafeCC1101Command):
            bus.write_register(b.PATABLE, 0xC0)
        self.assertEqual(chip.log, [])


class ConfigurationTests(unittest.TestCase):
    def test_frequency_and_rssi_conversion(self):
        self.assertAlmostEqual(carrier_frequency_hz() / 1e6, 433.9155, places=3)
        self.assertEqual(rssi_to_dbm(0x00), -74.0)
        self.assertEqual(rssi_to_dbm(0x80), -138.0)
        self.assertEqual(rssi_to_dbm(0x7F), -10.5)
        with self.assertRaises(ValueError):
            rssi_to_dbm(256)

    def test_configure_writes_verifies_and_enters_rx(self):
        chip = FakeChip()
        configure_rx(bus_for(chip), sleep=lambda _: None)
        self.assertEqual(chip.state, b.MARCSTATE_RX)
        for address, value, _ in RX_REGISTERS:
            self.assertEqual(chip.regs[address], value)
        self.assertEqual(chip.regs[b.PATABLE], 0x00)
        self.assertNotIn([b.STX], chip.log)

    def test_missing_chip_or_rejected_writes_fail_cleanly(self):
        with self.assertRaises(CC1101SetupError):
            configure_rx(bus_for(FakeChip(version=0x00)), sleep=lambda _: None)
        chip = FakeChip(accept_writes=False)
        with self.assertRaises(CC1101SetupError) as ctx:
            configure_rx(bus_for(chip), sleep=lambda _: None)
        self.assertIn("Register", str(ctx.exception))
        self.assertNotEqual(chip.state, b.MARCSTATE_RX)


def series(segments, period_ms=1.0):
    """[(duration_ms, dBm), ...] -> [(t_seconds, dBm), ...]"""
    t = 0.0
    out = []
    for duration, level in segments:
        for _ in range(int(duration / period_ms)):
            out.append((t, level))
            t += period_ms / 1000
    return out


class DetectorTests(unittest.TestCase):
    def run_series(self, samples):
        detector = RssiBurstDetector(started_wall=1_700_000_000.0)
        events = []
        for t, level in samples:
            events.extend(detector.process(t, level))
        return detector, events

    def test_pair_of_175ms_bursts_513ms_apart(self):
        samples = series([(600, -100), (175, -60), (338, -100), (175, -58), (300, -100)])
        detector, events = self.run_series(samples)
        bursts = [e for e in events if e["kind"] == "burst"]
        pairs = [e for e in events if e["kind"] == "pair"]
        self.assertEqual(len(bursts), 2)
        self.assertEqual(len(pairs), 1)
        self.assertAlmostEqual(bursts[0]["duration_ms"], 175, delta=2)
        self.assertAlmostEqual(pairs[0]["start_gap_ms"], 513, delta=2)
        self.assertEqual(bursts[1]["rssi_dbm"], -58.0)
        self.assertAlmostEqual(bursts[0]["peak_rise_db"], 40, delta=0.5)
        for event in events:
            self.assertEqual(event["source"], "cc1101")
            self.assertFalse(set(event) - _ALLOWED)
        self.assertEqual(detector.status()["baseline_dbm"], -100.0)

    def test_spikes_long_carriers_and_weak_signals_are_not_bursts(self):
        samples = series([(600, -100), (20, -60), (300, -100), (900, -60),
                          (300, -100), (175, -94), (300, -100)])
        _, events = self.run_series(samples)
        self.assertEqual(events, [])

    def test_short_dip_inside_burst_does_not_split_it(self):
        samples = series([(600, -100), (90, -60), (5, -100), (90, -60), (300, -100)])
        _, events = self.run_series(samples)
        self.assertEqual([e["kind"] for e in events], ["burst"])
        self.assertAlmostEqual(events[0]["duration_ms"], 185, delta=2)

    def test_long_carrier_adapts_baseline_and_detection_resumes(self):
        # Noise floor jumps by 20 dB for 5 s, then a real burst pair arrives on top.
        samples = series([(600, -100), (5000, -80), (175, -50), (338, -80),
                          (175, -50), (300, -80)])
        detector, events = self.run_series(samples)
        self.assertEqual([e["kind"] for e in events], ["burst", "burst", "pair"])
        self.assertGreaterEqual(detector.long_carrier_count, 1)
        self.assertAlmostEqual(detector.status()["baseline_dbm"], -80, delta=1)

    def test_burst_at_end_is_discarded_on_finish(self):
        detector = RssiBurstDetector()
        for t, level in series([(600, -100), (100, -60)]):
            detector.process(t, level)
        detector.finish()
        self.assertEqual(detector.burst_count, 0)
        self.assertFalse(detector.status()["burst_active"])


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.lock = threading.Lock()

    def clock(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.now += max(seconds, 0.0005)


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.lock = self.dir / "spidev-stand-in"
        self.lock.touch()

    def tearDown(self):
        self.temp.cleanup()

    def _receiver(self, chip, events, clock, **kwargs):
        on_event = kwargs.pop("on_event", events.append)
        return CC1101Receiver(on_event, lock_path=self.lock,
                              bus_factory=lambda: bus_for(chip),
                              clock=clock.clock, sleep=clock.sleep, **kwargs)

    def test_receiver_detects_pair_and_powers_down_on_stop(self):
        clock = FakeClock()
        levels = series([(600, -100), (175, -60), (338, -100), (175, -60), (2000, -100)])

        def rssi():
            index = min(int(clock.clock() * 1000), len(levels) - 1)
            dbm = levels[index][1]
            return int((dbm + 74) * 2) & 0xFF

        chip = FakeChip(rssi=rssi)
        events = []
        receiver = self._receiver(chip, events, clock)
        ok, message = receiver.start()
        self.assertTrue(ok, message)
        for _ in range(500):
            if [e for e in events if e["kind"] == "pair"]:
                break
            threading.Event().wait(0.01)
        status = receiver.status()
        self.assertEqual(status["state"], "running")
        self.assertTrue(receiver.stop()[0])
        self.assertEqual([e["kind"] for e in events], ["burst", "burst", "pair"])
        self.assertEqual(chip.log[-2:], [[b.SIDLE], [b.SPWD]])
        self.assertTrue(chip.closed)
        self.assertNotIn([b.STX], chip.log)
        self.assertFalse(status["tx_implemented"])
        self.assertEqual(receiver.status()["state"], "stopped")

    def test_missing_hardware_reports_error_without_crash(self):
        def broken():
            raise OSError("No such file or directory: /dev/spidev1.0")
        receiver = CC1101Receiver(lambda e: None, lock_path=self.lock, bus_factory=broken)
        ok, message = receiver.start()
        self.assertFalse(ok)
        self.assertIn("spidev1.0", message)
        self.assertIn("erneut starten", message)
        status = receiver.status()
        self.assertEqual(status["state"], "error")
        self.assertIn("OSError", status["error_detail"])
        self.assertFalse(receiver.stop()[0])

    def test_device_lock_prevents_parallel_use(self):
        chip = FakeChip()
        receiver = CC1101Receiver(lambda e: None, lock_path=self.lock,
                                  bus_factory=lambda: bus_for(chip))
        with b.cc1101_device_lock(self.lock):
            ok, message = receiver.start()
        self.assertFalse(ok)
        self.assertIn("anderen Prozess", message)
        self.assertEqual(chip.log, [])

    def _wait(self, predicate, seconds=5):
        for _ in range(int(seconds / 0.01)):
            if predicate():
                return True
            threading.Event().wait(0.01)
        return False

    def test_permission_error_names_the_spi_group(self):
        def denied():
            raise PermissionError(13, "Permission denied", "/dev/spidev1.0")
        receiver = CC1101Receiver(lambda e: None, lock_path=self.lock, bus_factory=denied)
        ok, message = receiver.start()
        self.assertFalse(ok)
        self.assertIn("Gruppe spi", message)

    def test_restart_after_error_is_not_overwritten_by_old_thread(self):
        clock = FakeClock()
        attempts = {"n": 0}
        chip = FakeChip(rssi=lambda: 0x34)

        def factory():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("spidev weg")
            return bus_for(chip)

        receiver = CC1101Receiver(lambda e: None, lock_path=self.lock, bus_factory=factory,
                                  clock=clock.clock, sleep=clock.sleep)
        self.assertFalse(receiver.start()[0])
        self.assertTrue(self._wait(lambda: not receiver._thread.is_alive()))
        ok, message = receiver.start()
        self.assertTrue(ok, message)
        threading.Event().wait(0.05)
        self.assertEqual(receiver.status()["state"], "running")
        self.assertTrue(receiver.stop()[0])
        self.assertEqual(receiver.status()["state"], "stopped")

    def test_stop_during_start_is_not_overridden(self):
        clock = FakeClock()
        gate = threading.Event()
        chip = FakeChip(rssi=lambda: 0x34)

        def slow_factory():
            gate.wait(2)
            return bus_for(chip)

        receiver = CC1101Receiver(lambda e: None, lock_path=self.lock, bus_factory=slow_factory,
                                  clock=clock.clock, sleep=clock.sleep)
        result = {}
        starter = threading.Thread(target=lambda: result.setdefault("start", receiver.start()))
        starter.start()
        self.assertTrue(self._wait(lambda: receiver.status()["state"] == "starting"))
        stopper = threading.Thread(target=lambda: result.setdefault("stop", receiver.stop()))
        stopper.start()
        self.assertTrue(self._wait(lambda: receiver.status()["state"] == "stopping"))
        gate.set()
        starter.join(5)
        stopper.join(5)
        self.assertFalse(result["start"][0])
        self.assertTrue(result["stop"][0])
        self.assertEqual(receiver.status()["state"], "stopped")
        self.assertNotIn([b.SRX], chip.log)  # never entered RX after the stop request

    def test_slow_or_failing_store_does_not_stall_sampling(self):
        clock = FakeClock()
        levels = series([(600, -100), (175, -60), (338, -100), (175, -60), (5000, -100)])

        def rssi():
            index = min(int(clock.clock() * 1000), len(levels) - 1)
            return int((levels[index][1] + 74) * 2) & 0xFF

        release = threading.Event()

        def blocking_store(event):
            release.wait(5)
            raise RuntimeError("database is locked")

        receiver = self._receiver(FakeChip(rssi=rssi), [], clock, on_event=blocking_store)
        self.assertTrue(receiver.start()[0])
        self.assertTrue(self._wait(lambda: receiver.status()["pair_count"] == 1))
        release.set()
        self.assertTrue(receiver.stop()[0])
        status = receiver.status()
        self.assertEqual(status["burst_count"], 2)
        self.assertEqual(status["store_errors"], 3)

    def test_unstable_marcstate_does_not_trigger_recovery(self):
        values = iter([b.MARCSTATE_RX, b.MARCSTATE_IDLE, b.MARCSTATE_RX, b.MARCSTATE_RX])
        from keyrfmonitor.hardware.cc1101_rx import read_marcstate

        class Flaky(FakeChip):
            def xfer2(self, payload):
                if payload == [b.READ | b.BURST | b.MARCSTATE, 0]:
                    return [0x0F, next(values)]
                return super().xfer2(payload)

        self.assertEqual(read_marcstate(bus_for(Flaky())), b.MARCSTATE_RX)

    def test_overflow_recovery_waits_for_idle_before_flush(self):
        chip = FakeChip()
        receiver = self._receiver(chip, [], FakeClock())
        chip.state = b.MARCSTATE_RXFIFO_OVERFLOW
        receiver._recover_rx(bus_for(chip), b.MARCSTATE_RXFIFO_OVERFLOW)
        strobes = [entry[0] for entry in chip.log if len(entry) == 1]
        self.assertEqual(strobes, [b.SIDLE, b.SFRX, b.SRX])
        self.assertLess(chip.log.index([b.SIDLE]), chip.log.index([b.SFRX]))
        idle_reads = [i for i, e in enumerate(chip.log)
                      if e == [b.READ | b.BURST | b.MARCSTATE, 0]]
        self.assertTrue(any(chip.log.index([b.SIDLE]) < i < chip.log.index([b.SFRX])
                            for i in idle_reads))
        self.assertEqual(chip.state, b.MARCSTATE_RX)

    def test_store_accepts_cc1101_events(self):
        store = EventStore(self.dir / "events.sqlite3")
        _, events = DetectorTests.run_series(None, series(
            [(600, -100), (175, -60), (338, -100), (175, -60), (300, -100)]))
        for event in events:
            store.insert(event)
        rows = store.recent()
        self.assertEqual({row["source"] for row in rows}, {"cc1101"})
        self.assertEqual(sum(row["kind"] == "burst" for row in rows), 2)
        store.close()


class CliTests(unittest.TestCase):
    def test_listen_validates_duration(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(__main__.main(["cc1101", "listen", "--seconds", "0"]), 2)
        self.assertIn("--seconds", err.getvalue())

    def test_listen_without_hardware_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("keyrfmonitor.hardware.cc1101_bus.SafeCC1101Bus",
                      side_effect=OSError("/dev/spidev1.0 fehlt")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = __main__.main(["cc1101", "listen", "--seconds", "1",
                                  "--database", str(Path(directory) / "e.sqlite3")])
        self.assertEqual(code, 2)
        self.assertIn("spidev1.0", err.getvalue())
        self.assertIn("erneut starten", err.getvalue())


class HttpTests(unittest.TestCase):
    setUp = _http_base.SummaryHTTPTests.setUp
    tearDown = _http_base.SummaryHTTPTests.tearDown

    def test_status_and_csrf_protected_start(self):
        with urlopen(self.base + "/api/cc1101/rx/status", timeout=5) as response:
            status = json.loads(response.read())
        self.assertEqual(status["state"], "stopped")
        self.assertFalse(status["tx_implemented"])
        request = Request(self.base + "/api/cc1101/rx/start", method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_start_without_hardware_returns_409(self):
        with urlopen(self.base + "/", timeout=5) as response:
            html = response.read().decode()
        token = html.split('name="rf-token" content="')[1].split('"')[0]
        with patch("keyrfmonitor.hardware.cc1101_bus.SafeCC1101Bus",
                   side_effect=OSError("/dev/spidev1.0 fehlt")):
            request = Request(self.base + "/api/cc1101/rx/start", method="POST",
                              headers={"X-RFMonitor-Token": token})
            with self.assertRaises(HTTPError) as ctx:
                urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 409)
        self.assertIn("spidev1.0", json.loads(ctx.exception.read())["message"])


if __name__ == "__main__":
    unittest.main()
