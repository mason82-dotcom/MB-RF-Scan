"""Binary-exact private archive export, without RF hardware or code rewriting."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from keyrfmonitor.dsp import RFConfig
from keyrfmonitor.iq_trace import (
    ArchiveExportError, MAX_EXPORT_METADATA_BYTES, TraceArchive,
)


class IQExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = TraceArchive(Path(self.temp.name) / "iq-traces")
        self.config = RFConfig()
        self.raw = b"\x70\x90" * (self.config.samples_per_frame * 3)
        self.event = {
            "kind": "burst",
            "at": datetime.now(timezone.utc).isoformat(),
            "start_s": .001, "end_s": .019,
            "duration_ms": 18.0, "peak_snr_db": 26,
            "peak_rise_db": 19,
        }
        self.saved = self.archive.save(self.raw, self.event, self.config, 0)
        self.assertIsNotNone(self.saved)
        self.identity = self.saved["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_both_exports_are_exact_and_archive_is_unchanged(self):
        before = {f.name: (f.read_bytes(), f.stat().st_mode & 0o777)
                  for f in self.archive.path.iterdir()}
        original_metadata = before[self.identity + ".json"][0]
        self.assertEqual(self.archive.export_bytes(self.identity, "cu8"), self.raw)
        self.assertEqual(self.archive.export_bytes(self.identity, "json"), original_metadata)
        self.assertEqual(json.loads(original_metadata)["decoder"],
                         self.saved["decoder"])
        after = {f.name: (f.read_bytes(), f.stat().st_mode & 0o777)
                 for f in self.archive.path.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(after[self.identity + ".cu8"][1], 0o600)
        self.assertEqual(after[self.identity + ".json"][1], 0o600)

    def test_missing_and_invalid_ids_or_formats(self):
        self.assertIsNone(self.archive.export_bytes("f" * 32, "cu8"))
        self.assertIsNone(self.archive.export_bytes("f" * 32, "json"))
        for bad in ("../passwd", "", "a" * 31, "A" * 32, "a" * 33, self.identity + "/x"):
            with self.subTest(identity=bad), self.assertRaises(ValueError):
                self.archive.export_bytes(bad, "cu8")
        for fmt in ("", "Cu8", "zip", "../json", "json?x=y", None):
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                self.archive.export_bytes(self.identity, fmt)

    def test_symlink_replacement_cannot_export_other_files(self):
        cu8 = self.archive.path / (self.identity + ".cu8")
        backup = self.archive.path / "source.cu8"
        cu8.rename(backup)
        cu8.symlink_to(backup)
        with self.assertRaises(ArchiveExportError):
            self.archive.export_bytes(self.identity, "cu8")
        # Even when CU8 is broken, the original JSON metadata remains exportable.
        self.assertEqual(
            self.archive.export_bytes(self.identity, "json"),
            (self.archive.path / (self.identity + ".json")).read_bytes(),
        )
        meta = self.archive.path / (self.identity + ".json")
        backup_meta = self.archive.path / "source.json"
        meta.rename(backup_meta)
        meta.symlink_to(backup_meta)
        with self.assertRaises(ArchiveExportError):
            self.archive.export_bytes(self.identity, "json")

    def test_cu8_integrity_uses_metadata_sample_count(self):
        raw_file = self.archive.path / (self.identity + ".cu8")
        raw_file.write_bytes(self.raw[:-2])
        with self.assertRaises(ArchiveExportError):
            self.archive.export_bytes(self.identity, "cu8")
        self.assertIsNotNone(self.archive.export_bytes(self.identity, "json"))

    def test_oversized_iq_and_metadata_are_rejected(self):
        raw_file = self.archive.path / (self.identity + ".cu8")
        with raw_file.open("wb") as output:
            output.truncate(self.config.sample_rate * 2 + 2)
        with self.assertRaises(ArchiveExportError):
            self.archive.export_bytes(self.identity, "cu8")
        metadata_file = self.archive.path / (self.identity + ".json")
        with metadata_file.open("wb") as output:
            output.truncate(MAX_EXPORT_METADATA_BYTES + 1)
        with self.assertRaises(ArchiveExportError):
            self.archive.export_bytes(self.identity, "json")

    def test_invalid_metadata_is_rejected_without_filesystem_changes(self):
        metadata_file = self.archive.path / (self.identity + ".json")
        baseline = json.loads(metadata_file.read_text(encoding="utf-8"))
        variants = [
            dict(baseline, id="0" * 32),
            dict(baseline, sample_count=-3),
            dict(baseline, sample_rate=999999),
            dict(baseline, iq_format="unknown"),
            dict(baseline, source="unknown"),
            dict(baseline, bytes_saved=False),
            dict(baseline, iq_bytes=12),
        ]
        for index, record in enumerate(variants):
            with self.subTest(variant=index):
                metadata_file.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaises(ArchiveExportError):
                    self.archive.export_bytes(self.identity, "cu8")
        metadata_file.write_bytes(b"{this-is-not-json")
        with self.assertRaises(ArchiveExportError):
            self.archive.export_bytes(self.identity, "json")

    def test_fifo_is_rejected_without_blocking_when_supported(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO not supported")
        raw_file = self.archive.path / (self.identity + ".cu8")
        raw_file.unlink()
        os.mkfifo(raw_file)
        with self.assertRaises(ArchiveExportError):
            self.archive.export_bytes(self.identity, "cu8")


if __name__ == "__main__":
    unittest.main()
