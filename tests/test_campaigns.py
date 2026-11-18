import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.campaigns import router
from app.keitaro import KeitaroClient, get_client


DRAFT = {
    "name": "Зимние скидки", "country": "de", "offer_id": 11, "domain_id": 21,
    "group_id": 31, "traffic_source_id": 41,
}
KEY = {"Idempotency-Key": "aa19e8fc-c42b-40a5-a8bb-284d43f6c1d2"}


@pytest.fixture(autouse=True)
def isolated_prefix(monkeypatch):
    monkeypatch.delenv("CAMPAIGN_NAME_PREFIX", raising=False)


@contextmanager
def api_with(handler):
    tracker = KeitaroClient("https://tracker.invalid", "unit-test", transport=httpx.MockTransport(handler))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_client] = lambda: tracker
    try:
        with TestClient(app) as api:
            yield api
    finally:
        tracker.close()


def test_options_and_offer_search_return_only_public_fields():
    def handler(request):
        assert request.method == "GET"
        route = request.url.path.removeprefix("/admin_api/v1/")
        if route == "groups":
            assert dict(request.url.params) == {"type": "campaigns"}
        item = {"id": 11, "name": "Лето", "state": "active", "notes": "private"}
        if route == "offers/11":
            return httpx.Response(200, json=item)
        if route == "offers/12":
            return httpx.Response(200, json={**item, "id": 12, "state": "deleted"})
        assert route in {"groups", "domains", "traffic_sources", "offers"}
        return httpx.Response(200, json=[item, {**item, "id": 12, "state": "deleted"}])

    with api_with(handler) as api:
        expected = [{"id": 11, "name": "Лето"}]
        assert api.get("/api/options").json() == dict.fromkeys(("domains", "groups", "traffic_sources"), expected)
        assert api.get("/api/offers", params={"q": "ЛЕТ"}).json() == expected
        assert api.get("/api/offers", params={"q": "11"}).json() == expected
        assert api.get("/api/offers", params={"q": "winter"}).json() == []
        assert api.get("/api/offers/11").json() == expected[0]
        assert api.get("/api/offers/12").status_code == 404


def test_ordered_routing_and_idempotent_repeat():
    records, calls, streams = [], [], []

    def handler(request):
        route = request.url.path.removeprefix("/admin_api/v1/")
        data = json.loads(request.content) if request.content else {}
        calls.append((request.method, route))
        if request.method == "GET" and route == "campaigns":
            return httpx.Response(200, json=records)
        if route == "offers/11":
            return httpx.Response(200, json={"id": 11, "name": "Offer"})
        if request.method == "POST" and route == "campaigns":
            assert data["state"] == "disabled"
            records.append({"id": 81, **data})
            return httpx.Response(200, json=records[0])
        if route == "streams":
            streams.append(data)
        elif route == "campaigns/81":
            assert data == {"state": "active"}
            assert len(streams) == 2
            records[0].update(data)
        else:
            pytest.fail(f"Unexpected route {route}")
        return httpx.Response(200, json={})

    with api_with(handler) as api:
        result = api.post("/api/campaigns", json=DRAFT, headers=KEY)
        assert result.status_code == 201
        assert result.json() == {"id": 81, "alias": records[0]["alias"]}
        assert records[0].items() >= {
            "domain_id": 21, "traffic_source_id": 41, "group_id": "31", "type": "position",
        }.items()
        first, fallback = streams
        assert first["campaign_id"] == fallback["campaign_id"] == 81
        assert first["position"] < fallback["position"]
        assert first["type"] == "regular" and first["schema"] == "redirect"
        assert first["action_type"] == "http" and first["action_payload"] == "https://www.google.com/"
        assert first["filters"] == [{"name": "country", "mode": "accept", "payload": ["DE"]}]
        assert fallback["type"] == "default" and fallback["schema"] == "landings"
        assert fallback["offers"] == [{"offer_id": 11, "share": 100, "state": "active"}]
        assert not fallback.get("filters")
        calls.clear()
        assert api.post("/api/campaigns", json=DRAFT, headers=KEY).json() == result.json()
        changed = api.post("/api/campaigns", json={**DRAFT, "offer_id": 12}, headers=KEY)
        assert changed.status_code == 409
        assert calls == [("GET", "campaigns"), ("GET", "campaigns")]


@pytest.mark.parametrize(("failure", "status", "archived"), [
    ("offer", 404, False), ("campaign", 422, False), ("stream", 422, True),
    ("activation", 422, True), ("archive", 502, True), ("lost_activation_reply", 201, False),
])
def test_provisioning_errors_archive_only_current_campaign(failure, status, archived):
    deletes = []

    def handler(request):
        route = request.url.path.removeprefix("/admin_api/v1/")
        if route == "offers/11":
            code = 404 if failure == "offer" else 200
            return httpx.Response(code, json={"id": 11, "name": "Offer"})
        if route == "campaigns":
            data = json.loads(request.content)
            code = 400 if failure == "campaign" else 200
            return httpx.Response(code, json={"id": 5, "alias": data["alias"]})
        if route == "streams":
            second_stream = json.loads(request.content)["position"] == 2
            code = 400 if second_stream and failure in {"stream", "archive"} else 200
            return httpx.Response(code, json={})
        assert route == "campaigns/5"
        if request.method == "GET":
            return httpx.Response(200, json={"id": 5, "state": "active"})
        if request.method == "PUT":
            if failure == "lost_activation_reply":
                raise httpx.ReadTimeout("connection lost")
            return httpx.Response(400)
        assert request.method == "DELETE"
        deletes.append(route)
        return httpx.Response(500 if failure == "archive" else 204)

    with api_with(handler) as api:
        response = api.post("/api/campaigns", json=DRAFT)
        assert response.status_code == status
        assert deletes == (["campaigns/5"] if archived else [])
        if failure == "archive":
            assert "5" in response.json()["detail"]


@pytest.mark.parametrize("invalid", [{"country": "ZZ"}, {"name": "  "}, {"offer_id": False}, {"domain_id": -2}])
def test_bad_campaign_input_preserves_tracker(invalid):
    with api_with(lambda _: pytest.fail("Invalid input called tracker")) as api:
        assert api.post("/api/campaigns", json={**DRAFT, **invalid}).status_code == 422


def test_configured_prefix_checked_before_api_calls(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_NAME_PREFIX", "test_rozhko_")
    with api_with(lambda _: pytest.fail("Wrong prefix called tracker")) as api:
        response = api.post("/api/campaigns", json=DRAFT, headers=KEY)
        assert response.status_code == 422
        assert "test_rozhko_" in response.json()["detail"]
