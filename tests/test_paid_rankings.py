import json
import math
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from pydantic import SecretStr

from radarauditor.collectors import paid_rankings
from radarauditor.collectors.quota import SourceGate
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source, SourcesSettings
from radarauditor.errors import (
    CollectionError,
    DailyCapReachedError,
    MonthlyCostCapReachedError,
    SourceDisabledError,
)

pytestmark = pytest.mark.usefixtures("fast_retries")

BASE = paid_rankings.API_BASE_URL
VOLUME_URL = f"{paid_rankings.API_BASE_URL}{paid_rankings.VOLUME_PATH}"


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def client(collector_settings: Settings, open_gate: SourceGate) -> paid_rankings.DataForSeoClient:
    client = paid_rankings.DataForSeoClient(_paid_settings(collector_settings), open_gate)
    client.sleep = _no_sleep
    return client


def _paid_settings(collector_settings: Settings) -> Settings:
    return collector_settings.model_copy(
        update={
            "dataforseo_login": "login@example.de",
            "dataforseo_password": SecretStr("secret"),
        }
    )


def _envelope(
    result: list[dict[str, object]] | None, task_status: int = 20000, cost: float = 0.0
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status_code": 20000,
            "status_message": "Ok.",
            "cost": cost,
            "tasks": [{"status_code": task_status, "status_message": "x", "result": result}],
        },
    )


