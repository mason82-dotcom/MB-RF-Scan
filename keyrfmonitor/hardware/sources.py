"""Receiver inventory independent of hardware availability or GPIO probing."""
from __future__ import annotations


def available_sources() -> list[dict]:
    return [
        {
            "id": "rtl_sdr", "name": "RTL-SDR", "mode": "live_receive_only",
            "hardware_required": True, "implemented": True,
            "description": "RTL2832U: passiver 433-MHz-Monitor; ADS-B-Konflikt wird geprüft",
        },
        {
            "id": "cc1101_sim", "name": "CC1101-Laborsimulator",
            "mode": "metadata_simulation", "hardware_required": False,
            "implemented": True,
            "description": "Rein synthetische Burst-Ereignisse, keine SPI- oder HF-Nutzung",
        },
        {
            "id": "cc1101", "name": "CC1101 (Hardware)",
            "mode": "unavailable", "hardware_required": True,
            "implemented": False,
            "description": "SPI-Probe vorbereitet; echte Paket- und RX-Konfiguration noch nicht umgesetzt",
        },
    ]
