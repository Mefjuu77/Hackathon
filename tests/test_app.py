from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import app

client = TestClient(app)


def test_index_serves_page():
    response = client.get("/")
    assert response.status_code == 200
    assert "Klient vs Help Desk" in response.text


def test_static_files_are_served():
    assert client.get("/static/app.js").status_code == 200


def test_scenarios_endpoint():
    scenarios = client.get("/api/scenarios").json()
    assert [s["id"] for s in scenarios] == ["S1", "S2", "S3", "S4"]
    assert all(s["title"] for s in scenarios)


def test_settings_defaults(monkeypatch):
    for name in ("ELEVENLABS_API_BASE", "MAX_CONVERSATION_SECONDS", "EL_MAX_DURATION_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.max_conversation_seconds == 360
    assert settings.el_max_duration_seconds == 420
    assert settings.runs_dir.name == "runs"
