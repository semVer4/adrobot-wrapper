"""Synchronous client for Keitaro's Admin API v1."""

import os
from collections.abc import Iterator
from typing import Any

import httpx
from fastapi import HTTPException


class KeitaroError(HTTPException):
    def __init__(self, status_code: int, detail: str, upstream_status: int | None = None):
        super().__init__(status_code=status_code, detail=detail)
        self.upstream_status = upstream_status


def _bad_response() -> KeitaroError:
    return KeitaroError(502, "Keitaro вернул ответ неожиданного формата.")


class KeitaroClient:
    def __init__(
        self, base_url: str, api_key: str, *, transport: httpx.BaseTransport | None = None
    ):
        try:
            origin = httpx.URL(base_url.strip())
        except httpx.InvalidURL:
            raise KeitaroError(503, "Проверьте настройки адреса Keitaro на сервере.") from None
        if (
            origin.scheme not in {"https", "http"} or not origin.host or origin.userinfo
            or origin.path not in {"", "/"} or origin.query or origin.fragment
            or not api_key.strip() or not api_key.isascii() or not api_key.isprintable()
        ):
            raise KeitaroError(503, "Проверьте настройки подключения к Keitaro на сервере.")
        self._http = httpx.Client(
            base_url=origin.join("/admin_api/v1/"),
            headers={"Api-Key": api_key.strip(), "Accept": "application/json"},
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=5.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise KeitaroError(504, "Keitaro не ответил вовремя.") from None
        except httpx.RequestError:
            raise KeitaroError(502, "Не удалось подключиться к Keitaro.") from None
        if response.is_success:
            return response
        code = response.status_code
        if code == 404:
            raise KeitaroError(404, "Объект не найден в Keitaro.", code)
        if code in {400, 406, 409, 422}:
            raise KeitaroError(409 if code == 409 else 422, "Keitaro отклонил параметры запроса.", code)
        if code in {401, 402, 403}:
            raise KeitaroError(502, "Keitaro отклонил доступ к Admin API. Проверьте ключ и права.", code)
        if code == 429:
            raise KeitaroError(503, "Keitaro временно ограничил частоту запросов.", code)
        raise KeitaroError(502, "Не удалось выполнить запрос к Keitaro.", code)

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        try:
            return response.json()
        except (ValueError, UnicodeError):
            raise _bad_response() from None

    def _list(self, path: str, **params: str | int) -> list[dict]:
        result = self._json("GET", path, params=params)
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise _bad_response()
        return result

    def list_domains(self) -> list[dict]:
        return self._list("domains")

    def list_campaign_groups(self) -> list[dict]:
        return self._list("groups", type="campaigns")

    def list_traffic_sources(self) -> list[dict]:
        return self._list("traffic_sources")

    def list_offers(self) -> list[dict]:
        return self._list("offers")

    def get_offer(self, offer_id: int) -> dict:
        data = self._json("GET", f"offers/{offer_id}")
        if not isinstance(data, dict) or str(data.get("id")) != str(offer_id):
            raise _bad_response()
        return data

    def list_streams(self, campaign_id: int) -> list[dict]:
        return self._list(f"campaigns/{campaign_id}/streams")

    def save_offers(self, stream_id: int, offers: list[dict]) -> None:
        writable = [
            {field: item[field] for field in ("offer_id", "share", "state") if field in item}
            for item in offers
        ]
        self._request("PUT", f"streams/{stream_id}", json={"offers": writable})

    def find_campaign(self, alias: str) -> dict | None:
        offset = 0
        page_size = 100
        while True:
            page = self._list("campaigns", offset=offset, limit=page_size)
            for campaign in page:
                if campaign.get("alias") == alias:
                    return campaign
            if len(page) < page_size:
                return None
            offset += len(page)

    def create_campaign(self, settings: dict) -> int:
        result = self._json("POST", "campaigns", json=settings)
        if not isinstance(result, dict) or result.get("alias") != settings["alias"]:
            raise _bad_response()
        campaign_id = result.get("id")
        if type(campaign_id) is not int or campaign_id <= 0:
            raise _bad_response()
        return campaign_id

    def create_stream(self, settings: dict) -> None:
        self._request("POST", "streams", json=settings)

    def get_campaign(self, campaign_id: int) -> dict:
        result = self._json("GET", f"campaigns/{campaign_id}")
        if not isinstance(result, dict) or result.get("id") != campaign_id:
            raise _bad_response()
        return result

    def activate_campaign(self, campaign_id: int) -> None:
        self._request("PUT", f"campaigns/{campaign_id}", json={"state": "active"})

    def archive_campaign(self, campaign_id: int) -> None:
        self._request("DELETE", f"campaigns/{campaign_id}")


def get_client() -> Iterator[KeitaroClient]:
    client = KeitaroClient(os.getenv("KEITARO_BASE_URL", ""), os.getenv("KEITARO_API_KEY", ""))
    try:
        yield client
    finally:
        client.close()
