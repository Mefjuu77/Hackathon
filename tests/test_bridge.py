import json
import wave
from array import array
from dataclasses import replace

import pytest

from app import content
from app.bridge import Bridge, build_summary, clamp_score
from app.config import load_settings
from tests.fake_elevenlabs import CUSTOMER_SAMPLE, HELPDESK_SAMPLE, FakeAgent, FakeElevenLabs


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.audio: list[bytes] = []

    async def emit(self, event: dict) -> None:
        self.events.append(event)

    async def emit_audio(self, frame: bytes) -> None:
        self.audio.append(frame)

    def of_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["type"] == event_type]


async def run_bridge(tmp_path, customer: FakeAgent, helpdesk: FakeAgent, *, nps=3, cx=9, max_seconds=10.0, agent_ids=("cust", "help")):
    async with FakeElevenLabs({"cust": customer, "help": helpdesk}) as server:
        settings = replace(
            load_settings(),
            elevenlabs_api_key="test-key",
            customer_agent_id=agent_ids[0],
            helpdesk_agent_id=agent_ids[1],
            elevenlabs_api_base=server.api_base,
            max_conversation_seconds=max_seconds,
            runs_dir=tmp_path,
        )
        recorder = Recorder()
        bridge = Bridge(settings, nps, cx, content.SCENARIOS["S1"], recorder.emit, recorder.emit_audio)
        summary = await bridge.run()
        return summary, recorder, tmp_path / summary["run_id"]


def agents(customer_steps, helpdesk_steps, **helpdesk_kwargs):
    return FakeAgent(CUSTOMER_SAMPLE, customer_steps), FakeAgent(HELPDESK_SAMPLE, helpdesk_steps, **helpdesk_kwargs)


# --- E2: bridge --------------------------------------------------------------

async def test_basic_conversation(tmp_path):
    customer, helpdesk = agents(
        [("audio", 1000), ("text", "Dzień dobry, nie działa mi internet."), ("ping",), ("wait_audio", 500), ("sleep", 0.2), ("end_call",)],
        [("wait_audio", 1000), ("audio", 500), ("text", "Już sprawdzam.")],
    )
    summary, recorder, run_dir = await run_bridge(tmp_path, customer, helpdesk)

    # Init data reached both agents
    for agent, expected in ((customer, {"nps_level": "3", "customer_name": "Marek Nowak"}), (helpdesk, {"cx_level": "9", "agent_name": "Joanna"})):
        init = agent.received[0]
        assert init["type"] == "conversation_initiation_client_data"
        assert expected.items() <= init["dynamic_variables"].items()

    # Ping answered with the same event_id
    assert {"type": "pong", "event_id": 101} in customer.received

    # Frames of 3200 bytes; burst audio is paced to real time
    assert {len(f) for f in helpdesk.audio_frames} == {3200}
    voiced_times = [t for f, t in zip(helpdesk.audio_frames, helpdesk.audio_frame_times) if any(f)]
    assert len(voiced_times) == 10
    assert 0.7 <= voiced_times[-1] - voiced_times[0] <= 1.2

    # Silence keeps flowing to the customer while the help desk is quiet
    silent_before = [t for f, t in zip(customer.audio_frames, customer.audio_frame_times) if not any(f)]
    assert len(silent_before) >= 5
    gaps = [b - a for a, b in zip(silent_before, silent_before[1:5])]
    assert all(0.05 <= g <= 0.2 for g in gaps)

    assert summary["end_reason"] == "customer_end_call"
    assert summary["turns"] == {"customer": 1, "helpdesk": 1}
    assert customer.closed_by_client and helpdesk.closed_by_client

    # UI events
    assert recorder.of_type("run_started")[0]["customer_name"] == "Marek Nowak"
    assert [e["text"] for e in recorder.of_type("transcript")] == ["Dzień dobry, nie działa mi internet.", "Już sprawdzam."]
    assert {len(f) for f in recorder.audio} == {6400}
    assert recorder.events[-1]["type"] == "run_ended"


