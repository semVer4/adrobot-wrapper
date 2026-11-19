from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.campaigns import offer as available_offer
from app.database import get_db
from app.keitaro import KeitaroClient, get_client
from app.models import StreamOffer, StreamRecord


router = APIRouter(prefix="/api", tags=["Редактор"])
Db = Annotated[Session, Depends(get_db)]
Client = Annotated[KeitaroClient, Depends(get_client)]
Identifier = Annotated[int, Path(gt=0, le=2_147_483_647)]


class OfferAddition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offer_id: int = Field(strict=True, gt=0, le=2_147_483_647)


class OfferEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: StrictBool | None = None
    pinned: StrictBool | None = None
    share: int | None = Field(default=None, ge=0, le=100, strict=True)


@contextmanager
def write_transaction(db: Session) -> Iterator[None]:
    with db.begin():
        # SQLite has no row locks. Reserve the writer before reading any draft.
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        yield


def stream_for_update(db: Session, stream_id: int) -> StreamRecord:
    stream = db.scalar(select(StreamRecord).where(StreamRecord.id == stream_id).with_for_update())
    if stream is None:
        raise HTTPException(404, "Поток ещё не загружен.")
    return stream


def draft_changed(row: StreamOffer) -> bool:
    return not row.published or (row.share, row.pinned, row.archived) != (
        row.published_share, row.published_pinned, row.published_archived
    )


def stream_snapshot(stream: StreamRecord) -> dict:
    return {
        "id": stream.id,
        "name": stream.name,
        "dirty": any(draft_changed(row) for row in stream.offers),
        "editable": bool(stream.offers) and not stream.missing,
        "conflict": stream.conflict,
        "missing": stream.missing,
        "offers": [
            {
                "offer_id": row.offer_id, "name": row.name, "share": row.share,
                "pinned": row.pinned, "archived": row.archived,
                "changed": draft_changed(row),
                "pending_removal": row.archived and row.published and not row.published_archived,
                "pending_addition": not row.archived and (not row.published or row.published_archived),
            }
            for row in sorted(stream.offers, key=lambda row: row.offer_id)
        ],
    }


def campaign_snapshot(campaign_id: int, streams: list[StreamRecord]) -> dict:
    return {
        "campaign_id": campaign_id,
        "streams": [
            stream_snapshot(s) for s in sorted(streams, key=lambda s: s.id)
            if not s.missing or any(draft_changed(row) for row in s.offers)
        ],
    }


@dataclass(frozen=True)
class RemoteOffer:
    name: str
    share: int
    archived: bool


@dataclass(frozen=True)
class RemoteStream:
    id: int
    name: str
    offers: dict[int, RemoteOffer]


def remote_number(value: object, low: int, high: int) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 10:
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise HTTPException(502, "Keitaro вернул некорректный идентификатор или вес.")
    return value


def read_remote_streams(client: KeitaroClient, campaign_id: int) -> dict[int, RemoteStream]:
    result = {}
    for raw in client.list_streams(campaign_id):
        if not isinstance(raw, dict):
            raise HTTPException(502, "Keitaro вернул некорректные данные потока.")
        stream_id = remote_number(raw.get("id"), 1, 2_147_483_647)
        raw_offers = raw.get("offers", [])
        if stream_id in result or not isinstance(raw_offers, list):
            raise HTTPException(502, "Keitaro вернул некорректный список потоков или офферов.")
        offers = {}
        for raw_offer in raw_offers:
            if not isinstance(raw_offer, dict):
                raise HTTPException(502, "Keitaro вернул некорректные данные оффера.")
            offer_id = remote_number(raw_offer.get("offer_id"), 1, 2_147_483_647)
            if offer_id in offers:
                raise HTTPException(502, "Keitaro вернул повторяющийся оффер в потоке.")
            share = remote_number(raw_offer.get("share"), 0, 100)
            archived = str(raw_offer.get("state", "active")).lower() != "active"
            offers[offer_id] = RemoteOffer(str(raw_offer.get("name") or ""), 0 if archived else share, archived)
        result[stream_id] = RemoteStream(stream_id, str(raw.get("name") or f"Поток {stream_id}"), offers)
    return result


def offer_name(client: KeitaroClient, offer_id: int, cache: dict[int, str]) -> str:
    if offer_id not in cache:
        try:
            details = client.get_offer(offer_id)
        except HTTPException as exc:
            if getattr(exc, "upstream_status", exc.status_code) != 404:
                raise
            details = {}
        cache[offer_id] = str(details.get("name") or f"Оффер {offer_id}")
    return cache[offer_id]


def published_weights(stream: StreamRecord) -> dict[int, int]:
    return {
        row.offer_id: row.published_share for row in stream.offers
        if row.published and not row.published_archived
    }


def draft_weights(stream: StreamRecord) -> dict[int, int]:
    return {row.offer_id: row.share for row in stream.offers if not row.archived}


