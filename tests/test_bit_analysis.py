"""FSK decoding alternatives are deterministic, conservative and read-only."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from keyrfmonitor.bit_analysis import (
    ALLOWED_BAUD, interpret_symbols, validate_options,
)
from keyrfmonitor.dsp import RFConfig
from keyrfmonitor.iq_trace import TraceArchive
import math
import numpy as np

def synthetic_cu8(bits: str, cfg: RFConfig) -> bytes:
    samples_per_symbol = round(cfg.sample_rate / 2000)
    frequencies = np.repeat(
        np.array([cfg.high_offset_hz if bit == "1" else cfg.low_offset_hz
                  for bit in bits], dtype=np.float64),
        samples_per_symbol)
    theta = 2 * math.pi * np.cumsum(frequencies) / cfg.sample_rate
    iq = np.empty((len(theta), 2), dtype=np.uint8)
    iq[:, 0] = np.clip(127.5 + 55 * np.cos(theta), 0, 255).astype(np.uint8)
    iq[:, 1] = np.clip(127.5 + 55 * np.sin(theta), 0, 255).astype(np.uint8)
    return iq.tobytes()



class SymbolInterpretationTests(unittest.TestCase):
    def setUp(self):
        self.rates = {"bits": "01100110", "confidence": [0.9] * 8,
                      "timing_offset_samples": 23, "samples_per_symbol": 512,
                      "estimated_low_hz": -20120.0,
                      "estimated_high_hz": 11350.0}

    def test_nrz_preserves_all_symbols_and_does_not_claim_verification(self):
        result = interpret_symbols(self.rates)
        self.assertEqual(result["bits"], "01100110")
        self.assertEqual(result["hex_preview"], "66")
        self.assertEqual(result["known_full_bytes"], 1)
        self.assertFalse(result["symbol_clock_verified"])
        self.assertFalse(result["frame_verified"])
        self.assertFalse(result["security_protocol_verified"])
        self.assertEqual(result["timing_offset_samples"], 23)
        self.assertEqual(result["estimated_low_hz"], -20120)

    def test_inversion_changes_only_known_bits(self):
        source = dict(self.rates, bits="101?0001")
        direct = interpret_symbols(source)
        inverted = interpret_symbols(source, polarity="inverted")
        self.assertEqual(direct["bits"], "101?0001")
        self.assertEqual(inverted["bits"], "010?1110")
        self.assertEqual(inverted["hex_preview"], "??")
        self.assertEqual(inverted["known_full_bytes"], 0)

    def test_manchester_both_phases_and_inverted_polarity(self):
        normal = interpret_symbols(self.rates, coding="manchester")
        self.assertEqual(normal["bits"], "0101")
        self.assertEqual(normal["trailing_bits"], "0101")
        self.assertEqual(normal["invalid_manchester_pairs"], 0)
        inverted = interpret_symbols(
            self.rates, coding="manchester", polarity="inverted")
        self.assertEqual(inverted["bits"], "1010")
        shifted = interpret_symbols(
            self.rates, coding="manchester", alignment=1)
        self.assertEqual(shifted["bits"], "???")
        self.assertEqual(shifted["invalid_manchester_pairs"], 3)

    def test_unknown_and_invalid_manchester_pairs_remain_uncertain(self):
        source = dict(self.rates, bits="001101??")
        result = interpret_symbols(source, coding="manchester")
        self.assertEqual(result["bits"], "??0?")
        self.assertEqual(result["invalid_manchester_pairs"], 3)
        self.assertEqual(result["uncertain_bits"], 3)
        self.assertEqual(result["uncertain_percent"], 75.0)
        self.assertEqual(result["confidence_mean"], 0.225)

    def test_alternating_levels_are_not_identified_as_valid_frame(self):
        source = {"bits": "00001010101010101110",
                  "confidence": [1.0] * 20}
        result = interpret_symbols(source)
        self.assertIsNotNone(result["alternating_run"])
        self.assertGreaterEqual(result["alternating_run"]["length_bits"], 10)
        self.assertEqual(
            result["alternating_run"]["interpretation"],
            "alternating_levels_only")
        self.assertFalse(result["frame_verified"])

    def test_invalid_coding_params_and_corrupt_metadata_rejected(self):
        for baud in (0, 999, 2000.0, True, "2000", 50000):
            with self.assertRaises(ValueError):
                validate_options(baud, "nrz", "normal", 0)
        for coding, polarity, alignment in (
            ("unknown", "normal", 0),
            ("nrz", "reverse", 0),
            ("nrz", "normal", 1),
            ("manchester", "normal", 2),
        ):
            with self.assertRaises(ValueError):
                validate_options(2000, coding, polarity, alignment)
        with self.assertRaises(ValueError):
            interpret_symbols({"bits": "013", "confidence": [1] * 3})
        with self.assertRaises(ValueError):
            interpret_symbols({"bits": "01", "confidence": [1]})
        with self.assertRaises(ValueError):
            interpret_symbols({"bits": "0", "confidence": [float("nan")]})


class ArchiveReDecodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = TraceArchive(Path(self.temp.name) / "iq-traces")
        self.config = RFConfig()
        self.cu8 = synthetic_cu8("0100110101110001" * 20, self.config)
        event = {
            "kind": "burst", "at": datetime.now(timezone.utc).isoformat(),
            "start_s": 0, "end_s": len(self.cu8) / (2 * self.config.sample_rate),
            "duration_ms": len(self.cu8) / (2 * self.config.sample_rate) * 1000,
            "peak_rise_db": 19.0, "peak_snr_db": 38.0,
        }
        self.item = self.archive.save(self.cu8, event, self.config, 0)

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_redecode_has_no_side_effects(self):
        before = set(p.name for p in self.archive.path.iterdir())
        identifier = self.item["id"]
        first = self.archive.decode_hypothesis(identifier)
        second = self.archive.decode_hypothesis(
            identifier, coding="manchester", polarity="inverted")
        third = self.archive.decode_hypothesis(
            identifier, baud=4000, coding="nrz")
        self.assertEqual(first["raw_symbol_count"], first["bit_count"])
        self.assertGreater(first["bit_count"], 200)
        self.assertEqual(second["coding"], "manchester")
        self.assertEqual(second["polarity"], "inverted")
        self.assertNotEqual(first["bit_count"], second["bit_count"])
        self.assertNotEqual(first["bit_count"], third["bit_count"])
        self.assertEqual(third["baud"], 4000)
        self.assertFalse(first["security_protocol_verified"])
        self.assertEqual(
            set(p.name for p in self.archive.path.iterdir()), before)
        self.assertEqual(
            (self.archive.path / (identifier + ".cu8")).read_bytes(), self.cu8)

    def test_bad_trace_id_and_unsupported_data_are_rejected(self):
        with self.assertRaises(ValueError):
            self.archive.decode_hypothesis("../secrets")
        self.assertIsNone(self.archive.decode_hypothesis("0" * 32))
        with self.assertRaises(ValueError):
            self.archive.decode_hypothesis(self.item["id"], baud=100)
        with self.assertRaises(ValueError):
            self.archive.decode_hypothesis(
                self.item["id"], coding="nrz", alignment=1)
        # The metadata must exactly match the raw file size.
        with (self.archive.path / (self.item["id"] + ".cu8")).open("ab") as f:
            f.write(b"\x00\x00")
        with self.assertRaises(ValueError):
            self.archive.decode_hypothesis(self.item["id"])


if __name__ == "__main__":
    unittest.main()
