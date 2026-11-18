"""Campaign provisioning and catalogs for the dashboard."""

import os
from hashlib import sha256
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator

from .keitaro import KeitaroClient, KeitaroError, get_client


router = APIRouter(prefix="/api", tags=["Кампании и справочники"])
Tracker = Annotated[KeitaroClient, Depends(get_client)]

# ISO 3166-1 alpha-2, the country identifiers used by Keitaro's country filter.
COUNTRIES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT
JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO
MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY
QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO
TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())


class CampaignInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    country: str
    offer_id: Annotated[int, Field(strict=True, gt=0)]
    domain_id: Annotated[int, Field(strict=True, gt=0)]
    group_id: Annotated[int, Field(strict=True, gt=0)]
    traffic_source_id: Annotated[int, Field(strict=True, gt=0)]

    @field_validator("country")
    @classmethod
    def validate_country(cls, value: str) -> str:
        code = value.upper()
        if code not in COUNTRIES:
            raise ValueError("Укажите двухбуквенный код страны по ISO 3166-1.")
        return code


class CatalogItem(BaseModel):
    id: PositiveInt
    name: str


class CampaignOptions(BaseModel):
    domains: list[CatalogItem]
    groups: list[CatalogItem]
    traffic_sources: list[CatalogItem]


class CreatedCampaign(BaseModel):
    id: PositiveInt
    alias: str


def _item(row: dict) -> CatalogItem:
    try:
        return CatalogItem(id=row.get("id"), name=row.get("name"))
    except ValueError:
        raise KeitaroError(502, "В справочнике Keitaro отсутствует идентификатор или название.") from None


def _items(rows: list[dict]) -> list[CatalogItem]:
    return [_item(row) for row in rows if row.get("state") not in {"deleted", "disabled", "archived"}]


@router.get("/options")
def options(client: Tracker) -> CampaignOptions:
    return CampaignOptions(
        domains=_items(client.list_domains()),
        groups=_items(client.list_campaign_groups()),
        traffic_sources=_items(client.list_traffic_sources()),
    )


@router.get("/offers")
def search_offers(client: Tracker, q: Annotated[str, Query(max_length=255)] = "") -> list[CatalogItem]:
    query = q.strip().casefold()
    return [
        item for item in _items(client.list_offers())
        if query in item.name.casefold() or query == str(item.id)
    ]


@router.get("/offers/{offer_id}")
def offer(client: Tracker, offer_id: Annotated[int, Path(gt=0)]) -> CatalogItem:
    row = client.get_offer(offer_id)
    if row.get("state") in {"deleted", "disabled", "archived"}:
        raise HTTPException(404, "Оффер недоступен в Keitaro.")
    return _item(row)


def _archive_incomplete(client: KeitaroClient, campaign_id: int) -> None:
    try:
        client.archive_campaign(campaign_id)
    except KeitaroError:
        raise KeitaroError(
            502, f"Создание не завершено. Не удалось подтвердить архивирование кампании {campaign_id}. "
                 "Проверьте её в Keitaro."
        ) from None


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
def create_campaign(
    draft: CampaignInput,
    client: Tracker,
    idempotency_key: Annotated[UUID | None, Header(alias="Idempotency-Key")] = None,
) -> CreatedCampaign:
    prefix = os.getenv("CAMPAIGN_NAME_PREFIX", "")
    if prefix and not draft.name.startswith(prefix):
        raise HTTPException(422, f"Название кампании должно начинаться с «{prefix}».")
    alias = "ar" + (idempotency_key or uuid4()).hex
    # The key identifies the submission. The fingerprint detects reuse with a different body.
    fingerprint = "adrobot:" + sha256(draft.model_dump_json().encode("utf-8")).hexdigest()
    if idempotency_key is not None:
        existing = client.find_campaign(alias)
        if existing:
            if existing.get("notes") != fingerprint:
                raise HTTPException(409, "Этот ключ запроса уже связан с другой кампанией.")
            if existing.get("state") != "active":
                raise HTTPException(409, f"Кампания {existing.get('id')} ещё не активна. Проверьте её в Keitaro.")
            return CreatedCampaign(id=existing["id"], alias=alias)

    offer(client, draft.offer_id)
    campaign_id = client.create_campaign({
        "alias": alias, "name": draft.name, "group_id": str(draft.group_id),
        "domain_id": draft.domain_id, "traffic_source_id": draft.traffic_source_id,
        "type": "position", "state": "disabled", "notes": fingerprint,
    })
    try:
        client.create_stream({
            "campaign_id": campaign_id, "name": f"{draft.country} → Google",
            "type": "regular", "position": 1, "schema": "redirect", "action_type": "http",
            "action_payload": "https://www.google.com/", "collect_clicks": True,
            "filters": [{"name": "country", "mode": "accept", "payload": [draft.country]}],
        })
        client.create_stream({
            "campaign_id": campaign_id, "name": f"Остальные → оффер {draft.offer_id}",
            "type": "default", "position": 2, "schema": "landings", "action_type": "http",
            "offers": [{"offer_id": draft.offer_id, "share": 100, "state": "active"}],
            "collect_clicks": True,
        })
    except KeitaroError:
        _archive_incomplete(client, campaign_id)
        raise

    try:
        client.activate_campaign(campaign_id)
    except KeitaroError as error:
        # Activation may have completed before a timeout or upstream 5xx.
        if error.upstream_status is None or error.upstream_status >= 500:
            try:
                current = client.get_campaign(campaign_id)
            except KeitaroError:
                raise KeitaroError(502, f"Не удалось проверить активацию кампании {campaign_id} в Keitaro.") from None
            if current.get("state") == "active":
                return CreatedCampaign(id=campaign_id, alias=alias)
        _archive_incomplete(client, campaign_id)
        raise

    return CreatedCampaign(id=campaign_id, alias=alias)