class Ledger:
    def __init__(self, spent: Decimal = Decimal(0)) -> None:
        self.spent = spent
        self.added: list[tuple[str, Decimal]] = []
        self.audited: list[tuple[UUID, str, str, int, Decimal]] = []

    def add_audit_cost(
        self,
        audit_id: UUID,
        source: str,
        service: str,
        *,
        calls: int,
        cost: Decimal,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        self.audited.append((audit_id, source, service, calls, cost))

    def month_source_cost(self, source: str) -> Decimal:
        return self.spent

    def add_source_cost(self, source: str, cost: Decimal) -> Decimal:
        self.added.append((source, cost))
        self.spent += cost
        return self.spent


ORGANIC_RESULT = [
    {
        "items": [
            {"type": "local_pack", "rank_group": 1, "title": "Dachdeckerei Hollenbeck GmbH"},
            {"type": "local_pack", "rank_group": 2, "title": "Bedachungen Overmann"},
            {"type": "organic", "rank_group": 2, "domain": "www.gelbeseiten.de"},
            {"type": "organic", "rank_group": 1, "domain": "www.dachdecker-hollenbeck.de"},
            {"type": "organic", "rank_group": 3, "domain": "dachdecker-hollenbeck.de"},
            {"type": "organic", "rank_group": 4, "domain": "dach-kemper.de"},
            {
                "type": "ai_overview",
                "rank_group": 1,
                "items": [
                    {
                        "type": "ai_overview_element",
                        "text": "Empfohlen wird oft Dach Service Kemper in Münster.",
                        "references": [{"source": "Gelbe Seiten", "domain": "gelbeseiten.de"}],
                    }
                ],
            },
            {
                "type": "people_also_ask",
                "rank_group": 1,
                "items": [
                    {"type": "people_also_ask_element", "title": "Was kostet ein Dachdecker?"},
                    {"type": "people_also_ask_element", "title": "Was kostet ein Dachdecker?"},
                ],
            },
        ]
    }
]


def test_client_requires_credentials(collector_settings: Settings, open_gate: SourceGate) -> None:
    with pytest.raises(CollectionError, match="credentials"):
        paid_rankings.DataForSeoClient(collector_settings, open_gate)
    assert paid_rankings.is_configured(collector_settings) is False
    assert paid_rankings.is_configured(_paid_settings(collector_settings)) is True


def test_client_requires_the_app_switch(collector_settings: Settings) -> None:
    sources = SourcesSettings.model_validate({"dataforseo": {"enabled": False}})

    with pytest.raises(SourceDisabledError, match="dataforseo"):
        paid_rankings.DataForSeoClient(
            _paid_settings(collector_settings), SourceGate(sources, lambda s, c: True)
        )


async def test_post_stops_at_the_daily_cap(collector_settings: Settings) -> None:
    gate = SourceGate(SourcesSettings(), lambda source, cap: False)
    capped = paid_rankings.DataForSeoClient(_paid_settings(collector_settings), gate)

    with pytest.raises(DailyCapReachedError, match="daily cap of 1000"):
        await capped.post(paid_rankings.VOLUME_PATH, {"keyword": "Dachdecker"})
    assert gate.exhausted == {Source.DATAFORSEO}


def _created(*ids: str, cost: float = 0.0012, status: int = 20100) -> httpx.Response:
    tasks = [
        {"id": task_id, "status_code": status, "status_message": "Task Created.", "cost": cost}
        for task_id in ids
    ]
    body = {"status_code": 20000, "status_message": "Ok.", "cost": cost * len(ids), "tasks": tasks}
    return httpx.Response(200, json=body)


def _ready(*ids: str) -> httpx.Response:
    result = [{"id": task_id} for task_id in ids]
    body = {"status_code": 20000, "tasks": [{"status_code": 20000, "result": result}]}
    return httpx.Response(200, json=body)


def _balance(amount: float) -> httpx.Response:
    result = [{"money": {"balance": amount}}]
    body = {"status_code": 20000, "tasks": [{"status_code": 20000, "result": result}]}
    return httpx.Response(200, json=body)


def _tasks(base: str, results: dict[str, list[dict[str, object]] | None]) -> respx.Route:
    post = respx.post(f"{BASE}{base}/task_post").mock(return_value=_created(*results))
    respx.get(f"{BASE}{base}/tasks_ready").mock(return_value=_ready(*results))
    for task_id, result in results.items():
        respx.get(f"{BASE}{base}/task_get/advanced/{task_id}").mock(return_value=_envelope(result))
    return post


@respx.mock
async def test_search_google_posts_priority_tasks_and_maps_the_items(
    client: paid_rankings.DataForSeoClient,
) -> None:
    post = _tasks(paid_rankings.ORGANIC_TASKS, {"a": ORGANIC_RESULT})

    batch = await paid_rankings.search_google(client, ["Dachdecker Münster"], 51.9607, 7.6261)

    assert batch.failed == []
    assert batch.reason is None
    result = batch.results[0]
    assert result.phrase == "Dachdecker Münster"
    assert (result.route, result.engine) == ("dataforseo", "google")
    assert result.organic == [
        (1, "dachdecker-hollenbeck.de"),
        (2, "gelbeseiten.de"),
        (4, "dach-kemper.de"),
    ]
    assert result.local_pack == [(1, "Dachdeckerei Hollenbeck GmbH"), (2, "Bedachungen Overmann")]
    assert result.ai_overview_present is True
    assert result.ai_overview_mentions == ["Gelbe Seiten", "Dach Service Kemper"]
    assert result.people_also_ask == ["Was kostet ein Dachdecker?"]
    request = post.calls[0].request
    assert json.loads(request.content) == [
        {
            "keyword": "Dachdecker Münster",
            "se_domain": "google.de",
            "location_coordinate": "51.9607000,7.6261000,200",
            "language_code": "de",
            "depth": 30,
            "priority": 2,
        }
    ]
    assert request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_search_google_reads_ads_and_knowledge_panel(
    client: paid_rankings.DataForSeoClient,
) -> None:
    items = [
        {"type": "paid", "rank_group": 1, "domain": "www.dach-kemper.de"},
        {"type": "paid", "rank_group": 2, "title": "Bedachungen Overmann"},
        {"type": "knowledge_graph", "rank_group": 1, "title": "Dachdeckerei Hollenbeck"},
        {"type": "organic", "rank_group": 1, "domain": "dachdecker-hollenbeck.de"},
    ]
    _tasks(paid_rankings.ORGANIC_TASKS, {"a": [{"items": items}]})

    batch = await paid_rankings.search_google(client, ["Dachdeckerei Hollenbeck"], 51.9, 7.6)

    assert batch.results[0].ads == ["dach-kemper.de", "Bedachungen Overmann"]
    assert batch.results[0].knowledge_panel is True


@respx.mock
async def test_search_google_reads_the_site_behind_google_redirect_links(
    client: paid_rankings.DataForSeoClient,
) -> None:
    redirect = "https://google.de/goto?url=CAESXw&gsessionid=abc"
    items = [
        {
            "type": "organic",
            "rank_group": 1,
            "domain": "google.de",
            "url": redirect,
            "breadcrumb": "http://www.malatelier-muenster.de",
        },
        {
            "type": "organic",
            "rank_group": 2,
            "domain": "google.de",
            "url": redirect,
            "breadcrumb": "https://kunstimatelier.de › wir-ueber-uns",
        },
        {
            "type": "organic",
            "rank_group": 3,
            "domain": "google.de",
            "url": redirect,
            "breadcrumb": "Ca. 90 Follower",
        },
        {"type": "organic", "rank_group": 4, "domain": "www.katjakarsch.de"},
        {"type": "paid", "rank_group": 1, "domain": "google.de", "url": redirect},
        {"type": "related_searches", "rank_group": 1, "items": ["Kunstgalerie Münster"]},
    ]
    _tasks(paid_rankings.ORGANIC_TASKS, {"a": [{"items": items}]})

    batch = await paid_rankings.search_google(client, ["künstlerin münster"], 51.96, 7.62)

    assert batch.failed == []
    assert batch.results[0].organic == [
        (1, "malatelier-muenster.de"),
        (2, "kunstimatelier.de"),
        (4, "katjakarsch.de"),
    ]
    assert batch.results[0].ads == []


@respx.mock
async def test_redirect_links_are_resolved_by_parameter_then_by_site_name(
    client: paid_rankings.DataForSeoClient,
) -> None:
    opaque = "https://google.de/goto?url=CAESXw&gsessionid=abc"
    items = [
        {
            "type": "organic",
            "rank_group": 1,
            "domain": "google.de",
            "url": "https://google.de/goto?url=https%3A%2F%2Fwww.atelier-eins.de%2Fstart&x=1",
            "breadcrumb": "vor 14 Jahren",
        },
        {
            "type": "organic",
            "rank_group": 2,
            "domain": "google.de",
            "url": "https://google.de/url?q=https://atelier-zwei.de/&sa=U",
        },
        {
            "type": "organic",
            "rank_group": 3,
            "domain": "google.de",
            "url": opaque,
            "breadcrumb": "vor 14 Jahren",
            "website_name": "digital-kunst-kirchhof.de",
        },
        {
            "type": "organic",
            "rank_group": 4,
            "domain": "google.de",
            "url": opaque,
            "website_name": "Art & Wine Magazine",
        },
        {"type": "organic", "rank_group": 5, "domain": "www.katjakarsch.de"},
    ]
    _tasks(paid_rankings.ORGANIC_TASKS, {"a": [{"items": items}]})

    batch = await paid_rankings.search_google(client, ["kunst"], None, None)

    assert batch.results[0].organic == [
        (1, "atelier-eins.de"),
        (2, "atelier-zwei.de"),
        (3, "digital-kunst-kirchhof.de"),
        (5, "katjakarsch.de"),
    ]


STORED_ANSWERS = Path(__file__).parents[1] / "work" / "belege_dataforseo_2026-11-13"


def _stored_items(name: str) -> list[dict[str, object]]:
    document = json.loads((STORED_ANSWERS / name).read_text(encoding="utf-8"))
    return document["tasks"][0]["result"]


@pytest.mark.skipif(not STORED_ANSWERS.is_dir(), reason="stored DataForSEO answers are absent")
@pytest.mark.parametrize(
    "name",
    [
        "11130748-3135-0139-0000-04ec62a6e4b8.json",
        "11130748-3135-0139-0000-07cb706b08dd.json",
        "11130748-3135-0139-0000-0e57cd5b53e1.json",
    ],
)
def test_stored_answers_keep_the_rank_group_of_every_organic_entry(name: str) -> None:
    result = _stored_items(name)
    ranks = [
        item["rank_group"] for row in result for item in row["items"] if item["type"] == "organic"
    ]
    serp = paid_rankings._serp_from_items("x", paid_rankings._serp_items(result))
    positions = [position for position, _ in serp.organic]
    assert positions == sorted(set(positions))
    assert set(positions) <= set(ranks)
    assert positions[0] == min(positions)
    assert len(positions) >= 1


@respx.mock
async def test_a_social_account_of_the_customer_is_no_place_of_the_website(
    client: paid_rankings.DataForSeoClient,
) -> None:
    items = [
        {
            "type": "organic",
            "rank_group": 1,
            "domain": "www.instagram.com",
            "breadcrumb": "https://www.instagram.com › katjakarsch",
        },
        {"type": "organic", "rank_group": 2, "domain": "www.katjakarsch.de"},
    ]
    _tasks(paid_rankings.ORGANIC_TASKS, {"a": [{"items": items}]})

    batch = await paid_rankings.search_google(client, ["Katja Karsch"], 51.96, 7.62)

    assert batch.results[0].organic == [(1, "instagram.com"), (2, "katjakarsch.de")]


@respx.mock
async def test_search_google_without_coordinates_searches_germany(
    client: paid_rankings.DataForSeoClient,
) -> None:
    post = _tasks(paid_rankings.ORGANIC_TASKS, {"a": None})

    batch = await paid_rankings.search_google(client, ["Dachdecker Münster"], None, None)

    body = json.loads(post.calls[0].request.content)[0]
    assert body["location_code"] == paid_rankings.GERMANY_LOCATION_CODE
    assert "location_coordinate" not in body
    assert batch.results[0].organic == []


@respx.mock
async def test_a_failed_task_is_repeated_once_when_the_credit_covers_it(
    client: paid_rankings.DataForSeoClient,
) -> None:
    base = f"{BASE}{paid_rankings.ORGANIC_TASKS}"
    post = respx.post(f"{base}/task_post").mock(
        side_effect=[_created("a", status=40501), _created("b")]
    )
    respx.get(f"{BASE}{paid_rankings.USER_DATA_PATH}").mock(return_value=_balance(1.0))
    respx.get(f"{base}/tasks_ready").mock(return_value=_ready("b"))
    respx.get(f"{base}/task_get/advanced/b").mock(return_value=_envelope(ORGANIC_RESULT))

    batch = await paid_rankings.search_google(client, ["x"], 51.0, 7.0)

    assert post.call_count == 2
    assert batch.failed == []
    assert batch.repeated == 1


@respx.mock
async def test_a_failed_task_is_not_repeated_without_credit(
    client: paid_rankings.DataForSeoClient,
) -> None:
    base = f"{BASE}{paid_rankings.ORGANIC_TASKS}"
    post = respx.post(f"{base}/task_post").mock(
        side_effect=[_created("a"), _created("z", status=40501)]
    )
    respx.get(f"{base}/tasks_ready").mock(return_value=_ready("a"))
    respx.get(f"{base}/task_get/advanced/a").mock(return_value=_envelope(None, task_status=40400))
    respx.get(f"{BASE}{paid_rankings.USER_DATA_PATH}").mock(return_value=_balance(0.0005))

    batch = await paid_rankings.search_google(client, ["x"], 51.0, 7.0)

    assert post.call_count == 1
    assert batch.failed == ["x"]
    assert batch.reason is not None and paid_rankings.NO_CREDIT_FOR_REPEAT in batch.reason
    assert batch.repeated == 0


@respx.mock
async def test_unauthorised_request_fails_the_phrases_and_reads_no_credit(
    client: paid_rankings.DataForSeoClient,
) -> None:
    respx.post(f"{BASE}{paid_rankings.ORGANIC_TASKS}/task_post").mock(
        return_value=httpx.Response(401)
    )
    respx.get(f"{BASE}{paid_rankings.USER_DATA_PATH}").mock(return_value=httpx.Response(401))

    batch = await paid_rankings.search_google(client, ["x", "y"], 51.0, 7.0)

    assert batch.failed == ["x", "y"]
    assert batch.reason is not None and "401" in batch.reason
    assert paid_rankings.UNREADABLE_CREDIT in batch.reason


@respx.mock
async def test_a_task_not_ready_in_time_fails_with_the_timeout(
    client: paid_rankings.DataForSeoClient,
) -> None:
    base = paid_rankings.ORGANIC_TASKS
    respx.post(f"{BASE}{base}/task_post").mock(return_value=_created("a"))
    respx.get(f"{BASE}{base}/tasks_ready").mock(return_value=_ready())
    ticks = iter([0.0, 1.0, 100.0])

    batch = await paid_rankings.run_tasks(
        client,
        base,
        [{"keyword": "x"}],
        repeat_failed=False,
        timeout_seconds=10,
        clock=lambda: next(ticks),
    )

    assert batch.failed == {0: "no result within 10 s"}


@respx.mock
async def test_every_raw_answer_is_kept(collector_settings: Settings) -> None:
    kept: list[tuple[str, bytes]] = []
    gate = SourceGate(
        SourcesSettings(), lambda source, cap: True, raw_sink=lambda n, b: kept.append((n, b))
    )
    client = paid_rankings.DataForSeoClient(_paid_settings(collector_settings), gate)
    client.sleep = _no_sleep
    _tasks(paid_rankings.ORGANIC_TASKS, {"a": ORGANIC_RESULT})

    await paid_rankings.search_google(client, ["x"], 51.0, 7.0)

    assert [name for name, _ in kept] == ["serp_google_organic_task_get_advanced_a.json"]
    assert b"dachdecker-hollenbeck.de" in kept[0][1]


@respx.mock
async def test_post_records_the_reported_cost_in_euros(collector_settings: Settings) -> None:
    ledger = Ledger()
    gate = SourceGate(SourcesSettings(), lambda source, cap: True, ledger)
    settings = _paid_settings(collector_settings).model_copy(
        update={"dataforseo_eur_per_usd": Decimal("0.9")}
    )
    client = paid_rankings.DataForSeoClient(settings, gate)
    respx.post(VOLUME_URL).mock(return_value=_envelope([], cost=0.002))

    await client.post(paid_rankings.VOLUME_PATH, {"keyword": "Dachdecker"})

    assert ledger.added == [("dataforseo", Decimal("0.0018"))]
    assert gate.cost_capped == frozenset()


@respx.mock
async def test_monthly_cost_cap_stops_further_requests(collector_settings: Settings) -> None:
    sources = SourcesSettings.model_validate({"dataforseo": {"monthly_cost_cap_eur": "0.003"}})
    ledger = Ledger(spent=Decimal("0.002"))
    gate = SourceGate(sources, lambda source, cap: True, ledger)
    client = paid_rankings.DataForSeoClient(_paid_settings(collector_settings), gate)
    route = respx.post(VOLUME_URL).mock(return_value=_envelope([], cost=0.002))

    await client.post(paid_rankings.VOLUME_PATH, {"keyword": "Dachdecker"})

    assert route.call_count == 1
    assert gate.cost_capped == {Source.DATAFORSEO}
    with pytest.raises(MonthlyCostCapReachedError):
        await client.post(paid_rankings.VOLUME_PATH, {"keyword": "x"})


def test_grid_points_are_one_kilometre_apart() -> None:
    points = paid_rankings.grid_points(51.96, 7.63, 3)

    assert len(points) == 9
    assert points[4] == pytest.approx((51.96, 7.63))
    south_west, east_neighbour, north_neighbour = points[0], points[1], points[3]
    latitude_km = (north_neighbour[0] - south_west[0]) * paid_rankings.KM_PER_DEGREE_LATITUDE
    longitude_km = (
        (east_neighbour[1] - south_west[1])
        * paid_rankings.KM_PER_DEGREE_LATITUDE
        * math.cos(math.radians(51.96))
    )
    assert latitude_km == pytest.approx(1.0)
    assert longitude_km == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("index", "key"), [(0, "r3c1"), (2, "r3c3"), (4, "r2c2"), (6, "r1c1"), (8, "r1c3")]
)
def test_grid_key_counts_rows_from_the_north(index: int, key: str) -> None:
    assert paid_rankings.grid_key(index, 3) == key


