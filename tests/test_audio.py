from array import array

import pytest

from app.audio import FRAME_BYTES, OutputBuffer, mix_stereo


def test_frame_is_100ms_of_16khz_pcm16():
    assert FRAME_BYTES == 3200


def test_take_returns_full_frame_and_pads_with_silence():
    buffer = OutputBuffer()
    buffer.append(b"\x01\x02" * 1000)  # 2000 bytes
    frame = buffer.take(FRAME_BYTES)
    assert len(frame) == FRAME_BYTES
    assert frame[:2000] == b"\x01\x02" * 1000
    assert frame[2000:] == bytes(1200)
    assert buffer.is_empty


def test_take_keeps_remainder_in_order():
    buffer = OutputBuffer()
    buffer.append(bytes(range(256)) * 20)  # 5120 bytes
    first = buffer.take(FRAME_BYTES)
    assert len(buffer) == 5120 - FRAME_BYTES
    second = buffer.take(FRAME_BYTES)
    assert first + second[: 5120 - FRAME_BYTES] == bytes(range(256)) * 20


def test_empty_buffer_gives_silence_and_clear_drops_audio():
    buffer = OutputBuffer()
    assert buffer.take() == bytes(FRAME_BYTES)
    buffer.append(b"\x10" * 5000)
    buffer.clear()
    assert buffer.is_empty
    assert buffer.take() == bytes(FRAME_BYTES)


def test_mix_stereo_interleaves_left_and_right():
    left = array("h", [1, 2, 3]).tobytes()
    right = array("h", [-1, -2, -3]).tobytes()
    assert array("h", mix_stereo(left, right)).tolist() == [1, -1, 2, -2, 3, -3]


def test_mix_stereo_requires_equal_lengths():
    with pytest.raises(ValueError):
        mix_stereo(b"\x00\x00", b"")
