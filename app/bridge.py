"""One run: two ElevenLabs sessions bridged audio-to-audio in real time (SPEC §6.3)."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from app import content
from app.audio import FRAME_BYTES, TICK_SECONDS, OutputBuffer, mix_stereo
from app.config import Settings
from app.elevenlabs_session import (
    AgentResponse,
    AgentToolResponse,
    AudioChunk,
    Closed,
    Correction,
    ElevenLabsSession,
    ErrorEvent,
    Interruption,
    SessionError,
    SessionEvent,
    ToolCall,
)
from app.run_store import RunStore

log = logging.getLogger(__name__)

ROLES = ("customer", "helpdesk")
DRAIN_MAX_SECONDS = 5.0
DRAIN_TAIL_SECONDS = 1.0

EmitEvent = Callable[[dict], Awaitable[None]]
EmitAudio = Callable[[bytes], Awaitable[None]]
SessionFactory = Callable[..., ElevenLabsSession]


def clamp_score(value) -> int | None:
    """Mood score as int clamped to 1-10, or None when it is not a number."""
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(1, min(10, score))


def build_summary(
    *,
    run_id: str,
    scenario_id: str,
    nps_level: int,
    cx_level: int,
    end_reason: str,
    error: str | None,
    duration_s: float,
    turns: dict[str, int],
    interruptions: dict[str, int],
    mood_points: list[dict],
    conversation_ids: dict[str, str | None],
) -> dict:
    start, end = mood_points[0]["score"], mood_points[-1]["score"]
    return {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "nps_level": nps_level,
        "cx_level": cx_level,
        "end_reason": end_reason,
        "error": error,
        "duration_s": round(duration_s, 1),
        "turns": dict(turns),
        "interruptions": dict(interruptions),
        "mood": {"start": start, "end": end, "delta": end - start, "points": list(mood_points)},
        "conversation_ids": dict(conversation_ids),
    }


class Bridge:
    def __init__(
        self,
        settings: Settings,
        nps_level: int,
        cx_level: int,
        scenario: content.Scenario,
        emit: EmitEvent,
        emit_audio: EmitAudio,
        session_factory: SessionFactory = ElevenLabsSession,
    ) -> None:
        self.settings = settings
        self.nps_level = nps_level
        self.cx_level = cx_level
        self.scenario = scenario
        self._emit_cb = emit
        self._emit_audio_cb = emit_audio
        self._session_factory = session_factory

        self.store: RunStore | None = None
        self.sessions: dict[str, ElevenLabsSession] = {}
        self.buffers = {role: OutputBuffer() for role in ROLES}
        self.turns = dict.fromkeys(ROLES, 0)
        self.interruptions = dict.fromkeys(ROLES, 0)
        self.utterances: list[dict] = []
        self._last_utterance: dict[str, dict | None] = dict.fromkeys(ROLES)
        self.mood_points = [{"turn": 0, "score": nps_level, "reason": "start"}]

        self.end_reason: str | None = None
        self.error: str | None = None
        self._stop_now = False
        self._drain_started: float | None = None
        self._drained_at: float | None = None
        self._loop_started: float | None = None
        self._started = False
        self._pending_ui: list[dict] = []  # UI events that arrive before run_started

    @property
    def run_id(self) -> str | None:
        return self.store.run_id if self.store else None

    # --- Public API ----------------------------------------------------------

    def stop(self) -> None:
        self._request_end("stopped", graceful=False)

    async def run(self) -> dict:
        """Run the whole conversation; always writes the run files and returns the summary."""
        self.store = RunStore(self.settings.runs_dir, self.scenario.id)
        duration = 0.0
        try:
            await self._connect()
            if not self._stop_now:
                await self._emit({
                    "type": "run_started",
                    "run_id": self.run_id,
                    "scenario_id": self.scenario.id,
                    "scenario_title": self.scenario.title,
                    "nps_level": self.nps_level,
                    "cx_level": self.cx_level,
                    "customer_name": self.scenario.customer_name,
                    "agent_name": content.AGENT_NAME,
                }, force=True)
                await self._flush_pending()
                self._loop_started = time.monotonic()
                await self._tick_loop()
                duration = time.monotonic() - self._loop_started
        except SessionError as error:
            self._request_end("error", graceful=False, error=str(error))
        except Exception as error:
            log.exception("run failed")
            self._request_end("error", graceful=False, error=f"{type(error).__name__}: {error}")
        finally:
            await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)
        return await self._finish(duration)

    # --- Lifecycle -----------------------------------------------------------

    async def _connect(self) -> None:
        settings = self.settings
        el_max = settings.el_max_duration_seconds
        inits = {
            "customer": content.build_customer_init(self.nps_level, self.scenario, el_max),
            "helpdesk": content.build_helpdesk_init(self.cx_level, self.scenario, el_max),
        }
        agent_ids = {"customer": settings.customer_agent_id, "helpdesk": settings.helpdesk_agent_id}
        for role in ROLES:
            self.sessions[role] = self._session_factory(
                role=role,
                agent_id=agent_ids[role],
                api_base=settings.elevenlabs_api_base,
                api_key=settings.elevenlabs_api_key,
                on_event=self._on_session_event,
                log_raw=self.store.log,
            )
        # Wait for both attempts so a failed one never leaves the other half-open.
        results = await asyncio.gather(
            *(self.sessions[role].connect(inits[role]) for role in ROLES), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _tick_loop(self) -> None:
        next_tick = self._loop_started
        customer, helpdesk = self.sessions["customer"], self.sessions["helpdesk"]
        while not self._should_exit():
            c = self.buffers["customer"].take(FRAME_BYTES)
            h = self.buffers["helpdesk"].take(FRAME_BYTES)
            await asyncio.gather(helpdesk.send_audio(c), customer.send_audio(h))
            stereo = mix_stereo(c, h)
            self.store.write_audio(stereo)
            try:
                await self._emit_audio_cb(stereo)
            except Exception:
                log.debug("emit_audio failed", exc_info=True)
            next_tick += TICK_SECONDS
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

    def _should_exit(self) -> bool:
        now = time.monotonic()
        if self._stop_now:
            return True
        if self.end_reason is None and now - self._loop_started >= self.settings.max_conversation_seconds:
            self._request_end("timeout", graceful=False)
            return True
        if self._drain_started is not None and self._drained_at is None:
            if all(b.is_empty for b in self.buffers.values()) or now - self._drain_started >= DRAIN_MAX_SECONDS:
                self._drained_at = now
        return self._drained_at is not None and now - self._drained_at >= DRAIN_TAIL_SECONDS

    def _request_end(self, reason: str, graceful: bool, error: str | None = None) -> None:
        if self.end_reason is not None:
            # Already ending (first reason wins); a hard stop still cuts the drain short.
            self._stop_now = self._stop_now or not graceful
            return
        self.end_reason = reason
        self.error = error
        if graceful:
            self._drain_started = time.monotonic()
        else:
            self._stop_now = True

    async def _finish(self, duration: float) -> dict:
        assert self.end_reason is not None
        summary = build_summary(
            run_id=self.run_id,
            scenario_id=self.scenario.id,
            nps_level=self.nps_level,
            cx_level=self.cx_level,
            end_reason=self.end_reason,
            error=self.error,
            duration_s=duration,
            turns=self.turns,
            interruptions=self.interruptions,
            mood_points=self.mood_points,
            conversation_ids={role: s.conversation_id for role, s in self.sessions.items()},
        )
        self.store.write_json("transcript.json", self.utterances)
        self.store.write_json("summary.json", summary)
        await self._flush_pending()
        if self.error:
            await self._emit({"type": "error", "message": self.error})
        await self._emit({
            "type": "run_ended",
            "end_reason": self.end_reason,
            "summary": summary,
            "files": {
                "audio": f"/runs/{self.run_id}/audio.wav",
                "transcript": f"/runs/{self.run_id}/transcript.json",
            },
        })
        self.store.close()
        return summary

    # --- Session events ------------------------------------------------------

    async def _emit(self, event: dict, force: bool = False) -> None:
        if not (self._started or force):
            self._pending_ui.append(event)
            return
        self.store.log("ui", event)
        try:
            await self._emit_cb(event)
        except Exception:
            log.debug("emit failed", exc_info=True)

    async def _flush_pending(self) -> None:
        self._started = True
        pending, self._pending_ui = self._pending_ui, []
        for event in pending:
            await self._emit(event)

    async def _on_session_event(self, role: str, event: SessionEvent) -> None:
        match event:
            case AudioChunk(pcm=pcm):
                if not self._stop_now:
                    self.buffers[role].append(pcm)
            case AgentResponse(text=text):
                await self._add_utterance(role, text)
            case Correction(original=original, corrected=corrected):
                await self._correct_utterance(role, original, corrected)
            case Interruption():
                await self._interrupt(role)
            case ToolCall():
                await self._tool_call(role, event)
            case AgentToolResponse(name="end_call"):
                self._request_end(f"{role}_end_call", graceful=True)
            case Closed():
                self._request_end(f"{role}_closed", graceful=True)
            case ErrorEvent(message=message):
                self._request_end("error", graceful=False, error=message)

    def _elapsed(self) -> float:
        return round(time.monotonic() - self._loop_started, 2) if self._loop_started else 0.0

    async def _add_utterance(self, role: str, text: str) -> None:
        self.turns[role] += 1
        utterance = {
            "utterance_id": len(self.utterances) + 1,
            "role": role,
            "text": text,
            "t": self._elapsed(),
            "interrupted": False,
        }
        self.utterances.append(utterance)
        self._last_utterance[role] = utterance
        await self._emit({"type": "transcript", **{k: utterance[k] for k in ("utterance_id", "role", "text", "t")}})

    async def _correct_utterance(self, role: str, original: str, corrected: str) -> None:
        candidates = [u for u in self.utterances if u["role"] == role]
        target = next((u for u in reversed(candidates) if u["text"] == original), candidates[-1] if candidates else None)
        if target is None:
            return
        target["text"] = corrected
        await self._emit({"type": "transcript_correction", "utterance_id": target["utterance_id"], "role": role, "text": corrected})

    async def _interrupt(self, role: str) -> None:
        self.buffers[role].clear()
        self.interruptions[role] += 1
        if self._last_utterance[role] is not None:
            self._last_utterance[role]["interrupted"] = True
        await self._emit({"type": "interruption", "role": role})

    async def _tool_call(self, role: str, call: ToolCall) -> None:
        session = self.sessions[role]
        if role != "customer" or call.name != "report_mood":
            await session.send_tool_result(call.call_id, is_error=True)
            return
        await session.send_tool_result(call.call_id)
        score = clamp_score(call.parameters.get("score"))
        if score is None:
            log.warning("report_mood with invalid score: %r", call.parameters)
            return
        point = {"turn": self.turns["helpdesk"], "score": score, "reason": str(call.parameters.get("reason", ""))}
        self.mood_points.append(point)
        await self._emit({"type": "mood", **point})