async def test_run_files(tmp_path):
    customer, helpdesk = agents(
        [("audio", 500), ("text", "Halo?"), ("wait_audio", 300), ("end_call",)],
        [("wait_audio", 500), ("audio", 300), ("text", "Słucham.")],
    )
    summary, recorder, run_dir = await run_bridge(tmp_path, customer, helpdesk)
    assert sorted(p.name for p in run_dir.iterdir()) == ["audio.wav", "events.jsonl", "summary.json", "transcript.json"]

    with wave.open(str(run_dir / "audio.wav")) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (2, 2, 16000)
        duration = wav.getnframes() / 16000
        samples = array("h", wav.readframes(wav.getnframes()))
    assert abs(duration - summary["duration_s"]) <= 0.25
    assert set(samples[0::2]) == {0, CUSTOMER_SAMPLE}
    assert set(samples[1::2]) == {0, HELPDESK_SAMPLE}

    lines = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {line["source"] for line in lines} >= {"customer", "helpdesk", "ui"}
    assert all({"t", "source", "event"} == set(line) for line in lines)
    assert "audio_base_64" not in (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert [line["event"]["type"] for line in lines if line["source"] == "ui"] == [e["type"] for e in recorder.events]

    transcript = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    assert [(u["role"], u["text"], u["interrupted"]) for u in transcript] == [("customer", "Halo?", False), ("helpdesk", "Słucham.", False)]
    assert json.loads((run_dir / "summary.json").read_text(encoding="utf-8")) == summary


async def test_loop_waits_for_both_sessions(tmp_path):
    # Customer audio arrives before the help desk is ready; it must be buffered, not lost.
    customer, helpdesk = agents(
        [("audio", 500), ("text", "Dzień dobry."), ("wait_audio", 100), ("end_call",)],
        [("wait_audio", 500), ("audio", 100)],
    )
    summary, recorder, _ = await run_bridge(tmp_path, customer, helpdesk)
    assert helpdesk.nonsilent_ms() == 500
    assert recorder.events[0]["type"] == "run_started"


async def test_interruption_flushes_buffer(tmp_path):
    customer, helpdesk = agents(
        [],
        [("audio", 3000), ("text", "Bardzo długa wypowiedź."), ("sleep", 0.5), ("interruption",), ("sleep", 1.0), ("close",)],
    )
    summary, recorder, run_dir = await run_bridge(tmp_path, customer, helpdesk)
    assert 300 <= customer.nonsilent_ms() <= 1000
    assert summary["interruptions"] == {"customer": 0, "helpdesk": 1}
    assert recorder.of_type("interruption") == [{"type": "interruption", "role": "helpdesk"}]
    transcript = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    assert transcript[0]["interrupted"] is True
    assert summary["end_reason"] == "helpdesk_closed"


async def test_correction_replaces_text(tmp_path):
    customer, helpdesk = agents(
        [("text", "Dzień dobry, mam problem z"), ("correction", "Dzień dobry, mam problem z", "Dzień dobry, mam"), ("end_call",)],
        [],
    )
    _, recorder, run_dir = await run_bridge(tmp_path, customer, helpdesk)
    assert recorder.of_type("transcript_correction") == [{"type": "transcript_correction", "utterance_id": 1, "role": "customer", "text": "Dzień dobry, mam"}]
    assert json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))[0]["text"] == "Dzień dobry, mam"


async def test_audio_format_mismatch_is_error(tmp_path):
    customer, helpdesk = agents([], [], audio_format="pcm_44100")
    summary, recorder, run_dir = await run_bridge(tmp_path, customer, helpdesk)
    assert summary["end_reason"] == "error"
    assert "pcm_44100" in summary["error"]
    assert recorder.of_type("error") and not recorder.of_type("run_started")
    assert (run_dir / "summary.json").exists()


# --- E4: mood, ending, errors ------------------------------------------------

async def test_report_mood(tmp_path):
    customer, helpdesk = agents(
        [
            ("wait_audio", 200),
            ("tool_call", "report_mood", {"score": 5, "reason": "konkret"}, "c1"),
            ("tool_call", "report_mood", {"score": 15, "reason": "za dużo"}, "c2"),
            ("tool_call", "report_mood", {"score": 0, "reason": "za mało"}, "c3"),
            ("tool_call", "report_mood", {"score": "abc", "reason": "?"}, "c4"),
            ("tool_call", "unknown_tool", {}, "c5"),
            ("sleep", 0.2),
            ("end_call",),
        ],
        [("text", "Dzień dobry."), ("audio", 200)],
    )
    summary, recorder, _ = await run_bridge(tmp_path, customer, helpdesk)
    results = {m["tool_call_id"]: m for m in customer.received if m.get("type") == "client_tool_result"}
    assert results["c1"] == {"type": "client_tool_result", "tool_call_id": "c1", "result": "ok", "is_error": False}
    assert results["c4"]["is_error"] is False
    assert results["c5"]["is_error"] is True
    assert [(m["turn"], m["score"]) for m in recorder.of_type("mood")] == [(1, 5), (1, 10), (1, 1)]
    assert summary["mood"]["start"] == 3
    assert summary["mood"]["end"] == 1
    assert summary["mood"]["delta"] == -2
    assert summary["mood"]["points"][0] == {"turn": 0, "score": 3, "reason": "start"}


