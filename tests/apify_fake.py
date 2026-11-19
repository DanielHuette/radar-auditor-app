import json
import re
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import respx

API = "https://api.apify.com/v2"
SYNC_URL = re.compile(rf"^{re.escape(API)}/acts/(?P<actor>[^/]+)/run-sync-get-dataset-items$")
DEFAULT_COST_USD = Decimal("0.0045")

type Answer = httpx.Response | BaseException | type[BaseException]
type Responder = Callable[[httpx.Request], Answer]


class FakeActor:
    def __init__(self, actor: str, cost_usd: Decimal) -> None:
        self.actor = actor
        self.cost_usd = cost_usd
        self.route = respx.post(f"{API}/acts/{actor}/runs")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.route, name)

    def mock(
        self,
        return_value: httpx.Response | None = None,
        side_effect: Responder | Iterator[Answer] | list[Answer] | None = None,
    ) -> respx.Route:
        answers = iter(side_effect) if isinstance(side_effect, list) else side_effect

        def answer(request: httpx.Request) -> httpx.Response:
            if callable(answers) and not isinstance(answers, type):
                given = answers(request)
            elif answers is not None:
                given = next(answers)
            else:
                assert return_value is not None
                given = return_value
            if isinstance(given, BaseException):
                raise given
            if isinstance(given, type):
                raise given()
            if given.status_code >= httpx.codes.BAD_REQUEST:
                return given
            return self._finished_run(given.json())

        self.route.mock(side_effect=answer)
        return self.route

    def _finished_run(self, items: list[dict[str, Any]]) -> httpx.Response:
        run_id = uuid4().hex
        run = {
            "id": run_id,
            "status": "SUCCEEDED",
            "defaultDatasetId": f"dataset-{run_id}",
            "usageTotalUsd": float(self.cost_usd),
        }
        respx.get(f"{API}/actor-runs/{run_id}").mock(
            return_value=httpx.Response(200, json={"data": run})
        )
        respx.get(f"{API}/datasets/dataset-{run_id}/items").mock(
            return_value=httpx.Response(200, content=json.dumps(items))
        )
        return httpx.Response(201, json={"data": run})


def apify_post(url: str, cost_usd: Decimal = DEFAULT_COST_USD) -> FakeActor:
    found = SYNC_URL.match(url)
    assert found is not None, url
    return FakeActor(found.group("actor"), cost_usd)
