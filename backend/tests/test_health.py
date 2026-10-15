from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.api import health as health_module


def test_health_reports_api_and_unconfigured_supabase(monkeypatch):
    settings = Settings(_env_file=None, ollama_model="unit-test-model")
    app.dependency_overrides[get_settings] = lambda: settings

    async def fake_check(_settings):
        return {
            "status": "unavailable",
            "connected": False,
            "model": "unit-test-model",
            "model_available": False,
            "available_models": [],
        }

    monkeypatch.setattr(health_module, "check_ollama", fake_check)
    try:
        response = TestClient(app).get("/api/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["components"]["api"]["status"] == "available"
    assert body["components"]["ollama"]["connected"] is False
    assert body["components"]["supabase"] == {"status": "not_configured", "connected": False}