def remote_weights(remote: RemoteStream) -> dict[int, int]:
    return {i: row.share for i, row in remote.offers.items() if not row.archived}


def restore_baseline(row: StreamOffer) -> None:
    row.removal_snapshot = None
    row.share = row.published_share
    row.archived = row.published_archived
    row.pinned = row.published_pinned


def reconcile(stream: StreamRecord, remote: RemoteStream, client: KeitaroClient, names: dict[int, str]) -> None:
    dirty = any(draft_changed(row) for row in stream.offers)
    actual = remote_weights(remote)
    stream.missing = False
    stream.name = remote.name
    stream.conflict = dirty and actual != draft_weights(stream) and (
        stream.conflict or actual != published_weights(stream)
    )
    local = {row.offer_id: row for row in stream.offers}
    for offer_id, entry in remote.offers.items():
        row = local.get(offer_id)
        if row is None:
            row = StreamOffer(offer_id=offer_id, share=0, pinned=False, archived=True)
            stream.offers.append(row)
        row.name = entry.name or row.name or offer_name(client, offer_id, names)
        names[offer_id] = row.name
        row.published = True
        row.published_share = entry.share
        row.published_archived = entry.archived
        if entry.archived:
            row.removal_snapshot = None
        row.published_pinned = bool(row.published_pinned) and not entry.archived
        if not dirty:
            restore_baseline(row)

    for offer_id, row in local.items():
        if offer_id not in remote.offers and row.published:
            row.removal_snapshot = None
            row.published_share = 0
            row.published_archived = True
            row.published_pinned = False
            if not dirty:
                restore_baseline(row)

    if not any(draft_changed(row) for row in stream.offers):
        stream.conflict = False


@router.get("/campaigns/{campaign_id}")
def read_campaign(campaign_id: Identifier, db: Db):
    statement = select(StreamRecord).where(StreamRecord.campaign_id == campaign_id)
    return campaign_snapshot(campaign_id, list(db.scalars(statement)))


@router.post("/campaigns/{campaign_id}/fetch")
def fetch_campaign(campaign_id: Identifier, db: Db, client: Client):
    with write_transaction(db):
        if db.get_bind().dialect.name == "postgresql":
            # Also serialize first fetches, when there are no stream rows to lock yet.
            db.execute(text("SELECT pg_advisory_xact_lock(124702, :id)"), {"id": campaign_id})
        statement = select(StreamRecord).where(StreamRecord.campaign_id == campaign_id)
        streams = {s.id: s for s in db.scalars(statement.order_by(StreamRecord.id).with_for_update())}
        remote = read_remote_streams(client, campaign_id)
        names = {row.offer_id: row.name for s in streams.values() for row in s.offers}
        for stream_id, entry in remote.items():
            if stream_id not in streams:
                streams[stream_id] = StreamRecord(id=stream_id, campaign_id=campaign_id, name=entry.name)
                db.add(streams[stream_id])
            reconcile(streams[stream_id], entry, client, names)
        for stream_id in streams.keys() - remote.keys():
            streams[stream_id].missing = True
        return campaign_snapshot(campaign_id, list(streams.values()))


def require_editable(stream: StreamRecord) -> None:
    if stream.missing:
        raise HTTPException(409, "Поток больше не найден в кампании.")
    if not stream.offers:
        raise HTTPException(409, "В этом потоке нет офферов для управления.")


def distribute(stream: StreamRecord, *, keep_share_of: StreamOffer | None = None) -> None:
    active = [row for row in stream.offers if not row.archived]
    if not active:
        raise HTTPException(422, "В потоке должен остаться хотя бы один активный оффер.")
    remaining = 100 - sum(row.share for row in active if row.pinned or row is keep_share_of)
    free = sorted(
        (row for row in active if not row.pinned and row is not keep_share_of),
        key=lambda row: row.offer_id,
    )
    if keep_share_of is not None and (remaining < 0 or (not free and remaining != 0)):
        raise HTTPException(422, "Прежняя доля не помещается. Измените закреплённые доли других офферов.")
    if remaining < 0:
        raise HTTPException(422, "Сумма закреплённых весов превышает 100%.")
    if not free:
        if remaining:
            raise HTTPException(422, "Закреплённые веса должны дать 100%. Открепите один из офферов.")
        return
    share, extra = divmod(remaining, len(free))
    for index, row in enumerate(free):
        row.share = share + int(index < extra)


def undo_removal(stream: StreamRecord, row: StreamOffer) -> None:
    before = row.removal_snapshot
    if before is None:
        # Drafts removed before the migration only have their published baseline.
        before = {
            str(r.offer_id): [r.published_share, r.published_pinned]
            for r in stream.offers if r.published and not r.published_archived
        }
    row.archived = False
    row.share, row.pinned = before[str(row.offer_id)]
    active = {str(r.offer_id): r for r in stream.offers if not r.archived}
    if active.keys() == before.keys() and all(
        r.pinned == before[key][1] and (not r.pinned or r.share == before[key][0])
        for key, r in active.items()
    ):
        # Removal also redistributed the other offers, including unequal, unpinned shares.
        for key, r in active.items():
            r.share = before[key][0]
    else:
        # Keep later edits to the other offers and reserve the restored share for this calculation.
        distribute(stream, keep_share_of=row)
    row.removal_snapshot = None


