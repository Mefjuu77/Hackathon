"""At most one active run at a time (SPEC §6.4)."""

import asyncio

from app import content
from app.bridge import Bridge, EmitAudio, EmitEvent
from app.config import Settings

ALREADY_RUNNING = "Rozmowa już trwa"


class RunManager:
    def __init__(self, settings: Settings, bridge_factory=Bridge) -> None:
        self.settings = settings
        self._bridge_factory = bridge_factory
        self._bridge: Bridge | None = None
        self._task: asyncio.Task | None = None
        self._owner: object | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(
        self,
        owner: object,
        nps_level: int,
        cx_level: int,
        scenario: content.Scenario,
        emit: EmitEvent,
        emit_audio: EmitAudio,
    ) -> bool:
        """Start a run owned by `owner`; False when another run is still active."""
        if self.active:
            return False
        self._bridge = self._bridge_factory(self.settings, nps_level, cx_level, scenario, emit, emit_audio)
        self._owner = owner
        self._task = asyncio.create_task(self._bridge.run())
        return True

    def stop(self, owner: object) -> None:
        """Stop the active run if `owner` started it."""
        if self.active and self._owner is owner:
            self._bridge.stop()

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
