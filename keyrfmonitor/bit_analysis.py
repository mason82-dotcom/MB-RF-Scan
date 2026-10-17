"""Read-only FSK symbol-to-bit hypotheses. No protocol or key verification.

Manchester and raw NRZ display conventions are *alternatives*, not a claim
that a real proprietary RF transmitter uses either. Unknowns stay unknown.
"""
from __future__ import annotations

import re

ALLOWED_BAUD = (600, 1000, 1200, 1600, 2000, 2400, 3200, 4000, 4800, 9600)
ALLOWED_CODINGS = ("nrz", "manchester")
ALLOWED_POLARITY = ("normal", "inverted")


def validate_options(baud: int, coding: str, polarity: str,
                     alignment: int) -> None:
    if type(baud) is not int or baud not in ALLOWED_BAUD:
        raise ValueError("Choose a supported FSK rate")
    if coding not in ALLOWED_CODINGS:
        raise ValueError("Unknown bit coding")
    if polarity not in ALLOWED_POLARITY:
        raise ValueError("Unknown bit polarity")
    if type(alignment) is not int or alignment not in (0, 1):
        raise ValueError("Manchester alignment must be zero or one")
    if coding == "nrz" and alignment != 0:
        raise ValueError("NRZ does not have Manchester alignment")


def _flip(bit: str) -> str:
    return {"0": "1", "1": "0", "?": "?"}[bit]


def _hex_preview(bits: str) -> tuple[str, int, int]:
    """Use ?? for uncertain whole bytes; never fabricate unknown bits."""
    values = []
    valid = 0
    count = len(bits) // 8
    for index in range(count):
        block = bits[index * 8:(index + 1) * 8]
        if "?" in block:
            values.append("??")
        else:
            values.append(f"{int(block, 2):02X}")
            valid += 1
    return " ".join(values), valid, count


def _longest_alternation(bits: str) -> dict | None:
    best_start, best_length = 0, 0
    run_start, run_length, last = 0, 0, ""
    for index, bit in enumerate(bits):
        if bit == "?":
            run_length, last = 0, ""
            continue
        if last and bit != last:
            run_length += 1
        else:
            run_start, run_length = index, 1
        if run_length > best_length:
            best_start, best_length = run_start, run_length
        last = bit
    if best_length < 8:
        return None
    return {"start_bit": best_start, "length_bits": best_length,
            "pattern": bits[best_start:best_start + min(best_length, 16)],
            "interpretation": "alternating_levels_only"}


def interpret_symbols(demodulated: dict, *, baud: int = 2000,
                      coding: str = "nrz", polarity: str = "normal",
                      alignment: int = 0) -> dict:
    """Preserve uncertainty and expose a selectable, unverified bit view."""
    validate_options(baud, coding, polarity, alignment)
    source = demodulated.get("bits", "")
    confidence = demodulated.get("confidence", [])
    if not isinstance(source, str) or re.fullmatch("[01?]*", source) is None:
        raise ValueError("Invalid symbol sequence")
    if len(source) != len(confidence):
        raise ValueError("Confidence count does not match symbols")
    if any(type(score) not in (int, float) or not 0 <= score <= 1
           for score in confidence):
        raise ValueError("Invalid confidence data")
    inverted = polarity == "inverted"
    decoded = []
    scores = []
    invalid_pairs = 0
    if coding == "nrz":
        for bit, score in zip(source, confidence):
            decoded.append(_flip(bit) if inverted else bit)
            scores.append(round(float(score), 2) if bit != "?" else 0.0)
    else:
        for index in range(alignment, len(source) - 1, 2):
            pair = source[index:index + 2]
            if pair not in ("01", "10"):
                invalid_pairs += 1
                decoded.append("?")
                scores.append(0.0)
                continue
            bit = "0" if pair == "01" else "1"
            decoded.append(_flip(bit) if inverted else bit)
            scores.append(round(float(min(confidence[index:index + 2])), 2))
    bits = "".join(decoded)
    hex_values, known, whole = _hex_preview(bits)
    uncertain = bits.count("?")
    return {
        "baud": baud,
        "coding": coding,
        "polarity": polarity,
        "alignment": alignment,
        "raw_symbols": source,
        "raw_symbol_count": len(source),
        "raw_unknown_count": source.count("?"),
        "bits": bits,
        "bit_confidence": scores,
        "bit_count": len(bits),
        "uncertain_bits": uncertain,
        "uncertain_percent": round(100 * uncertain / len(bits), 1) if bits else 0.0,
        "confidence_mean": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "invalid_manchester_pairs": invalid_pairs,
        "hex_preview": hex_values,
        "known_full_bytes": known,
        "total_full_bytes": whole,
        "trailing_bits": bits[whole * 8:],
        "alternating_run": _longest_alternation(bits),
        "timing_offset_samples": demodulated.get("timing_offset_samples", 0),
        "samples_per_symbol": demodulated.get("samples_per_symbol"),
        "estimated_low_hz": demodulated.get("estimated_low_hz"),
        "estimated_high_hz": demodulated.get("estimated_high_hz"),
        "method": "phase_discriminator_fsk",
        "symbol_clock_verified": False,
        "frame_verified": False,
        "security_protocol_verified": False,
        "interpretation": "hypothesis_only",
    }
