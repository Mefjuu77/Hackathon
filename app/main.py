import asyncio
import json
import logging
import re

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import content
from app.config import PROJECT_ROOT, Settings, load_settings
from app.content import SCENARIOS
from app.replay import replay_run
from app.run_manager import ALREADY_RUNNING, RunManager
from app.run_store import RUN_FILES

STATIC_DIR = PROJECT_ROOT / "static"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

log = logging.getLogger(__name__)


class BrowserConnection:
    """Outgoing messages for one browser socket, sent by a single task so senders never block each other."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self._queue: asyncio.Queue[dict | bytes | None] = asyncio.Queue()

    async def send_json(self, event: dict) -> None:
        self._queue.put_nowait(event)

    async def send_bytes(self, frame: bytes) -> None:
        self._queue.put_nowait(frame)

    def close(self) -> None:
        self._queue.put_nowait(None)

    async def pump(self) -> None:
        try:
            while (item := await self._queue.get()) is not None:
                if isinstance(item, bytes):
                    await self.websocket.send_bytes(item)
                else:
                    await self.websocket.send_text(json.dumps(item, ensure_ascii=False))
        except (WebSocketDisconnect, RuntimeError):
            pass  # browser went away


def parse_start(message: dict) -> tuple[int, int, content.Scenario]:
    """Validate a `start` command; raises ValueError with a message for the UI."""
    levels = []
    for key in ("nps_level", "cx_level"):
        value = message.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value not in content.LEVELS:
            raise ValueError(f"Niepoprawna wartość {key}: {value!r} (oczekiwano 1–10)")
        levels.append(value)
    scenario_id = message.get("scenario_id", "random")
    if scenario_id != "random" and scenario_id not in SCENARIOS:
        raise ValueError(f"Nieznany scenariusz: {scenario_id!r}")
    return levels[0], levels[1], content.pick_scenario(scenario_id)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    manager = RunManager(settings)

    app = FastAPI(title="Customer vs Help Desk")
    app.state.settings = settings
    app.state.run_manager = manager
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def run_dir_or_404(run_id: str):
        path = settings.runs_dir / run_id
        if not RUN_ID_PATTERN.match(run_id) or not (path / "events.jsonl").is_file():
            raise HTTPException(status_code=404)
        return path

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/scenarios")
    async def list_scenarios() -> list[dict]:
        return [{"id": s.id, "title": s.title} for s in SCENARIOS.values()]

    @app.get("/replay/{run_id}", include_in_schema=False)
    async def replay_page(run_id: str) -> FileResponse:
        run_dir_or_404(run_id)
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/runs/{run_id}/{name}")
    async def run_file(run_id: str, name: str) -> FileResponse:
        if name not in RUN_FILES:
            raise HTTPException(status_code=404)
        path = run_dir_or_404(run_id) / name
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, filename=f"{run_id}_{name}")

    @app.websocket("/ws")
    async def live(websocket: WebSocket) -> None:
        await websocket.accept()
        connection = BrowserConnection(websocket)
        pump = asyncio.create_task(connection.pump())
        try:
            while True:
                try:
                    message = json.loads(await websocket.receive_text())
                except json.JSONDecodeError:
                    await connection.send_json({"type": "error", "message": "Niepoprawna wiadomość (oczekiwano JSON)"})
                    continue
                match message.get("type") if isinstance(message, dict) else None:
                    case "start":
                        await handle_start(connection, message)
                    case "stop":
                        manager.stop(connection)
                    case other:
                        await connection.send_json({"type": "error", "message": f"Nieznana komenda: {other!r}"})
        except WebSocketDisconnect:
            pass
        finally:
            manager.stop(connection)  # a closed tab ends its run as `stopped`
            connection.close()
            await pump

    async def handle_start(connection: BrowserConnection, message: dict) -> None:
        try:
            nps_level, cx_level, scenario = parse_start(message)
            settings.require_elevenlabs()
        except (ValueError, RuntimeError) as error:
            await connection.send_json({"type": "error", "message": str(error)})
            return
        if not manager.start(connection, nps_level, cx_level, scenario, connection.send_json, connection.send_bytes):
            await connection.send_json({"type": "error", "message": ALREADY_RUNNING})

    @app.websocket("/ws/replay/{run_id}")
    async def replay(websocket: WebSocket, run_id: str) -> None:
        await websocket.accept()
        connection = BrowserConnection(websocket)
        pump = asyncio.create_task(connection.pump())
        try:
            run_dir = run_dir_or_404(run_id)
        except HTTPException:
            await connection.send_json({"type": "error", "message": f"Nie znaleziono przebiegu {run_id}"})
        else:
            replay_task = asyncio.create_task(replay_run(run_dir, connection.send_json, connection.send_bytes))
            closed = asyncio.create_task(wait_for_disconnect(websocket))
            await asyncio.wait({replay_task, closed}, return_when=asyncio.FIRST_COMPLETED)
            for task in (replay_task, closed):
                task.cancel()
        connection.close()
        await pump
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):  # the browser may be gone already
            pass

    return app


async def wait_for_disconnect(websocket: WebSocket) -> None:
    try:
        while True:
            await websocket.receive_text()  # replay takes no commands
    except WebSocketDisconnect:
        pass


app = create_app()
