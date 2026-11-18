from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.database import get_engine
from app.editor import router
from app.keitaro import get_client
from app.models import Base


def link(offer_id, share):
    return {"offer_id": offer_id, "share": share, "state": "active"}


class FakeTracker:
    def __init__(self):
        self.streams = [
            {"id": "100", "name": "Основной", "offers": [link(22, 40), link(11, 60)]},
            {"id": 101, "name": "Редирект", "offers": []},
        ]
        self.writes = []
        self.fail_write = False
        self.lose_reply = False

    def list_streams(self, campaign_id):
        assert campaign_id == 1
        return deepcopy(self.streams)

    def get_offer(self, offer_id):
        if offer_id == 999:
            raise HTTPException(404, "Оффер не найден.")
        return {"id": offer_id, "name": f"Оффер {offer_id}"}

    def save_offers(self, stream_id, offers):
        assert stream_id == 100
        self.writes.append(deepcopy(offers))
        if self.fail_write:
            raise HTTPException(502, "Нет связи.")
        self.streams[0]["offers"] = deepcopy(offers)
        if self.lose_reply:
            raise HTTPException(504, "Нет ответа.")


FETCH = "/api/campaigns/1/fetch"
LOCAL = "/api/campaigns/1"
STREAM = "/api/streams/100"
OFFERS = STREAM + "/offers"


@pytest.fixture
def editor(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + (tmp_path / "editor.db").as_posix())
    engine = get_engine()
    Base.metadata.create_all(engine)
    tracker = FakeTracker()
    api = FastAPI()
    api.include_router(router)
    api.dependency_overrides[get_client] = lambda: tracker
    with TestClient(api) as client:
        yield client, tracker
    engine.dispose()


def rows(stream):
    return {row["offer_id"]: row for row in stream["offers"]}


def weights(stream):
    return {i: row["share"] for i, row in rows(stream).items() if not row["archived"]}


def test_fetch_and_cancel_preserve_original_split(editor):
    client, tracker = editor
    assert client.get(LOCAL).json()["streams"] == []
    original, redirect = client.post(FETCH).json()["streams"]
    assert weights(original) == {11: 60, 22: 40}
    assert rows(original)[11]["name"] == "Оффер 11"
    assert original["editable"] and not original["dirty"]
    assert not redirect["editable"] and redirect["offers"] == []
    assert client.post("/api/streams/101/offers", json={"offer_id": 33}).status_code == 409
    added = client.post(OFFERS, json={"offer_id": 33}).json()
    assert weights(added) == {11: 34, 22: 33, 33: 33}
    assert rows(added)[33]["pending_addition"]
    assert client.get(LOCAL).json()["streams"][0] == added
    assert client.post(FETCH).json()["streams"][0] == added
    assert client.post(STREAM + "/cancel").json() == original
    assert tracker.writes == []


def test_publish_archive_and_restore(editor):
    client, tracker = editor
    client.post(FETCH)
    draft = client.patch(OFFERS + "/22", json={"archived": True}).json()
    assert weights(draft) == {11: 100}
    assert rows(draft)[22]["pending_removal"] and rows(draft)[22]["share"] == 0
    assert client.patch(OFFERS + "/11", json={"archived": True}).status_code == 422
    assert client.get(LOCAL).json()["streams"][0] == draft
    published = client.post(STREAM + "/push").json()
    assert not published["dirty"] and not rows(published)[22]["pending_removal"]
    assert tracker.writes == [[link(11, 100)]]
    assert client.post(FETCH).json()["streams"][0] == published
    restored = client.patch(OFFERS + "/22", json={"archived": False}).json()
    assert weights(restored) == {11: 50, 22: 50}
    assert rows(restored)[22]["pending_addition"]
    assert client.post(STREAM + "/push").status_code == 200
    assert tracker.writes[-1] == [link(11, 50), link(22, 50)]


def test_removing_unpublished_offer_makes_it_available_again(editor):
    client, tracker = editor
    tracker.streams[0]["offers"] = [link(11, 100)]
    original = client.post(FETCH).json()["streams"][0]
    added = client.post(OFFERS, json={"offer_id": 33}).json()
    assert rows(added)[11]["changed"] and rows(added)[33]["changed"]
    assert client.patch(OFFERS + "/33", json={"archived": True}).json() == original
    assert client.post(OFFERS, json={"offer_id": 33}).status_code == 200
    assert tracker.writes == []


