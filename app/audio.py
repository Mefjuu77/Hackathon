"""PCM16 LE mono 16 kHz helpers: per-agent output buffer and stereo mixer."""

import sys
from array import array

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
TICK_SECONDS = 0.1
FRAME_BYTES = int(SAMPLE_RATE * TICK_SECONDS) * SAMPLE_WIDTH  # 3200 bytes = 100 ms


class OutputBuffer:
    """Audio produced by an agent, waiting to be forwarded in real-time frames."""

    def __init__(self) -> None:
        self._data = bytearray()

    def append(self, pcm: bytes) -> None:
        self._data += pcm

    def take(self, n: int = FRAME_BYTES) -> bytes:
        """Return exactly `n` bytes, padding with silence when the buffer runs short."""
        chunk = bytes(self._data[:n])
        del self._data[:n]
        return chunk + bytes(n - len(chunk))

    def clear(self) -> None:
        self._data.clear()

    @property
    def is_empty(self) -> bool:
        return not self._data

    def __len__(self) -> int:
        return len(self._data)


def _samples(pcm: bytes) -> array:
    samples = array("h", pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def mix_stereo(left: bytes, right: bytes) -> bytes:
    """Interleave two equal-length mono frames into one stereo frame (L, R, L, R, ...)."""
    if len(left) != len(right):
        raise ValueError("left and right frames must have the same length")
    out = array("h", bytes(len(left) * 2))
    out[0::2] = _samples(left)
    out[1::2] = _samples(right)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()
