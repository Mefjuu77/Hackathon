"""Files of a single run: audio.wav, events.jsonl, transcript.json, summary.json."""

import json
import time
import wave
from datetime import datetime
from pathlib import Path

from app.audio import SAMPLE_RATE, SAMPLE_WIDTH

RUN_FILES = ("audio.wav", "events.jsonl", "transcript.json", "summary.json")


class RunStore:
    def __init__(self, runs_dir: Path, scenario_id: str, now: datetime | None = None) -> None:
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{stamp}_{scenario_id}"
        self.path = runs_dir / self.run_id
        suffix = 1
        while self.path.exists():
            suffix += 1
            self.run_id = f"{stamp}-{suffix}_{scenario_id}"
            self.path = runs_dir / self.run_id
        self.path.mkdir(parents=True)
        self._t0 = time.monotonic()
        self._events = (self.path / "events.jsonl").open("w", encoding="utf-8")
        self._wav = wave.open(str(self.path / "audio.wav"), "wb")
        self._wav.setnchannels(2)
        self._wav.setsampwidth(SAMPLE_WIDTH)
        self._wav.setframerate(SAMPLE_RATE)

    def elapsed(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    def log(self, source: str, event: dict) -> None:
        line = {"t": self.elapsed(), "source": source, "event": event}
        self._events.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._events.flush()

    def write_audio(self, stereo_frame: bytes) -> None:
        self._wav.writeframes(stereo_frame)

    def write_json(self, name: str, data) -> None:
        (self.path / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self) -> None:
        self._wav.close()
        self._events.close()
