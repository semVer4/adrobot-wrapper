import json

import httpx
import pytest

from app.keitaro import KeitaroClient, KeitaroError, get_client


def test_stream_read_and_offer_only_update():
    streams = [{
        "id": 7, "position": 2, "filters": [{"name": "country"}],
        "landings": [{"landing_id": 4}], "comments": "keep",
        "offers": [{"id": 12, "stream_id": 7, "offer_id": 17, "share": 100,
                    "state": "disabled", "created_at": "yesterday"}],
    }]
    calls = []

    def handler(request):
        assert request.headers["Api-Key"] == "test-only-key"
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=streams)
        assert json.loads(request.content) == {
            "offers": [{"offer_id": 17, "share": 100, "state": "disabled"}]
        }
        return httpx.Response(204)

    client = KeitaroClient("https://tracker.invalid", "test-only-key", transport=httpx.MockTransport(handler))
    try:
        raw = client.list_streams(1)
        assert raw == streams
        client.save_offers(7, raw[0]["offers"])
    finally:
        client.close()
    assert calls == [
        ("GET", "/admin_api/v1/campaigns/1/streams"),
        ("PUT", "/admin_api/v1/streams/7"),
    ]


@pytest.mark.parametrize(("reply", "expected"), [
    (httpx.Response(401, text="test-only-key"), 502),
    (httpx.Response(302, headers={"Location": "https://foreign.invalid"}), 502),
    (httpx.Response(404, text="test-only-key"), 404),
    (httpx.Response(200, content=b"bad: test-only-key"), 502),
    (httpx.Response(200, json={"id": 99}), 502),
])
def test_errors_and_invalid_responses_do_not_expose_raw_details(reply, expected):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return reply

    client = KeitaroClient("https://tracker.invalid", "test-only-key", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(KeitaroError) as exc:
            client.get_offer(17)
        assert exc.value.status_code == expected
        assert "test-only-key" not in str(exc.value)
        assert seen == ["tracker.invalid"]
    finally:
        client.close()


def test_timeout_does_not_retry_the_write():
    calls = []

    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("test-only-key")

    client = KeitaroClient("https://tracker.invalid", "test-only-key", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(KeitaroError) as exc:
            client.create_stream({"name": "smoke"})
        assert exc.value.status_code == 504
        assert "test-only-key" not in str(exc.value)
        assert calls == ["POST"]
    finally:
        client.close()


def test_missing_settings(monkeypatch):
    monkeypatch.delenv("KEITARO_API_KEY", raising=False)
    monkeypatch.delenv("KEITARO_BASE_URL", raising=False)
    with pytest.raises(KeitaroError) as exc:
        next(get_client())
    assert exc.value.status_code == 503
