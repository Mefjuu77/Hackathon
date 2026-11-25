"""Fake ElevenLabs server: signed-URL HTTP endpoint + conversation WebSocket.

Each agent runs a script of steps after receiving the initiation message. Audio is sent
in bursts (faster than real time), like the real TTS. Everything received is recorded.
"""

import asyncio
import base64
import json
from array import array
from dataclasses import dataclass, field
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.http11 import Request

from app.audio import FRAME_BYTES

CUSTOMER_SAMPLE = 1000  # constant sample value marking customer audio
HELPDESK_SAMPLE = 2000


def tone(ms: int, value: int) -> bytes:
    return array("h", [value] * (16 * ms)).tobytes()


@dataclass
class FakeAgent:
    sample: int
    steps: list[tuple] = field(default_factory=list)
    audio_format: str = "pcm_16000"
    received: list[dict] = field(default_factory=list)
    audio_frames: list[bytes] = field(default_factory=list)
    audio_frame_times: list[float] = field(default_factory=list)
    closed_by_client: bool = False

    def nonsilent_ms(self) -> int:
        return sum(len(f) for f in self.audio_frames if any(f)) // 32

    def received_types(self) -> list[str]:
        return [m.get("type", "user_audio_chunk" if "user_audio_chunk" in m else "?") for m in self.received]


class FakeElevenLabs:
    def __init__(self, agents: dict[str, FakeAgent]) -> None:
        self.agents = agents
        self.signed_url_requests = 0
        self._server = None
        self.port = 0
        self._ping_ids = 100

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0, process_request=self._http)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    def _http(self, connection: ServerConnection, request: Request):
        url = urlparse(request.path)
        if url.path == "/v1/convai/conversation/get-signed-url":
            agent_id = parse_qs(url.query)["agent_id"][0]
            if agent_id not in self.agents:
                return connection.respond(HTTPStatus.NOT_FOUND, "unknown agent\n")
            self.signed_url_requests += 1
            body = json.dumps({"signed_url": f"ws://127.0.0.1:{self.port}/v1/convai/conversation?agent_id={agent_id}&token=t"})
            return connection.respond(HTTPStatus.OK, body)
        return None  # proceed with the WebSocket handshake

    async def _handle(self, ws: ServerConnection) -> None:
        agent_id = parse_qs(urlparse(ws.request.path).query)["agent_id"][0]
        agent = self.agents[agent_id]
        init = json.loads(await ws.recv())
        agent.received.append(init)
        await ws.send(json.dumps({
            "type": "conversation_initiation_metadata",
            "conversation_initiation_metadata_event": {
                "conversation_id": f"conv_{agent_id}",
                "agent_output_audio_format": agent.audio_format,
                "user_input_audio_format": "pcm_16000",
            },
        }))
        reader = asyncio.create_task(self._read(ws, agent))
        try:
            for step in agent.steps:
                await self._step(ws, agent, step)
            await reader
        except websockets.ConnectionClosed:
            pass
        finally:
            reader.cancel()

    async def _read(self, ws: ServerConnection, agent: FakeAgent) -> None:
        loop = asyncio.get_running_loop()
        try:
            async for raw in ws:
                message = json.loads(raw)
                if "user_audio_chunk" in message:
                    agent.audio_frames.append(base64.b64decode(message["user_audio_chunk"]))
                    agent.audio_frame_times.append(loop.time())
                else:
                    agent.received.append(message)
        except websockets.ConnectionClosed:
            pass
        agent.closed_by_client = True

    async def _step(self, ws: ServerConnection, agent: FakeAgent, step: tuple) -> None:
        kind, *args = step
        match kind:
            case "audio":  # ("audio", ms[, is_final])
                ms, is_final = args[0], (args[1] if len(args) > 1 else True)
                pcm = tone(ms, agent.sample)
                chunks = [pcm[i:i + FRAME_BYTES * 2] for i in range(0, len(pcm), FRAME_BYTES * 2)]
                for i, chunk in enumerate(chunks):
                    await ws.send(json.dumps({"type": "audio", "audio_event": {
                        "audio_base_64": base64.b64encode(chunk).decode(), "event_id": i,
                        "is_final": is_final and i == len(chunks) - 1}}))
            case "text":
                await ws.send(json.dumps({"type": "agent_response", "agent_response_event": {"agent_response": args[0], "event_id": 1}}))
            case "correction":
                await ws.send(json.dumps({"type": "agent_response_correction", "agent_response_correction_event": {
                    "original_agent_response": args[0], "corrected_agent_response": args[1], "event_id": 2}}))
            case "ping":
                self._ping_ids += 1
                await ws.send(json.dumps({"type": "ping", "ping_event": {"event_id": self._ping_ids, "ping_ms": 5}}))
            case "interruption":
                await ws.send(json.dumps({"type": "interruption", "interruption_event": {"event_id": 3}}))
            case "tool_call":  # ("tool_call", name, params, call_id)
                await ws.send(json.dumps({"type": "client_tool_call", "client_tool_call": {
                    "tool_name": args[0], "parameters": args[1], "tool_call_id": args[2], "event_id": 4, "expects_response": False}}))
            case "end_call":
                await ws.send(json.dumps({"type": "agent_tool_response", "agent_tool_response": {
                    "tool_name": "end_call", "tool_call_id": "t_end", "tool_type": "system", "is_error": False, "event_id": 5}}))
            case "client_error":
                await ws.send(json.dumps({"type": "client_error", "error_event": {"code": 1008, "message": args[0]}}))
            case "close":
                await ws.close()
            case "sleep":
                await asyncio.sleep(args[0])
            case "wait_audio":  # wait until this agent heard at least `ms` of non-silent audio
                while agent.nonsilent_ms() < args[0]:
                    await asyncio.sleep(0.02)
            case _:
                raise ValueError(f"unknown step {kind}")