def test_grid_key_rejects_index_outside_grid() -> None:
    with pytest.raises(ValueError, match="outside"):
        paid_rankings.grid_key(9, 3)


def test_grid_points_rejects_empty_grid() -> None:
    with pytest.raises(ValueError, match="grid_size"):
        paid_rankings.grid_points(51.96, 7.63, 0)


@respx.mock
async def test_monthly_volumes_maps_back_to_input_phrases(
    client: paid_rankings.DataForSeoClient,
) -> None:
    route = respx.post(VOLUME_URL).mock(
        return_value=_envelope(
            [
                {"keyword": "dachdecker münster", "search_volume": 880},
                {"keyword": "flachdach münster", "search_volume": None},
            ]
        )
    )

    volumes = await paid_rankings.monthly_volumes(
        client, ["Dachdecker Münster", "Flachdach Münster"]
    )

    assert volumes == {"Dachdecker Münster": 880}
    body = json.loads(route.calls[0].request.content)[0]
    assert body["location_code"] == 2276
    assert body["language_code"] == "de"


@respx.mock
async def test_monthly_volumes_rejects_malformed_rows(
    client: paid_rankings.DataForSeoClient,
) -> None:
    respx.post(VOLUME_URL).mock(return_value=_envelope([{"search_volume": 1}]))

    with pytest.raises(CollectionError, match="malformed"):
        await paid_rankings.monthly_volumes(client, ["x"])


@respx.mock
async def test_post_books_the_request_under_its_endpoint_for_the_audit(
    collector_settings: Settings,
) -> None:
    ledger = Ledger()
    audit_id = uuid4()
    gate = SourceGate(SourcesSettings(), lambda source, cap: True, ledger, audit_id)
    client = paid_rankings.DataForSeoClient(_paid_settings(collector_settings), gate)
    respx.post(VOLUME_URL).mock(return_value=_envelope([], cost=0.002))

    await client.post(paid_rankings.VOLUME_PATH, {"keyword": "Dachdecker"})

    assert ledger.audited == [
        (audit_id, "dataforseo", paid_rankings.VOLUME_PATH, 1, Decimal("0.002"))
    ]
