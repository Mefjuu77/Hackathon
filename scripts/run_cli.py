"""Run one conversation from the console, without the UI.

Usage: uv run python scripts/run_cli.py --nps 3 --cx 9 --scenario S1
"""

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import content  # noqa: E402
from app.bridge import Bridge  # noqa: E402
from app.config import load_settings  # noqa: E402

LABELS = {"customer": "👤 Klient", "helpdesk": "🎧 Konsultant"}


async def print_event(event: dict) -> None:
    match event["type"]:
        case "run_started":
            print(f"▶ {event['run_id']}: {event['scenario_title']} (NPS {event['nps_level']}, CX {event['cx_level']})")
        case "transcript":
            print(f"[{event['t']:6.1f}s] {LABELS[event['role']]}: {event['text']}")
        case "transcript_correction":
            print(f"         ✎ {LABELS[event['role']]} (korekta #{event['utterance_id']}): {event['text']}")
        case "interruption":
            print(f"         ⤳ przerwano: {LABELS[event['role']]}")
        case "mood":
            print(f"         nastrój: {event['score']} ({event['reason']})")
        case "error":
            print(f"✖ Błąd: {event['message']}")
        case "run_ended":
            print(f"■ Koniec: {event['end_reason']}")
            print(json.dumps(event["summary"], ensure_ascii=False, indent=2))


async def ignore_audio(_: bytes) -> None:
    pass


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    parser = argparse.ArgumentParser()
    parser.add_argument("--nps", type=int, required=True, choices=content.LEVELS)
    parser.add_argument("--cx", type=int, required=True, choices=content.LEVELS)
    parser.add_argument("--scenario", default="random", choices=[*content.SCENARIOS, "random"])
    args = parser.parse_args()

    settings = load_settings()
    try:
        settings.require_elevenlabs()
    except RuntimeError as error:
        print(error)
        return 2
    bridge = Bridge(settings, args.nps, args.cx, content.pick_scenario(args.scenario), print_event, ignore_audio)
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, bridge.stop)
    except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
        pass
    task = asyncio.create_task(bridge.run())
    try:
        summary = await asyncio.shield(task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        bridge.stop()
        summary = await task
    print(f"Pliki: {settings.runs_dir / summary['run_id']}")
    return 0 if summary["end_reason"] != "error" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
