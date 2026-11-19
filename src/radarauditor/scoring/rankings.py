from typing import Literal
from uuid import UUID, uuid5

from radarauditor.collectors.identity import identity, same_business
from radarauditor.collectors.patterns import collapse_whitespace, host_of
from radarauditor.collectors.snapshot import PositionChange, RankingHistory, RawAudit, SerpResult
from radarauditor.db.models import Audit, Competitor, Keyword, Ranking
from radarauditor.scoring.checks.common import google_web_serps, same_site

CUSTOMER_SUBJECT = "customer"
type PositionKind = Literal["organic", "local_pack"]
POSITION_KINDS: tuple[PositionKind, ...] = ("organic", "local_pack")


def rankings_from_raw(
    audit_id: UUID,
    raw: RawAudit,
    keywords: list[Keyword],
    competitors: list[Competitor],
    customer_hostname: str | None,
    customer_place_name: str | None,
) -> list[Ranking]:
    searches = {_phrase_key(serp.phrase): serp for serp in google_web_serps(raw)}
    customer_host = host_of(customer_hostname)
    rows: list[Ranking] = []
    for keyword in keywords:
        serp = searches.get(_phrase_key(keyword.search_phrase)) or searches.get(
            _phrase_key(keyword.phrase)
        )
        if serp is None:
            continue
        rows.append(
            _row(audit_id, keyword, serp, CUSTOMER_SUBJECT, customer_host, customer_place_name)
        )
        rows.extend(
            _row(audit_id, keyword, serp, rival.name, host_of(rival.website), rival.name)
            for rival in competitors
        )
    return rows + grid_rankings(audit_id, raw, keywords)


def grid_rankings(audit_id: UUID, raw: RawAudit, keywords: list[Keyword]) -> list[Ranking]:
    keyword = next((item for item in keywords if item.is_primary), None) or (
        keywords[0] if keywords else None
    )
    if keyword is None:
        return []
    return [
        Ranking(
            id=uuid5(audit_id, f"{keyword.id}/{CUSTOMER_SUBJECT}/{point.key}"),
            audit_id=audit_id,
            keyword_id=keyword.id,
            subject=CUSTOMER_SUBJECT,
            organic_position=None,
            local_pack_position=point.position,
            grid_point=point.key,
            measured_at=raw.collected_at,
        )
        for point in raw.grid
    ]


def _row(
    audit_id: UUID,
    keyword: Keyword,
    serp: SerpResult,
    subject: str,
    host: str | None,
    place_name: str | None,
) -> Ranking:
    return Ranking(
        id=uuid5(audit_id, f"{keyword.id}/{subject}"),
        audit_id=audit_id,
        keyword_id=keyword.id,
        subject=subject,
        organic_position=_organic_position(serp, host),
        local_pack_position=_local_pack_position(serp, place_name),
        grid_point=None,
        measured_at=serp.measured_at,
    )


def _organic_position(serp: SerpResult, host: str | None) -> int | None:
    if host is None:
        return None
    return min((position for position, url in serp.organic if same_site(url, host)), default=None)


def _local_pack_position(serp: SerpResult, name: str | None) -> int | None:
    if not name:
        return None
    own = identity(name=name)
    return min(
        (
            position
            for position, entry in serp.local_pack
            if same_business(own, identity(name=entry))
        ),
        default=None,
    )


def _phrase_key(phrase: str) -> str:
    return collapse_whitespace(phrase).casefold()


def ranking_history(
    previous_audit: Audit,
    previous_rows: list[Ranking],
    current_rows: list[Ranking],
    phrases: dict[UUID, str],
) -> RankingHistory:
    before = _customer_rows(previous_rows)
    after = _customer_rows(current_rows)
    changes = [
        PositionChange(
            phrase=phrases[keyword_id],
            kind=kind,
            before=_position(before[keyword_id], kind),
            after=_position(after[keyword_id], kind),
        )
        for keyword_id in sorted(
            before.keys() & after.keys() & phrases.keys(), key=phrases.__getitem__
        )
        for kind in POSITION_KINDS
    ]
    return RankingHistory(
        previous_audit_id=previous_audit.id,
        previous_requested_at=previous_audit.requested_at,
        changes=changes,
    )


def _position(row: Ranking, kind: PositionKind) -> int | None:
    return row.organic_position if kind == "organic" else row.local_pack_position


def _customer_rows(rows: list[Ranking]) -> dict[UUID, Ranking]:
    return {
        row.keyword_id: row
        for row in rows
        if row.subject == CUSTOMER_SUBJECT and row.grid_point is None
    }
