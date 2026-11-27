import asyncio
import json
import threading
import wave
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import content
from app.bridge import Bridge
from app.config import load_settings
from app.main import create_app, parse_start
from app.replay import load_ui_events, replay_run
from tests.fake_elevenlabs import CUSTOMER_SAMPLE, HELPDESK_SAMPLE, FakeAgent, FakeElevenLabs


@contextmanager
def fake_elevenlabs_in_thread(agents):
    """FakeElevenLabs on its own event loop, so the sync TestClient can talk to it."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    async def serve():
        async with FakeElevenLabs(agents) as server:
            holder["server"] = server
            holder["stop"] = asyncio.Event()
            ready.set()
            await holder["stop"].wait()

    thread = threading.Thread(target=loop.run_until_complete, args=(serve(),), daemon=True)
    thread.start()
    assert ready.wait(5)
    try:
        yield holder["server"]
    finally:
        loop.call_soon_threadsafe(holder["stop"].set)
        thread.join(5)
        loop.close()


def make_settings(tmp_path, api_base="http://127.0.0.1:9", **overrides):
    values = {
        "elevenlabs_api_key": "test-key",
        "customer_agent_id": "cust",
        "helpdesk_agent_id": "help",
        "elevenlabs_api_base": api_base,
        "runs_dir": tmp_path,
    }
    return replace(load_settings(), **(values | overrides))


def receive_until(ws, event_type, limit=2000):
    """Collect messages until a JSON event of `event_type`; returns (events, binary_frames)."""
    events, frames = [], []
    for _ in range(limit):
        message = ws.receive()
        if message.get("bytes") is not None:
            frames.append(message["bytes"])
            continue
        event = json.loads(message["text"])
        events.append(event)
        if event["type"] == event_type:
            return events, frames
    raise AssertionError(f"no {event_type} in {limit} messages: {events}")


START = {"type": "start", "nps_level": 3, "cx_level": 9, "scenario_id": "S1"}


# --- Live WebSocket ----------------------------------------------------------

def test_live_run_over_websocket(tmp_path):
    customer = FakeAgent(CUSTOMER_SAMPLE, [("audio", 500), ("text", "Dzień dobry."), ("wait_audio", 300), ("end_call",)])
    helpdesk = FakeAgent(HELPDESK_SAMPLE, [("wait_audio", 500), ("audio", 300), ("text", "Słucham.")])
    with fake_elevenlabs_in_thread({"cust": customer, "help": helpdesk}) as server:
        with TestClient(create_app(make_settings(tmp_path, server.api_base))) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json(START)
                events, frames = receive_until(ws, "run_ended")
    assert events[0]["type"] == "run_started"
    assert events[0]["scenario_id"] == "S1"
    assert [e["text"] for e in events if e["type"] == "transcript"] == ["Dzień dobry.", "Słucham."]
    assert frames and {len(f) for f in frames} == {6400}
    ended = events[-1]
    assert ended["end_reason"] == "customer_end_call"
    run_id = ended["summary"]["run_id"]
    assert ended["files"] == {"audio": f"/runs/{run_id}/audio.wav", "transcript": f"/runs/{run_id}/transcript.json"}
    assert client.get(ended["files"]["audio"]).status_code == 200
    assert client.get(ended["files"]["transcript"]).json()[0]["text"] == "Dzień dobry."


def test_second_start_is_rejected_and_stop_ends_run(tmp_path):
    customer = FakeAgent(CUSTOMER_SAMPLE, [("sleep", 30)])
    helpdesk = FakeAgent(HELPDESK_SAMPLE, [("sleep", 30)])
    with fake_elevenlabs_in_thread({"cust": customer, "help": helpdesk}) as server:
        with (
            TestClient(create_app(make_settings(tmp_path, server.api_base))) as client,
            client.websocket_connect("/ws") as ws,
            client.websocket_connect("/ws") as other,
        ):
            ws.send_json(START)
            receive_until(ws, "run_started")
            other.send_json(START)
            assert other.receive_json() == {"type": "error", "message": "A conversation is already running"}
            ws.send_json(START)
            events, _ = receive_until(ws, "error")
            assert events[-1]["message"] == "A conversation is already running"
            other.send_json({"type": "stop"})  # only the owner can stop the run
            ws.send_json({"type": "stop"})
            events, _ = receive_until(ws, "run_ended")
    assert events[-1]["end_reason"] == "stopped"


def test_closing_the_tab_stops_the_run(tmp_path):
    customer = FakeAgent(CUSTOMER_SAMPLE, [("sleep", 30)])
    helpdesk = FakeAgent(HELPDESK_SAMPLE, [("sleep", 30)])
    with fake_elevenlabs_in_thread({"cust": customer, "help": helpdesk}) as server:
        app = create_app(make_settings(tmp_path, server.api_base))
        with TestClient(app) as client:  # one event loop for all sockets, like uvicorn
            with client.websocket_connect("/ws") as ws:
                ws.send_json(START)
                receive_until(ws, "run_started")
            for _ in range(50):
                if not app.state.run_manager.active:
                    break
                threading.Event().wait(0.1)
            assert not app.state.run_manager.active
    summary = json.loads(next(tmp_path.glob("*/summary.json")).read_text(encoding="utf-8"))
    assert summary["end_reason"] == "stopped"


@pytest.mark.parametrize("command,fragment", [
    ({**START, "nps_level": 0}, "nps_level"),
    ({**START, "cx_level": "5"}, "cx_level"),
    ({**START, "scenario_id": "S9"}, "S9"),
    ({"type": "dance"}, "Unknown command"),
])
def test_invalid_commands_get_error(tmp_path, command, fragment):
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect("/ws") as ws:
        ws.send_json(command)
        event = ws.receive_json()
    assert event["type"] == "error" and fragment in event["message"]
    assert not list(tmp_path.iterdir())


def test_start_without_elevenlabs_settings(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path, elevenlabs_api_key="")))
    with client.websocket_connect("/ws") as ws:
        ws.send_json(START)
        event = ws.receive_json()
    assert event["type"] == "error" and "ELEVENLABS_API_KEY" in event["message"]


def test_parse_start_random_scenario():
    nps, cx, scenario = parse_start({"type": "start", "nps_level": 1, "cx_level": 10, "scenario_id": "random"})
    assert (nps, cx) == (1, 10) and scenario.id in content.SCENARIOS


# --- Run files ---------------------------------------------------------------

@pytest.fixture
def saved_run(tmp_path):
    """A finished run produced by the real Bridge against the fake server."""
    customer = FakeAgent(CUSTOMER_SAMPLE, [
        ("audio", 600), ("text", "Halo, nie działa internet."),
        ("tool_call", "report_mood", {"score": 4, "reason": "czekam"}, "c1"),
        ("wait_audio", 400), ("end_call",),
    ])
    helpdesk = FakeAgent(HELPDESK_SAMPLE, [("wait_audio", 600), ("audio", 400), ("text", "Już sprawdzam.")])

    async def produce():
        async with FakeElevenLabs({"cust": customer, "help": helpdesk}) as server:
            bridge = Bridge(make_settings(tmp_path, server.api_base), 3, 9, content.SCENARIOS["S1"], _noop, _noop)
            return await bridge.run()

    summary = asyncio.run(produce())
    return tmp_path, summary["run_id"]


async def _noop(_):
    pass


def test_run_files_are_served(saved_run):
    runs_dir, run_id = saved_run
    client = TestClient(create_app(make_settings(runs_dir)))
    for name in ("audio.wav", "events.jsonl", "transcript.json", "summary.json"):
        assert client.get(f"/runs/{run_id}/{name}").status_code == 200
    assert client.get(f"/runs/{run_id}/summary.json").json()["run_id"] == run_id


@pytest.mark.parametrize("path", [
    "/runs/{run_id}/../x",
    "/runs/{run_id}/%2e%2e%2fsummary.json",
    "/runs/{run_id}/secret.txt",
    "/runs/..%2f..%2f/summary.json",
    "/runs/missing_S1/summary.json",
    "/runs/{run_id}/..%5c..%5c.env",
])
def test_run_files_reject_bad_paths(saved_run, path):
    runs_dir, run_id = saved_run
    (runs_dir / run_id / "secret.txt").write_text("x")
    client = TestClient(create_app(make_settings(runs_dir)))
    assert client.get(path.format(run_id=run_id)).status_code == 404


def test_replay_page(saved_run):
    runs_dir, run_id = saved_run
    client = TestClient(create_app(make_settings(runs_dir)))
    assert "Customer vs Help Desk" in client.get(f"/replay/{run_id}").text
    assert client.get("/replay/nope").status_code == 404


# --- Replay ------------------------------------------------------------------

async def test_replay_reproduces_events_and_audio(saved_run):
    runs_dir, run_id = saved_run
    run_dir = runs_dir / run_id
    sent_events, sent_audio, order = [], [], []

    async def send_json(event):
        sent_events.append(event)
        order.append("event")

    async def send_bytes(frame):
        sent_audio.append(frame)
        order.append("audio")

    await replay_run(run_dir, send_json, send_bytes, speed=20)

    assert sent_events == [event for _, event in load_ui_events(run_dir)]
    assert [e["type"] for e in sent_events][0] == "run_started" and sent_events[-1]["type"] == "run_ended"
    with wave.open(str(run_dir / "audio.wav")) as wav:
        assert b"".join(sent_audio) == wav.readframes(wav.getnframes())
    assert {len(f) for f in sent_audio[:-1]} == {6400}
    assert order[0] == "event" and order[-1] == "event"  # run_started first, run_ended after the audio


async def test_replay_is_paced_in_real_time(saved_run):
    runs_dir, run_id = saved_run
    loop = asyncio.get_running_loop()
    times = []

    async def send_bytes(_):
        times.append(loop.time())

    async def send_json(_):
        pass

    await replay_run(runs_dir / run_id, send_json, send_bytes)
    expected = (len(times) - 1) * 0.1
    assert abs((times[-1] - times[0]) - expected) <= 0.15


def test_replay_websocket(saved_run):
    runs_dir, run_id = saved_run
    client = TestClient(create_app(make_settings(runs_dir, elevenlabs_api_key="")))  # no EL key needed
    with client.websocket_connect(f"/ws/replay/{run_id}") as ws:
        events, frames = receive_until(ws, "run_ended")
    assert [e["type"] for e in events] == [e["type"] for _, e in load_ui_events(runs_dir / run_id)]
    assert frames and {len(f) for f in frames[:-1]} == {6400}


def test_replay_websocket_unknown_run(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect("/ws/replay/missing") as ws:
        assert ws.receive_json()["type"] == "error"


# --- Run listing ---------------------------------------------------------------

def _write_run(runs_dir, run_id, summary="default", files=("audio.wav", "events.jsonl")):
    path = runs_dir / run_id
    path.mkdir()
    for name in files:
        (path / name).write_bytes(b"")
    if summary == "default":
        summary = {
            "run_id": run_id, "scenario_id": "S2", "nps_level": 2, "cx_level": 8,
            "end_reason": "helpdesk_end_call", "duration_s": 207.3,
            "turns": {"customer": 9, "helpdesk": 8}, "mood": {"start": 2, "end": 7, "delta": 5, "points": []},
        }
    if summary is not None:
        text = summary if isinstance(summary, str) else json.dumps(summary)
        (path / "summary.json").write_text(text, encoding="utf-8")


def test_list_runs_newest_first_with_fields(tmp_path):
    _write_run(tmp_path, "20261125-161827_S1")
    _write_run(tmp_path, "20261126-111736_S2")
    client = TestClient(create_app(make_settings(tmp_path)))
    runs = client.get("/api/runs").json()
    assert [r["run_id"] for r in runs] == ["20261126-111736_S2", "20261125-161827_S1"]
    assert runs[0] == {
        "run_id": "20261126-111736_S2", "scenario_id": "S2", "scenario_title": content.SCENARIOS["S2"].display_title,
        "nps_level": 2, "cx_level": 8, "end_reason": "helpdesk_end_call", "duration_s": 207.3,
        "turns": {"customer": 9, "helpdesk": 8}, "mood": {"start": 2, "end": 7, "delta": 5},
        "started_at": "2026-11-26T11:17:36",
    }


def test_list_runs_unknown_scenario_and_unparsable_id(tmp_path):
    _write_run(tmp_path, "custom_run", {
        "scenario_id": "ZZ", "nps_level": 1, "cx_level": 1, "end_reason": "stopped", "duration_s": 1.0,
        "turns": {"customer": 0, "helpdesk": 0}, "mood": {"start": 1, "end": 1, "delta": 0},
    })
    run = TestClient(create_app(make_settings(tmp_path))).get("/api/runs").json()[0]
    assert run["scenario_title"] is None and run["started_at"] is None


def test_list_runs_skips_incomplete_bad_and_corrupt(tmp_path):
    _write_run(tmp_path, "20261126-111736_S2")
    _write_run(tmp_path, "20261126-120000_S1", files=("audio.wav",))        # no events
    _write_run(tmp_path, "20261126-120001_S1", summary=None)                 # no summary
    _write_run(tmp_path, "20261126-120002_S1", summary="{not json")         # corrupt
    _write_run(tmp_path, "20261126-120003_S1", summary={"scenario_id": "S1"})  # missing fields
    _write_run(tmp_path, "bad name")                                         # fails run-id regex
    (tmp_path / "stray.txt").write_text("x")
    client = TestClient(create_app(make_settings(tmp_path)))
    response = client.get("/api/runs")
    assert response.status_code == 200
    assert [r["run_id"] for r in response.json()] == ["20261126-111736_S2"]


def test_list_runs_missing_dir(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path / "nope")))
    assert client.get("/api/runs").json() == []