@router.post("/streams/{stream_id}/offers")
def add_offer(stream_id: Identifier, body: OfferAddition, db: Db, client: Client):
    with write_transaction(db):
        stream = stream_for_update(db, stream_id)
        require_editable(stream)
        if any(row.offer_id == body.offer_id for row in stream.offers):
            raise HTTPException(409, "Оффер уже есть в потоке или его архиве.")
        details = available_offer(client, body.offer_id)
        stream.offers.append(StreamOffer(
            offer_id=body.offer_id, name=details.name,
            share=0, archived=False, pinned=False, published=False,
            published_share=0, published_pinned=False, published_archived=False,
        ))
        distribute(stream)
        return stream_snapshot(stream)


@router.patch("/streams/{stream_id}/offers/{offer_id}")
def edit_offer(stream_id: Identifier, offer_id: Identifier, body: OfferEdit, db: Db):
    with write_transaction(db):
        stream = stream_for_update(db, stream_id)
        require_editable(stream)
        row = next((r for r in stream.offers if r.offer_id == offer_id), None)
        if row is None:
            raise HTTPException(404, "Оффер не найден в потоке.")
        old_state = row.archived, row.pinned, row.share
        if not body.model_fields_set or any(getattr(body, key) is None for key in body.model_fields_set):
            raise HTTPException(422, "Укажите изменение оффера.")
        if body.archived is not None:
            if len(body.model_fields_set) != 1:
                raise HTTPException(422, "Архивация и восстановление выполняются отдельно от изменения веса.")
            if body.archived == row.archived:
                return stream_snapshot(stream)
            if not body.archived and row.published and not row.published_archived:
                undo_removal(stream, row)
                return stream_snapshot(stream)
            if body.archived and row.published and not row.published_archived:
                row.removal_snapshot = {
                    str(r.offer_id): [r.share, r.pinned] for r in stream.offers if not r.archived
                }
            row.archived = body.archived
            if row.archived:
                row.pinned = False
                row.share = 0
                if not row.published:
                    stream.offers.remove(row)
        else:
            if row.archived:
                raise HTTPException(422, "Перед изменением веса восстановите оффер.")
            if body.share is not None:
                if body.pinned is False:
                    raise HTTPException(422, "Точный вес можно задать только закреплённому офферу.")
                row.share = body.share
            row.pinned = True if body.pinned is None else body.pinned
        if old_state != (row.archived, row.pinned, row.share):
            distribute(stream)
        return stream_snapshot(stream)


@router.post("/streams/{stream_id}/cancel")
def cancel_stream(stream_id: Identifier, db: Db):
    with write_transaction(db):
        stream = stream_for_update(db, stream_id)
        for row in list(stream.offers):
            if row.published:
                restore_baseline(row)
            else:
                stream.offers.remove(row)
        stream.conflict = False
        return stream_snapshot(stream)


def accept_draft(stream: StreamRecord) -> None:
    for row in list(stream.offers):
        if row.archived and not row.published:
            stream.offers.remove(row)
            continue
        row.published = True
        row.removal_snapshot = None
        row.published_share = row.share
        row.published_pinned = row.pinned
        row.published_archived = row.archived
    stream.conflict = False
    stream.missing = False


@router.post("/streams/{stream_id}/push")
def push_stream(stream_id: Identifier, db: Db, client: Client):
    with write_transaction(db):
        stream = stream_for_update(db, stream_id)
        if not any(draft_changed(row) for row in stream.offers):
            return stream_snapshot(stream)
        remote = read_remote_streams(client, stream.campaign_id).get(stream.id)
        if remote is None:
            stream.missing = True
            error = "Поток больше не найден в Keitaro. Черновик сохранён."
        else:
            actual = remote_weights(remote)
            desired = draft_weights(stream)
            if actual == desired:
                # A previous PUT may have succeeded despite a lost response.
                accept_draft(stream)
                return stream_snapshot(stream)
            if not stream.conflict and actual == published_weights(stream):
                if not desired or sum(desired.values()) != 100:
                    raise HTTPException(422, "Активные офферы должны составлять 100%.")
                payload = [
                    {"offer_id": i, "share": desired[i], "state": "active"} for i in sorted(desired)
                ]
                client.save_offers(stream.id, payload)
                accept_draft(stream)
                return stream_snapshot(stream)
            reconcile(stream, remote, client, {})
            error = "Офферы в Keitaro изменились. Отмените черновик, чтобы загрузить актуальные веса."
    # Commit the latest published baseline before reporting the conflict.
    raise HTTPException(409, error)
