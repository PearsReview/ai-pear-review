"""python/player.py's WAV decoding: every encoding a speech service may send comes
out as the 16-bit PCM the output stream plays."""

from __future__ import annotations

import struct
import sys
from array import array
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from player import to_pcm16


def wav(
    samples: bytes, *, bits: int, fmt: int = 1, channels: int = 1, rate: int = 16000, size: int | None = None
) -> bytes:
    block = channels * bits // 8
    fmt_body = struct.pack("<HHIIHH", fmt, channels, rate, rate * block, block, bits)
    data_size = len(samples) if size is None else size
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt_body))
        + fmt_body
        + b"data"
        + struct.pack("<I", data_size)
        + samples
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def ints(pcm: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


def test_16_bit_passes_through():
    samples = struct.pack("<3h", 0, 1000, -1000)
    assert to_pcm16(wav(samples, bits=16)) == (16000, 1, samples)


def test_8_bit_is_unsigned_and_widened():
    _, _, pcm = to_pcm16(wav(bytes([128, 255, 0]), bits=8))
    assert ints(pcm) == [0, 127 << 8, -128 << 8]


def test_24_and_32_bit_keep_their_top_16_bits():
    s24 = b"".join(v.to_bytes(3, "little", signed=True) for v in (0, 1000 << 8, -(1000 << 8)))
    assert ints(to_pcm16(wav(s24, bits=24))[2]) == [0, 1000, -1000]
    s32 = struct.pack("<3i", 0, 1000 << 16, -(1000 << 16))
    assert ints(to_pcm16(wav(s32, bits=32))[2]) == [0, 1000, -1000]


def test_float_is_scaled_and_clipped():
    floats = array("f", [0.0, 0.5, -1.0, 2.0]).tobytes()
    assert ints(to_pcm16(wav(floats, bits=32, fmt=3))[2]) == [0, 16383, -32767, 32767]


def test_a_streamed_data_size_runs_to_the_end():
    samples = struct.pack("<2h", 5, 6)
    assert to_pcm16(wav(samples, bits=16, size=0xFFFFFFFF))[2] == samples


def test_stereo_is_kept():
    samples = struct.pack("<4h", 1, 2, 3, 4)
    assert to_pcm16(wav(samples, bits=16, channels=2))[:2] == (16000, 2)


@pytest.mark.parametrize("data", [b"", b"ID3 not a wav", wav(b"\x00" * 4, bits=16, fmt=85)])
def test_anything_else_is_refused_with_a_reason(data):
    with pytest.raises(ValueError):
        to_pcm16(data)
