from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT
from app.content import SCENARIOS

STATIC_DIR = PROJECT_ROOT / "static"

app = FastAPI(title="Customer vs Help Desk")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/scenarios")
async def list_scenarios() -> list[dict]:
    return [{"id": s.id, "title": s.title} for s in SCENARIOS.values()]