def test_pin_exact_weights_validate_and_cancel(editor):
    client, tracker = editor
    client.post(FETCH)
    client.post(OFFERS, json={"offer_id": 33})
    fixed = client.patch(OFFERS + "/22", json={"pinned": True, "share": 75}).json()
    assert weights(fixed) == {11: 13, 22: 75, 33: 12}
    assert client.patch(OFFERS + "/11", json={"share": 40}).status_code == 422
    assert client.get(LOCAL).json()["streams"][0] == fixed
    assert client.patch(OFFERS + "/11", json={"share": 5}).status_code == 200
    assert client.patch(OFFERS + "/33", json={"share": 19}).status_code == 422
    saved = client.post(STREAM + "/push").json()
    assert weights(saved) == {11: 5, 22: 75, 33: 20}
    assert rows(saved)[22]["pinned"]
    assert client.post(FETCH).json()["streams"][0] == saved
    free = client.patch(OFFERS + "/22", json={"pinned": False}).json()
    assert weights(free) == {11: 5, 22: 48, 33: 47}
    assert client.post(STREAM + "/cancel").json() == saved
    assert len(tracker.writes) == 1


def test_fetch_external_deletion_can_restore_all_archived(editor):
    client, tracker = editor
    client.post(FETCH)
    tracker.streams[0]["offers"] = []
    archived = client.post(FETCH).json()["streams"][0]
    assert archived["editable"] and not archived["dirty"]
    assert all(row["archived"] and row["share"] == 0 for row in archived["offers"])
    restored = client.patch(OFFERS + "/22", json={"archived": False}).json()
    assert weights(restored) == {22: 100}
    assert client.post(STREAM + "/push").status_code == 200
    assert tracker.writes == [[link(22, 100)]]


@pytest.mark.parametrize("manual_fetch", [False, True])
def test_conflict_keeps_draft_and_cancel_restores_latest_remote(editor, manual_fetch):
    client, tracker = editor
    client.post(FETCH)
    draft = client.post(OFFERS, json={"offer_id": 33}).json()
    tracker.streams[0]["offers"] = [link(11, 100)]
    if manual_fetch:
        refreshed = client.post(FETCH).json()["streams"][0]
        assert refreshed["conflict"] and weights(refreshed) == weights(draft)
    assert client.post(STREAM + "/push").status_code == 409
    retained = client.get(LOCAL).json()["streams"][0]
    assert retained["dirty"] and retained["conflict"] and weights(retained) == weights(draft)
    assert tracker.writes == []
    cancelled = client.post(STREAM + "/cancel").json()
    assert weights(cancelled) == {11: 100}
    assert not cancelled["dirty"] and not cancelled["conflict"]
    assert rows(cancelled)[22]["archived"] and 33 not in rows(cancelled)


def test_missing_dirty_stream_is_recoverable(editor):
    client, tracker = editor
    client.post(FETCH)
    draft = client.post(OFFERS, json={"offer_id": 33}).json()
    stream = tracker.streams.pop(0)
    assert client.post(STREAM + "/push").status_code == 409
    missing = client.post(FETCH).json()["streams"][0]
    assert missing["missing"] and weights(missing) == weights(draft)
    assert client.patch(OFFERS + "/11", json={"share": 25}).status_code == 409
    tracker.streams.insert(0, stream)
    assert client.post(FETCH).json()["streams"][0] == draft


@pytest.mark.parametrize("lost_reply", [False, True])
def test_failed_write_can_be_retried(editor, lost_reply):
    client, tracker = editor
    client.post(FETCH)
    draft = client.patch(OFFERS + "/22", json={"share": 25}).json()
    tracker.lose_reply = lost_reply
    tracker.fail_write = not lost_reply
    assert client.post(STREAM + "/push").status_code == (504 if lost_reply else 502)
    assert client.get(LOCAL).json()["streams"][0] == draft
    tracker.lose_reply = tracker.fail_write = False
    saved = client.post(STREAM + "/push").json()
    assert not saved["dirty"] and weights(saved) == {11: 75, 22: 25}
    assert len(tracker.writes) == (1 if lost_reply else 2)
    assert client.post(STREAM + "/push").json() == saved


def test_rejects_invalid_edits_and_rolls_back_broken_fetch(editor):
    client, tracker = editor
    original = client.post(FETCH).json()
    assert client.post(OFFERS, json={"offer_id": 11}).status_code == 409
    assert client.post(OFFERS, json={"offer_id": 999}).status_code == 404
    assert client.post(OFFERS, json={"offer_id": True}).status_code == 422
    for body in ({}, {"share": None}, {"share": 101}, {"share": 1.5}, {"share": True},
                 {"archived": True, "share": 10}, {"share": 10, "pinned": False}):
        assert client.patch(OFFERS + "/11", json=body).status_code == 422
    tracker.streams[0]["name"] = "Изменено"
    tracker.streams[1]["offers"] = [link(1, 300)]
    assert client.post(FETCH).status_code == 502
    assert client.get(LOCAL).json() == original


def test_concurrent_draft_additions_both_survive(editor):
    client, _ = editor
    client.post(FETCH)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda i: client.post(OFFERS, json={"offer_id": i}), (33, 44)))
    assert all(response.status_code == 200 for response in results)
    assert weights(client.get(LOCAL).json()["streams"][0]) == {11: 25, 22: 25, 33: 25, 44: 25}
