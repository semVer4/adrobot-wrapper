from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.database import get_engine
from app.main import app


def test_migrated_application(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + (tmp_path / "app.db").as_posix())
    monkeypatch.setenv("KEITARO_API_KEY", "")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    with TestClient(app, base_url="http://localhost") as client:
        assert "Создание кампании" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/api/campaigns/1").json() == {"campaign_id": 1, "streams": []}
        assert client.get("/api/options").status_code == 503
        foreign = client.post("/api/campaigns/1/fetch", headers={"Origin": "https://other.example"})
        assert foreign.status_code == 403
        assert client.get("/.env").status_code == 404
    get_engine().dispose()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.check(config)
