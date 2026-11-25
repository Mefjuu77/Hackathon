"""Client for one ElevenLabs Agents conversation over WebSocket (raw protocol, no SDK).

Protocol reference: https://elevenlabs.io/docs/eleven-agents/api-reference/eleven-agents/websocket
"""

import asyncio
import base64
import copy
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import websockets

log = logging.getLogger(__name__)

EXPECTED_AUDIO_FORMAT = "pcm_16000"
CONNECT_TIMEOUT_SECONDS = 15


# --- Normalized events passed to the bridge ---------------------------------

@dataclass(frozen=True)
class AudioChunk:
    pcm: bytes
    is_final: bool


@dataclass(frozen=True)
class AgentResponse:
    text: str


@dataclass(frozen=True)
class Correction:
    original: str
    corrected: str


@dataclass(frozen=True)
class Interruption:
    pass


@dataclass(frozen=True)
class ToolCall:
    name: str
    call_id: str
    parameters: dict


@dataclass(frozen=True)
class AgentToolResponse:
    name: str


@dataclass(frozen=True)
class Closed:
    code: int | None
    reason: str


@dataclass(frozen=True)
class ErrorEvent:
    message: str


SessionEvent = AudioChunk | AgentResponse | Correction | Interruption | ToolCall | AgentToolResponse | Closed | ErrorEvent
EventHandler = Callable[[str, SessionEvent], Awaitable[None]]
RawLogger = Callable[[str, dict], None]


class SessionError(Exception):
    pass


def _loggable(message: dict) -> dict:
    """Copy of a raw server message with the audio payload replaced by its size."""
    audio_event = message.get("audio_event")
    if not isinstance(audio_event, dict) or "audio_base_64" not in audio_event:
        return message
    message = copy.deepcopy(message)
    payload = message["audio_event"].pop("audio_base_64") or ""
    message["audio_event"]["audio_bytes"] = len(payload) * 3 // 4
    return message


class ElevenLabsSession:
    def __init__(
        self,
        role: str,
        agent_id: str,
        api_base: str,
        api_key: str,
        on_event: EventHandler,
        log_raw: RawLogger,
    ) -> None:
        self.role = role
        self.agent_id = agent_id
        self.api_base = api_base
        self.api_key = api_key
        self.conversation_id: str | None = None
        self._on_event = on_event
        self._log_raw = log_raw
        self._ws: websockets.ClientConnection | None = None
        self._receiver: asyncio.Task | None = None
        self._closing = False

    async def _signed_url(self) -> str:
        async with httpx.AsyncClient(base_url=self.api_base, timeout=CONNECT_TIMEOUT_SECONDS) as client:
            response = await client.get(
                "/v1/convai/conversation/get-signed-url",
                params={"agent_id": self.agent_id},
                headers={"xi-api-key": self.api_key},
            )
            response.raise_for_status()
            return response.json()["signed_url"]

    async def connect(self, init_message: dict) -> None:
        """Open the session, send initiation data and wait for the server's metadata."""
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                url = await self._signed_url()
                self._ws = await websockets.connect(url, max_size=None)
                await self._ws.send(json.dumps(init_message))
                self._log_raw(self.role, {"sent": init_message})
                while True:
                    message = json.loads(await self._ws.recv())
                    self._log_raw(self.role, _loggable(message))
                    if message.get("type") == "ping":
                        await self._pong(message)
                    elif message.get("type") == "conversation_initiation_metadata":
                        self._accept_metadata(message)
                        break
        except TimeoutError:
            raise SessionError(f"{self.role}: no conversation_initiation_metadata within {CONNECT_TIMEOUT_SECONDS} s") from None
        except (httpx.HTTPError, OSError, websockets.WebSocketException, KeyError, ValueError) as error:
            raise SessionError(f"{self.role}: connection failed: {error}") from error
        self._receiver = asyncio.create_task(self._receive_loop())

    def _accept_metadata(self, message: dict) -> None:
        metadata = message.get("conversation_initiation_metadata_event", {})
        self.conversation_id = metadata.get("conversation_id")
        formats = (metadata.get("agent_output_audio_format"), metadata.get("user_input_audio_format"))
        if formats != (EXPECTED_AUDIO_FORMAT, EXPECTED_AUDIO_FORMAT):
            raise SessionError(f"{self.role}: audio formats {formats}, expected {EXPECTED_AUDIO_FORMAT} in and out")

    async def _pong(self, message: dict) -> None:
        event_id = message.get("ping_event", {}).get("event_id")
        await self._ws.send(json.dumps({"type": "pong", "event_id": event_id}))

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                self._log_raw(self.role, _loggable(message))
                if message.get("type") == "ping":
                    await self._pong(message)
                    continue
                event = self._parse(message)
                if event is not None:
                    await self._on_event(self.role, event)
        except websockets.ConnectionClosed:
            pass
        except Exception as error:  # surface unexpected failures as a run error
            log.exception("%s receive loop failed", self.role)
            await self._on_event(self.role, ErrorEvent(f"{self.role}: {error}"))
            return
        if not self._closing:
            await self._on_event(self.role, Closed(self._ws.close_code, self._ws.close_reason or ""))

    @staticmethod
    def _parse(message: dict) -> SessionEvent | None:
        match message.get("type"):
            case "audio":
                event = message.get("audio_event", {})
                return AudioChunk(base64.b64decode(event.get("audio_base_64") or ""), bool(event.get("is_final")))
            case "agent_response":
                return AgentResponse(message.get("agent_response_event", {}).get("agent_response", ""))
            case "agent_response_correction":
                event = message.get("agent_response_correction_event", {})
                return Correction(event.get("original_agent_response", ""), event.get("corrected_agent_response", ""))
            case "interruption":
                return Interruption()
            case "client_tool_call":
                call = message.get("client_tool_call", {})
                return ToolCall(call.get("tool_name", ""), call.get("tool_call_id", ""), call.get("parameters") or {})
            case "agent_tool_response":
                return AgentToolResponse(message.get("agent_tool_response", {}).get("tool_name", ""))
            case "client_error":
                error = message.get("error_event", {})
                return ErrorEvent(f"client_error {error.get('code')}: {error.get('message', '')}")
        return None  # user_transcript and unknown types are only logged

    async def send_audio(self, pcm: bytes) -> None:
        await self._send({"user_audio_chunk": base64.b64encode(pcm).decode("ascii")}, log_message=False)

    async def send_tool_result(self, call_id: str, is_error: bool = False) -> None:
        await self._send({
            "type": "client_tool_result",
            "tool_call_id": call_id,
            "result": "error" if is_error else "ok",
            "is_error": is_error,
        })

    async def _send(self, message: dict, log_message: bool = True) -> None:
        if self._ws is None or self._closing:
            return
        try:
            await self._ws.send(json.dumps(message))
        except websockets.ConnectionClosed:
            return
        if log_message:
            self._log_raw(self.role, {"sent": message})

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            await self._ws.close()
        if self._receiver is not None:
            self._receiver.cancel()
            try:
                await self._receiver
            except asyncio.CancelledError:
                pass
