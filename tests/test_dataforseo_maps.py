import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from radarauditor.collectors import chatgpt_scraper, dataforseo_maps, paid_rankings
from radarauditor.collectors.dataforseo_maps import MapsQuery
from radarauditor.collectors.quota import SourceGate
from radarauditor.config import Settings

pytestmark = pytest.mark.usefixtures("fast_retries")

BASE = paid_rankings.API_BASE_URL
SAMPLES = Path(__file__).parent / "fixtures" / "dataforseo"


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def client(collector_settings: Settings, open_gate: SourceGate) -> paid_rankings.DataForSeoClient:
    settings = collector_settings.model_copy(
        update={"dataforseo_login": "login", "dataforseo_password": SecretStr("secret")}
    )
    client = paid_rankings.DataForSeoClient(settings, open_gate)
    client.sleep = _no_sleep
    return client


def _sample(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((SAMPLES / f"{name}_task_get.json").read_text("utf-8"))
    return body


def _created(*ids: str) -> httpx.Response:
    tasks = [{"id": task_id, "status_code": 20100, "status_message": "Created"} for task_id in ids]
    return httpx.Response(200, json={"status_code": 20000, "cost": 0.0, "tasks": tasks})


def _ready(*ids: str) -> httpx.Response:
    result = [{"id": task_id} for task_id in ids]
    return httpx.Response(
        200, json={"status_code": 20000, "tasks": [{"status_code": 20000, "result": result}]}
    )


def _serve(base: str, result_path: str, answers: dict[str, httpx.Response]) -> respx.Route:
    post = respx.post(f"{BASE}{base}/task_post").mock(return_value=_created(*answers))
    respx.get(f"{BASE}{base}/tasks_ready").mock(return_value=_ready(*answers))
    for task_id, answer in answers.items():
        respx.get(f"{BASE}{base}{result_path}/{task_id}").mock(return_value=answer)
    return post


def _failed() -> httpx.Response:
    body = {"status_code": 20000, "tasks": [{"status_code": 40400, "status_message": "Not Found"}]}
    return httpx.Response(200, json=body)


@respx.mock
async def test_search_maps_lists_the_organic_places_without_the_paid_ones(
    client: paid_rankings.DataForSeoClient,
) -> None:
    post = _serve(
        dataforseo_maps.MAPS_TASKS,
        paid_rankings.TASK_GET_ADVANCED,
        {"m": httpx.Response(200, json=_sample("maps"))},
    )

    batch = await dataforseo_maps.search_maps(client, [MapsQuery("pizza", 51.5, -0.12)])

    assert batch.failed == {}
    assert [(item.name, item.place_id, item.review_count) for item in batch.listings[0]] == [
        ("50 Kalò di Ciro Salvo Pizzeria London🍕", "ChIJ76-IInUFdkgR5bH-0ABH0Zo", 6239),
        ("PizzaExpress", "ChIJjQlM9c4EdkgRPu9M3C4I0J8", 3708),
        ("Fatto a Mano Covent Garden", "ChIJ1awQuSYFdkgRsVNky4bQvLg", 2070),
        ("Vasiniko", "ChIJH_WWHgAFdkgRTCp0OSLObAk", 3262),
    ]
    task = json.loads(post.calls[0].request.content)[0]
    assert task == {
        "keyword": "pizza",
        "language_code": "de",
        "location_coordinate": "51.5000000,-0.1200000,14z",
        "depth": 20,
        "priority": 2,
    }


def _profile_answers(reviews: httpx.Response, questions: httpx.Response) -> None:
    get = paid_rankings.TASK_GET
    _serve(dataforseo_maps.INFO_TASKS, get, {"i": httpx.Response(200, json=_sample("info"))})
    _serve(dataforseo_maps.REVIEWS_TASKS, get, {"r": reviews})
    _serve(dataforseo_maps.QUESTIONS_TASKS, get, {"q": questions})


@respx.mock
async def test_read_profiles_builds_the_profile_with_reviews_replies_and_questions(
    client: paid_rankings.DataForSeoClient,
) -> None:
    _profile_answers(
        httpx.Response(200, json=_sample("rev")), httpx.Response(200, json=_sample("qa"))
    )

    (read,) = await dataforseo_maps.read_profiles(client, ["place_id:ChIJP0warDDowokRfvXMkBqL5Cg"])

    assert read.failure is None and read.notes == []
    place = read.snapshot
    assert place is not None
    assert (place.place_id, place.name, place.primary_type) == (
        "ChIJP0warDDowokRfvXMkBqL5Cg",
        "RustyBrick, Inc.",
        "Software company",
    )
    assert place.types == ["Software company", "Internet marketing service", "Website designer"]
    assert (place.rating, place.review_count, place.photo_count) == (4.7, 83, 36)
    assert place.has_opening_hours is True
    assert place.business_status == "OPERATIONAL"
    assert str(place.google_maps_uri) == "https://maps.google.com/?cid=2946633002421908862"
    assert place.question_count == 6
    assert len(place.recent_reviews) == 10
    assert place.recent_reviews[0]["publishTime"] == "2026-10-16T22:29:49+00:00"
    assert place.recent_reviews[0]["reviewReply"] is None
    reply = place.recent_reviews[9]["reviewReply"]
    assert isinstance(reply, dict) and reply["publishTime"] == "2026-10-16T03:44:37+00:00"
    assert place.route_of("recent_reviews") == "dataforseo"
    assert place.route_of("services") is None


@respx.mock
async def test_read_profiles_keeps_the_profile_when_reviews_and_questions_fail(
    client: paid_rankings.DataForSeoClient,
) -> None:
    respx.get(f"{BASE}{paid_rankings.USER_DATA_PATH}").mock(return_value=httpx.Response(401))
    _profile_answers(_failed(), _failed())

    (read,) = await dataforseo_maps.read_profiles(client, ["RustyBrick New York"])

    assert read.snapshot is not None
    assert read.snapshot.route_of("recent_reviews") is None
    assert read.snapshot.question_count is None
    assert [note.split(":")[0] for note in read.notes] == [
        "DataForSEO reviews not read",
        "DataForSEO questions not read",
    ]


@respx.mock
async def test_read_profiles_says_when_no_profile_was_found(
    client: paid_rankings.DataForSeoClient,
) -> None:
    empty = {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": []}]}]}
    get = paid_rankings.TASK_GET
    _serve(dataforseo_maps.INFO_TASKS, get, {"i": httpx.Response(200, json=empty)})
    _serve(dataforseo_maps.REVIEWS_TASKS, get, {"r": httpx.Response(200, json=empty)})
    _serve(dataforseo_maps.QUESTIONS_TASKS, get, {"q": httpx.Response(200, json=empty)})

    (read,) = await dataforseo_maps.read_profiles(client, ["Unbekannt Münster"])

    assert (read.snapshot, read.failure, read.notes) == (None, None, [dataforseo_maps.NO_PROFILE])


@pytest.mark.parametrize(
    ("name", "address", "place_id", "keyword"),
    [
        ("Atelier", "Heerdestraße 16, 48149 Münster", "ChIJabc", "place_id:ChIJabc"),
        ("Atelier", "Münster", "0x1:0x2", "Atelier Münster"),
        ("Atelier", "Münster", None, "Atelier Münster"),
    ],
)
def test_profile_keyword_prefers_the_place_id(
    name: str, address: str, place_id: str | None, keyword: str
) -> None:
    assert dataforseo_maps.profile_keyword(name, address, place_id) == keyword


@respx.mock
async def test_ask_chatgpt_reads_the_answer_and_its_sources(
    client: paid_rankings.DataForSeoClient,
) -> None:
    post = _serve(
        chatgpt_scraper.SCRAPER_TASKS,
        paid_rankings.TASK_GET_ADVANCED,
        {"g": httpx.Response(200, json=_sample("gpt"))},
    )

    batch = await chatgpt_scraper.ask_chatgpt(client, ["Wer ist Albert Einstein?"])

    assert batch.failed == {}
    answer = batch.answers[0]
    assert answer.text.startswith("# Albert Einstein")
    assert answer.citations[0].startswith("https://www.nobelprize.org/prizes/physics/1921/")
    assert len(answer.citations) == 5
    task = json.loads(post.calls[0].request.content)[0]
    assert task == {
        "keyword": "Wer ist Albert Einstein?",
        "location_code": 2276,
        "language_code": "de",
        "force_web_search": True,
        "priority": 2,
    }