async def test_end_call_drains_remaining_audio(tmp_path):
    customer, helpdesk = agents(
        [("audio", 1500), ("text", "Do widzenia!"), ("end_call",)],
        [],
    )
    summary, _, _ = await run_bridge(tmp_path, customer, helpdesk)
    assert summary["end_reason"] == "customer_end_call"
    assert helpdesk.nonsilent_ms() == 1500
    assert 2.3 <= summary["duration_s"] <= 3.2  # 1.5 s audio + 1 s tail


async def test_server_close_without_end_call(tmp_path):
    customer, helpdesk = agents([], [("sleep", 0.3), ("close",)])
    summary, _, _ = await run_bridge(tmp_path, customer, helpdesk)
    assert summary["end_reason"] == "helpdesk_closed"
    assert customer.closed_by_client


async def test_timeout(tmp_path):
    customer, helpdesk = agents([], [])
    summary, _, _ = await run_bridge(tmp_path, customer, helpdesk, max_seconds=1.0)
    assert summary["end_reason"] == "timeout"
    assert 0.9 <= summary["duration_s"] <= 1.3


async def test_client_error_ends_run(tmp_path):
    customer, helpdesk = agents([("sleep", 0.3), ("client_error", "boom")], [])
    summary, recorder, run_dir = await run_bridge(tmp_path, customer, helpdesk)
    assert summary["end_reason"] == "error"
    assert "boom" in summary["error"]
    assert helpdesk.closed_by_client
    assert recorder.of_type("error")[0]["message"] == summary["error"]
    assert (run_dir / "audio.wav").exists()


async def test_connection_failure_closes_other_session(tmp_path):
    customer, helpdesk = agents([], [])
    summary, recorder, _ = await run_bridge(tmp_path, customer, helpdesk, agent_ids=("cust", "missing"))
    assert summary["end_reason"] == "error"
    assert "helpdesk" in summary["error"]
    assert customer.closed_by_client


async def test_stop(tmp_path):
    customer, helpdesk = agents([], [])
    async with FakeElevenLabs({"cust": customer, "help": helpdesk}) as server:
        settings = replace(
            load_settings(), elevenlabs_api_key="k", customer_agent_id="cust", helpdesk_agent_id="help",
            elevenlabs_api_base=server.api_base, runs_dir=tmp_path,
        )
        recorder = Recorder()
        bridge = Bridge(settings, 5, 5, content.SCENARIOS["S2"], recorder.emit, recorder.emit_audio)

        async def stop_soon(event):
            await recorder.emit(event)
            if event["type"] == "run_started":
                bridge.stop()

        bridge._emit_cb = stop_soon
        summary = await bridge.run()
    assert summary["end_reason"] == "stopped"
    assert summary["duration_s"] <= 0.3


# --- Pure helpers ------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(5, 5), ("7", 7), (15, 10), (0, 1), (-3, 1), (6.6, 6), ("abc", None), (None, None)])
def test_clamp_score(value, expected):
    assert clamp_score(value) == expected


def test_build_summary():
    summary = build_summary(
        run_id="r", scenario_id="S1", nps_level=3, cx_level=9, end_reason="timeout", error=None, duration_s=12.345,
        turns={"customer": 2, "helpdesk": 2}, interruptions={"customer": 0, "helpdesk": 1},
        mood_points=[{"turn": 0, "score": 3, "reason": "start"}, {"turn": 2, "score": 8, "reason": "ok"}],
        conversation_ids={"customer": "a", "helpdesk": "b"},
    )
    assert summary["duration_s"] == 12.3
    assert summary["mood"] == {"start": 3, "end": 8, "delta": 5, "points": [{"turn": 0, "score": 3, "reason": "start"}, {"turn": 2, "score": 8, "reason": "ok"}]}
