"""Files of a single run: audio.wav, events.jsonl, transcript.json, summary.json."""

import json
import re
import time
import wave
from datetime import datetime
from pathlib import Path

from app.audio import SAMPLE_RATE, SAMPLE_WIDTH

RUN_FILES = ("audio.wav", "events.jsonl", "transcript.json", "summary.json")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
REPLAYABLE_FILES = ("summary.json", "audio.wav", "events.jsonl")


def _started_at(run_id: str) -> str | None:
    try:
        return datetime.strptime(run_id[:15], "%Y%m%d-%H%M%S").isoformat()
    except ValueError:
        return None


def list_runs(runs_dir: Path, scenario_titles: dict[str, str]) -> list[dict]:
    """Replayable runs (summary.json, audio.wav and events.jsonl present), newest first."""
    if not runs_dir.is_dir():
        return []
    runs = []
    for path in runs_dir.iterdir():
        if not RUN_ID_PATTERN.match(path.name) or not all((path / name).is_file() for name in REPLAYABLE_FILES):
            continue
        try:
            summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
            mood = summary["mood"]
            turns = summary["turns"]
            scenario_id = summary["scenario_id"]
            runs.append({
                "run_id": path.name,
                "scenario_id": scenario_id,
                "scenario_title": scenario_titles.get(scenario_id),
                "nps_level": summary["nps_level"],
                "cx_level": summary["cx_level"],
                "end_reason": summary["end_reason"],
                "duration_s": summary["duration_s"],
                "turns": {"customer": turns["customer"], "helpdesk": turns["helpdesk"]},
                "mood": {"start": mood["start"], "end": mood["end"], "delta": mood["delta"]},
                "started_at": _started_at(path.name),
            })
        except (OSError, ValueError, KeyError, TypeError):
            continue  # unreadable or malformed summary: not replayable
    return sorted(runs, key=lambda run: run["run_id"], reverse=True)


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
