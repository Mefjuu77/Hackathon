"""Replay of a saved run: the same UI events and stereo audio, paced like the live run (SPEC §6.8)."""

import asyncio
import json
import time
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.audio import TICK_SECONDS

STEREO_FRAME_SAMPLES = 1600  # 100 ms at 16 kHz


def load_ui_events(run_dir: Path) -> list[tuple[float, dict]]:
    """UI events from events.jsonl as (t, event), in the order they were sent."""
    events = []
    with (run_dir / "events.jsonl").open(encoding="utf-8") as lines:
        for line in lines:
            if not line.strip():
                continue
            record = json.loads(line)
            if record["source"] == "ui":
                events.append((record["t"], record["event"]))
    return events


async def replay_run(
    run_dir: Path,
    send_json: Callable[[dict], Awaitable[None]],
    send_bytes: Callable[[bytes], Awaitable[None]],
    speed: float = 1.0,
) -> None:
    """Send the run's UI events at their original offsets and the audio in 100 ms frames.

    Audio frame k was recorded k ticks after run_started, so both streams are timed
    from the first UI event.
    """
    events = load_ui_events(run_dir)
    if not events:
        return
    t0 = events[0][0]
    start = time.monotonic()

    async def wait_until(offset: float) -> None:
        await asyncio.sleep(max(0.0, start + offset / speed - time.monotonic()))

    with wave.open(str(run_dir / "audio.wav"), "rb") as wav:
        frame_index = 0
        frame = wav.readframes(STEREO_FRAME_SAMPLES)
        for t, event in events:
            offset = t - t0
            while frame and frame_index * TICK_SECONDS < offset:
                await wait_until(frame_index * TICK_SECONDS)
                await send_bytes(frame)
                frame_index += 1
                frame = wav.readframes(STEREO_FRAME_SAMPLES)
            if event["type"] == "run_ended":
                # Live, run_ended follows the last audio frame; keep that order.
                while frame:
                    await wait_until(frame_index * TICK_SECONDS)
                    await send_bytes(frame)
                    frame_index += 1
                    frame = wav.readframes(STEREO_FRAME_SAMPLES)
            await wait_until(offset)
            await send_json(event)
