"""Standalone CLI: offline capture inspection or local dashboard."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .dsp import PassiveDetector, RFConfig
from .server import serve


def analyze(path: Path, config: RFConfig) -> int:
    if not path.is_file():
        print(f"Datei nicht gefunden: {path}", file=sys.stderr)
        return 2
    detector = PassiveDetector(config)
    print("=== KeyRFMonitor: passive Offline-Analyse ===")
    print("Keine Speicherung oder Ausgabe von Schlüsselbits / IQ-Rohdaten.")
    chunk_size = 2 * config.samples_per_frame
    with path.open("rb") as stream:
        while block := stream.read(chunk_size):
            if len(block) != chunk_size:
                break
            for event in detector.process(block):
                if event["kind"] == "burst":
                    print(f"Burst  {event['start_s']:7.3f} s | "
                          f"{event['duration_ms']:5.0f} ms | "
                          f"+{event['peak_rise_db']:.1f} dB | "
                          f"SNR {event['peak_snr_db']:.1f} dB")
                elif event["kind"] == "pair":
                    print(f"  → Paar erkannt: {event['start_gap_ms']:.0f} ms Startabstand")
    detector.finish()
    s = detector.status()
    print(f"Ergebnis: {s['burst_count']} Bursts, {s['pair_count']} Paare; "
          f"analysiert {s['processed_seconds']:.2f} s")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="keyrfmonitor", description="Eigenständige passive RF-Diagnose")
    sub = parser.add_subparsers(dest="cmd", required=True)
    api = sub.add_parser("serve", help="Lokales Dashboard auf 127.0.0.1")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--lan-host", choices=["192.168.178.63"], default=None,
                     help="Zusätzlicher Listener, nur auf Pi-LAN-IP 192.168.178.63")
    api.add_argument("--port", type=int, default=8765)
    api.add_argument("--database", default=None)
    scan = sub.add_parser("scan", help="ADS-B befristet pausieren, IQ-/Bitaufnahmen standardmäßig erfassen und ADS-B wiederherstellen")
    scan.add_argument("--seconds", type=int, default=60, help="Scan-Dauer, 20 bis 300 Sekunden (Standard 60)")
    iq_choice = scan.add_mutually_exclusive_group()
    iq_choice.add_argument("--iq", dest="iq", action="store_true", default=True,
                           help="Echte CU8-I/Q- und FSK-Bitaufnahmen einschalten (Standard)")
    iq_choice.add_argument("--no-iq", dest="iq", action="store_false",
                           help="Nur Burst-Metadaten erfassen, keine IQ-/Bitdateien")
    offline = sub.add_parser("analyze", help="Lokale RTL-SDR CU8-Datei analysieren")
    offline.add_argument("file", type=Path)
    cc = sub.add_parser("cc1101", help="CC1101-Pinbelegung und nicht sendende SPI-Pruefung")
    cc_sub = cc.add_subparsers(dest="cc_action", required=True)
    cc_sub.add_parser("simulate", help="Neun synthetische CC1101-Metadatenereignisse ausgeben (kein SPI)")
    cc_sub.add_parser("wiring", help="Pinbelegung anzeigen (ohne Hardwarezugriff)")
    cc_probe = cc_sub.add_parser("probe", help="Nur PARTNUM/VERSION per SPI auslesen")
    cc_probe.add_argument("--bus", type=int, default=0)
    cc_probe.add_argument("--device", type=int, default=0)
    args = parser.parse_args(argv)
    if args.cmd == "cc1101":
        from .hardware.cc1101 import CC1101ProbeError, probe, wiring_lines
        if args.cc_action == "simulate":
            from .hardware.cc1101_sim import simulated_events
            for event in simulated_events():
                print(json.dumps(event, ensure_ascii=False))
            return 0
        if args.cc_action == "wiring":
            print("=== CC1101 / Raspberry Pi 5, SPI0 CE0 ===")
            for item in wiring_lines():
                print("-", item)
            print("CC1101-VCC nur 3,3 V; GDO-Pins sind derzeit unbenutzt.")
            return 0
        try:
            result = probe(bus=args.bus, device=args.device)
        except (CC1101ProbeError, ValueError) as exc:
            print(f"CC1101: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["cc1101_plausible"] else 1
    if args.cmd == "scan":
        from .adsb_session import ScanError, run_scan
        try:
            return run_scan(seconds=args.seconds, capture_iq=args.iq)
        except ScanError as exc:
            print(f"Scan fehlgeschlagen: {exc}", file=sys.stderr)
            return 2
    if args.cmd == "serve":
        serve(args.host, args.port, args.database, lan_host=args.lan_host)
        return 0
    return analyze(args.file, RFConfig())


if __name__ == "__main__":
    raise SystemExit(main())
