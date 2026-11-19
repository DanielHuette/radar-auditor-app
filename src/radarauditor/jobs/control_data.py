import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from radarauditor.collectors.snapshot import AssistantAnswer, PageSnapshot, RawAudit
from radarauditor.collectors.validation import COMPANY_SUPPLIER, STEP_MEASUREMENTS, steps_of_check
from radarauditor.feedback.routes import load_measurements, supplier_labels
from radarauditor.scoring.catalog import CheckDefinition
from radarauditor.scoring.checks.common import service_terms

PART_CHARS = 80_000
COMPANY_ENDPOINT = "Impressum der Website"
RULES_FILE = Path("control") / "rules.yaml"
PAGE_KEYS = ("url", "status_code")
ECHO_KEYS = frozenset({*PAGE_KEYS, "start_url", "name"})
ASSISTANT_PROVIDERS = {"k1": "chatgpt", "k2": "chatgpt", "k6": "claude", "k7": "claude"}
DIRECTORY_WORDS = {"h1": "apple", "h2": "bing"}
SERP_CHECKS = {"j1": "web", "j2": "web", "j4": "web", "j6": "web", "p3": "web", "p4": "web"}
PROFILE_CHECKS = frozenset({"f1", "f2", "f4", "f5", "f7", "f8", "f13"})
START_SITE = ("start_url",)
LEGAL_SITE = ("start_url", "legal_notice_url", "privacy_url")
SITE_ROBOTS = ("start_url", "robots_txt", "robots_status", "robots_skipped")
SITE_FETCH = ("start_url", "robots_status", "agent_fetches")
INDEX_PAGE = ("canonical", "meta_robots", "x_robots_tag", "internal_links")
IMAGE_PAGE = ("images", "images_without_alt")
FOCUS_PAGE = ("title", "headings", "body_text", "text_excerpt")
PAGE_FIELDS: dict[str, tuple[str, ...]] = {
    "a4": INDEX_PAGE,
    "a14": (*INDEX_PAGE, "meta_robots_bing"),
    "a12": ("meta_robots", "x_robots_tag", "has_data_nosnippet"),
    "b10": ("meta_robots", "meta_robots_bing", "x_robots_tag", "has_data_nosnippet"),
    "a7": IMAGE_PAGE,
    "b16": IMAGE_PAGE,
    "b5": ("links", "internal_links"),
    "b13": ("links",),
    "b3": ("title", "meta_description"),
    "b4": ("headings",),
    "b15": ("word_count", "body_text", "text_excerpt", "script_sources"),
    "b17": FOCUS_PAGE,
    "b18": ("title", "headings", "first_text", "body_text", "text_excerpt"),
    "b19": ("last_modified",),
    "b20": ("json_ld", "body_text", "text_excerpt"),
    "b21": ("title", "json_ld", "open_graph"),
    "c4": ("title", "headings", "body_text", "text_excerpt", "images", "links"),
    "b11": ("canonical",),
    "b14": ("canonical",),
    "b12": (),
}
SITE_FIELDS: dict[str, tuple[str, ...]] = {
    "a4": SITE_ROBOTS,
    "a14": SITE_ROBOTS,
    "a11": SITE_ROBOTS,
    "a15": SITE_ROBOTS,
    "a17": SITE_ROBOTS,
    "b5": ("start_url", "sitemap_pages", "crawl_capped"),
    "b11": ("start_url", "robots_txt", "sitemap_urls", "sitemap_entries", "sitemap_probes"),
    "b12": ("start_url", "robots_txt", "crawl_errors"),
    "b14": ("start_url", "uses_https", "redirects_to_single_host", "variant_probes"),
    "b17": LEGAL_SITE,
    "b18": LEGAL_SITE,
    "b21": ("start_url", "footer_nap", "legal_notice"),
    "b19": ("start_url", "sitemap_entries"),
    "a13": SITE_FETCH,
    "a16": SITE_FETCH,
    "a18": SITE_FETCH,
    "b22": SITE_FETCH,
}
START_PAGE_ONLY = frozenset({"a13", "a16", "a18", "b22"})
START_PAGE_FIELDS = ("text_excerpt", "body_text", "first_text", "word_count")
NO_PAGE_CHECKS = frozenset({"a11", "a15", "a17"})
BUSINESS_CHECKS = frozenset({"b21", "b18", "b17", "c4", "c12", "b20"})
TERMS_CHECKS = frozenset({"b18", "c4", "c12"})
PLACE_FIELDS = (
    "name",
    "formatted_address",
    "primary_type",
    "types",
    "description",
    "services",
    "attributes",
    "has_opening_hours",
    "photo_count",
    "business_status",
    "rating",
    "review_count",
    "value_routes",
)
SUPPLIER_ANSWERS = "supplier_answers"
RAW_ENDPOINT = re.compile(r"/v3/([a-z0-9_/]+)")
RAW_STAMP = re.compile(r"^\d{8}T\d{6}-\d{5}-")
RAW_TAIL_SEGMENTS = frozenset({"task_post", "task_get", "live", "advanced", "regular", "html"})

type Keys = tuple[str, ...]


@dataclass(frozen=True)
class Group:
    path: Keys
    items: list[Any]


@dataclass(frozen=True)
class Data:
    common: dict[str, Any]
    groups: tuple[Group, ...] = ()


@dataclass(frozen=True)
class Item:
    check_id: str
    part: int
    parts: int
    payload: dict[str, Any]
    data: dict[str, Any]
    text: str
    common: dict[str, Any]


@dataclass(frozen=True)
class RawAnswer:
    name: str
    text: str


@dataclass(frozen=True)
class StepPart:
    step: str
    part: int
    parts: int
    payload: dict[str, Any]
    data: dict[str, Any]
    text: str


type Chunks = list[list[tuple[int, Any]]]


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def answer_keys(definition: CheckDefinition, routes_path: Path) -> frozenset[str]:
    steps = steps_of_check(definition.id, definition.measurement)
    return step_answer_keys(steps, routes_path)


def step_answer_keys(steps: tuple[str, ...], routes_path: Path) -> frozenset[str]:
    keys: set[str] = set()
    for supplier in _suppliers(steps, routes_path):
        for path in RAW_ENDPOINT.findall(str(supplier["endpoint"])):
            segments = [part for part in path.split("/") if part]
            while segments and segments[-1] in RAW_TAIL_SEGMENTS:
                segments.pop()
            if segments:
                keys.add("_".join(segments))
    return frozenset(keys)


def answer_name(path: str) -> str:
    return RAW_STAMP.sub("", path.rsplit("/", 1)[-1])


def answer_paths(keys: frozenset[str], paths: Sequence[str], step: str = "") -> list[str]:
    return [
        path
        for path in sorted(paths)
        if any(answer_name(path).startswith(f"{key}_") for key in keys)
        or (step != "" and answer_name(path).startswith(f"{step}-"))
    ]


def items_of(
    definition: CheckDefinition,
    outcome: str,
    measured: dict[str, object],
    raw: RawAudit,
    routes_path: Path,
    rules: dict[str, str],
    answers: Sequence[RawAnswer] = (),
) -> list[Item]:
    data = _data(definition.id, raw)
    data = _with_answers(data, answers)
    rule = rules.get(definition.id, definition.why)
    header: dict[str, Any] = {
        "check_id": definition.id,
        "name": definition.name,
        "kind": definition.kind,
        "customer_question": definition.customer_question,
        "rule": rule,
        "thresholds": definition.thresholds,
        "calculated_result": {"outcome": outcome, "measured": measured},
        "request": request_of(definition, raw, routes_path),
    }
    chunks = _chunks(data)
    parts: list[Item] = []
    common = json.loads(dump(data.common))
    for number, chunk in enumerate(chunks, start=1):
        part_data = _with_items(data, chunk)
        payload = {**header, "part": number, "of": len(chunks), "data": part_data}
        parts.append(
            Item(definition.id, number, len(chunks), payload, part_data, dump(payload), common)
        )
    return parts


def _with_answers(data: Data, answers: Sequence[RawAnswer]) -> Data:
    if not answers:
        return data
    records = [{"name": answer.name, "text": answer.text} for answer in answers]
    return Data(data.common, (*data.groups, Group((SUPPLIER_ANSWERS,), records)))


def step_request(step: str, raw: RawAudit, routes_path: Path) -> dict[str, Any]:
    records = [record.model_dump(mode="json") for record in raw.measurements if record.step == step]
    return {
        "step": step,
        "suppliers": _suppliers((step,), routes_path),
        "parameters": _parameters(step, raw),
        "measurement_log": records,
        "business": _business(raw),
    }


def step_parts(step: str, request: dict[str, Any], answers: Sequence[RawAnswer]) -> list[StepPart]:
    records = [{"name": answer.name, "text": answer.text} for answer in answers]
    data = Data(request, (Group((SUPPLIER_ANSWERS,), records),))
    chunks = _chunks(data)
    parts: list[StepPart] = []
    for number, chunk in enumerate(chunks, start=1):
        part_data = _with_items(data, chunk)
        payload = {"step": step, "part": number, "of": len(chunks), "data": part_data}
        parts.append(StepPart(step, number, len(chunks), payload, part_data, dump(payload)))
    return parts


def _with_items(data: Data, chunk: list[tuple[int, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(dump(data.common))
    for index, group in enumerate(data.groups):
        target = result
        for key in group.path[:-1]:
            target = target.setdefault(key, {})
        target[group.path[-1]] = [item for number, item in chunk if number == index]
    return result


def _chunks(data: Data) -> Chunks:
    entries = [(index, item) for index, group in enumerate(data.groups) for item in group.items]
    if not entries:
        return [[]]
    budget = max(PART_CHARS - len(dump(data.common)), 1)
    chunks: Chunks = [[]]
    used = 0
    for index, item in entries:
        size = len(dump(item))
        if chunks[-1] and used + size > budget:
            chunks.append([])
            used = 0
        chunks[-1].append((index, item))
        used += size
    return chunks


def _data(check_id: str, raw: RawAudit) -> Data:
    builder = _builder(check_id)
    return builder(check_id, raw)


def _builder(check_id: str) -> Callable[[str, RawAudit], Data]:
    if check_id in PAGE_FIELDS or check_id in SITE_FIELDS:
        return _website_data
    if check_id == "a2":
        return _pagespeed_data
    if check_id in PROFILE_CHECKS:
        return _profile_data
    if check_id in {"g1", "g2"}:
        return _reviews_data
    if check_id == "j3":
        return _grid_data
    if check_id in SERP_CHECKS:
        return _serp_data
    if check_id in {"j7", "j8"}:
        return _other_serp_data
    if check_id == "j9":
        return _history_data
    if check_id in {"i1", "j5", "c12"}:
        return _paid_data
    if check_id in ASSISTANT_PROVIDERS:
        return _assistant_data
    if check_id in DIRECTORY_WORDS:
        return _directory_data
    if check_id == "p1":
        return _google_ads_data
    if check_id == "p2":
        return _meta_ads_data
    return _empty_data


def _single(common: dict[str, Any], path: Keys, items: list[Any]) -> Data:
    return Data(common, (Group(path, items),))


def _empty_data(check_id: str, raw: RawAudit) -> Data:
    return Data({})


def _business(raw: RawAudit) -> object:
    return raw.business.model_dump(mode="json") if raw.business is not None else None


def _page_dump(page: PageSnapshot, fields: tuple[str, ...]) -> dict[str, Any]:
    return page.model_dump(mode="json", include={*PAGE_KEYS, *fields})


def _website_data(check_id: str, raw: RawAudit) -> Data:
    if raw.website is None:
        return Data({"website": None})
    site_fields = SITE_FIELDS.get(check_id, START_SITE)
    site = raw.website.model_dump(mode="json", include=set(site_fields))
    common: dict[str, Any] = {"website": site}
    if check_id in BUSINESS_CHECKS:
        common["business"] = _business(raw)
    if check_id in TERMS_CHECKS:
        common["service_terms"] = service_terms(raw)
        common["search_phrases"] = _phrases(raw)
    if check_id in NO_PAGE_CHECKS:
        return Data(common)
    pages = raw.website.pages
    if check_id in START_PAGE_ONLY:
        fields = (*PAGE_FIELDS.get(check_id, ()), *START_PAGE_FIELDS)
        common["website"]["pages"] = [_page_dump(page, fields) for page in pages[:1]]
        return Data(common)
    fields = PAGE_FIELDS.get(check_id, ())
    return _single(common, ("website", "pages"), [_page_dump(page, fields) for page in pages])


def _phrases(raw: RawAudit) -> list[str]:
    phrases = [serp.phrase for serp in raw.serps if serp.engine == "google"]
    phrases.extend(ranking.phrase for ranking in raw.map_rankings)
    return list(dict.fromkeys(phrases))


def _pagespeed_data(check_id: str, raw: RawAudit) -> Data:
    snapshot = raw.pagespeed.model_dump(mode="json") if raw.pagespeed is not None else None
    return Data({"pagespeed": snapshot})


def _profile_data(check_id: str, raw: RawAudit) -> Data:
    place = raw.place.model_dump(mode="json", include=set(PLACE_FIELDS)) if raw.place else None
    competitors = [
        competitor.model_dump(mode="json", include={"name", "primary_type"})
        for competitor in raw.competitor_places
    ]
    common: dict[str, Any] = {
        "place": place,
        "business": _business(raw),
        "profile_missing": raw.profile_missing,
        "has_location": raw.has_location,
    }
    if check_id == "f2":
        common["competitor_places"] = competitors
    return Data(common)


def _reviews_data(check_id: str, raw: RawAudit) -> Data:
    place = (
        raw.place.model_dump(mode="json", include={"name", "rating", "review_count", "place_id"})
        if raw.place
        else None
    )
    listed = raw.map_results.model_dump(mode="json") if raw.map_results is not None else None
    businesses: list[Any] = listed.pop("businesses") if listed is not None else []
    common: dict[str, Any] = {"place": place, "business": _business(raw), "map_results": listed}
    if listed is None:
        common["competitor_places"] = [
            competitor.model_dump(mode="json", include={"name", "rating", "review_count"})
            for competitor in raw.competitor_places
        ]
    return _single(common, ("map_results", "businesses"), businesses)


def _grid_data(check_id: str, raw: RawAudit) -> Data:
    place = (
        raw.place.model_dump(mode="json", include={"name", "formatted_address"})
        if raw.place
        else None
    )
    return _single(
        {"place": place, "business": _business(raw)},
        ("grid",),
        [point.model_dump(mode="json") for point in raw.grid],
    )


def _customer(raw: RawAudit) -> dict[str, Any]:
    start = raw.website.start_url if raw.website is not None else None
    return {"website": str(start) if start is not None else None, "business": _business(raw)}


def _serp_data(check_id: str, raw: RawAudit) -> Data:
    serps = [
        serp.model_dump(mode="json")
        for serp in raw.serps
        if serp.engine == "google" and serp.vertical == "web"
    ]
    common: dict[str, Any] = {"customer": _customer(raw)}
    if check_id == "j2":
        common["map_rankings"] = [ranking.model_dump(mode="json") for ranking in raw.map_rankings]
    return _single(common, ("serps",), serps)


def _other_serp_data(check_id: str, raw: RawAudit) -> Data:
    if check_id == "j7":
        chosen = [serp for serp in raw.serps if serp.engine == "bing"]
    else:
        chosen = [serp for serp in raw.serps if serp.vertical == "images"]
    return _single(
        {"customer": _customer(raw)}, ("serps",), [serp.model_dump(mode="json") for serp in chosen]
    )


def _history_data(check_id: str, raw: RawAudit) -> Data:
    history = raw.ranking_history.model_dump(mode="json") if raw.ranking_history else None
    if history is None:
        return Data({"ranking_history": None})
    changes = history.pop("changes")
    return _single({"ranking_history": history}, ("ranking_history", "changes"), changes)


def _paid_data(check_id: str, raw: RawAudit) -> Data:
    paid = raw.paid
    if paid is None:
        return Data({"paid": None})
    if check_id == "c12":
        gap = [item.model_dump(mode="json") for item in paid.keyword_gap or []]
        common: dict[str, Any] = {
            "paid": {"customer_domain": paid.customer_domain},
            "business": _business(raw),
            "service_terms": service_terms(raw),
            "search_phrases": _phrases(raw),
        }
        return _single(common, ("paid", "keyword_gap"), gap)
    if check_id == "j5":
        common = {
            "paid": {
                "customer_domain": paid.customer_domain,
                "ranked_keywords": paid.ranked_keywords,
            }
        }
        return Data(common)
    backlinks = {key: value.model_dump(mode="json") for key, value in paid.backlinks.items()}
    paid_data = {"customer_domain": paid.customer_domain, "backlinks": backlinks}
    return Data({"paid": paid_data})


def _assistant_data(check_id: str, raw: RawAudit) -> Data:
    wanted = ASSISTANT_PROVIDERS[check_id]
    answers: list[AssistantAnswer] = [a for a in raw.assistant_answers if a.provider == wanted]
    common: dict[str, Any] = {"business": _business(raw), "provider": wanted}
    if raw.website is not None:
        common["customer_website"] = str(raw.website.start_url)
    return _single(common, ("answers",), [answer.model_dump(mode="json") for answer in answers])


def _directory_data(check_id: str, raw: RawAudit) -> Data:
    word = DIRECTORY_WORDS[check_id]
    matching = [hit for hit in raw.directories if word in hit.directory.casefold()]
    hits = matching or raw.directories
    records = [hit.model_dump(mode="json") for hit in hits]
    return _single({"business": _business(raw)}, ("directories",), records)


def _google_ads_data(check_id: str, raw: RawAudit) -> Data:
    ads = raw.ads_google
    if ads is None:
        return Data({"ads_google": None})
    shown = ads.model_dump(mode="json", exclude={"ads", "raw_items"})
    listed = [ad.model_dump(mode="json") for ad in ads.ads] or ads.raw_items
    return _single({"ads_google": shown}, ("ads_google", "ads"), listed)


def _meta_ads_data(check_id: str, raw: RawAudit) -> Data:
    ads = raw.ads_meta
    if ads is None:
        return Data({"ads_meta": None})
    shown = ads.model_dump(mode="json", exclude={"own_ads", "raw_items"})
    listed = [ad.model_dump(mode="json") for ad in ads.own_ads] or ads.raw_items
    return _single({"ads_meta": shown}, ("ads_meta", "own_ads"), listed)


def request_of(definition: CheckDefinition, raw: RawAudit, routes_path: Path) -> dict[str, Any]:
    steps = steps_of_check(definition.id, definition.measurement)
    records = [
        record.model_dump(mode="json") for record in raw.measurements if record.step in steps
    ]
    return {
        "steps": list(steps),
        "suppliers": _suppliers(steps, routes_path),
        "parameters": {step: _parameters(step, raw) for step in steps},
        "measurement_log": records,
    }


def _suppliers(steps: tuple[str, ...], routes_path: Path) -> list[dict[str, Any]]:
    measurements = load_measurements(routes_path)
    labels = supplier_labels(routes_path)
    found: list[dict[str, Any]] = []
    for step in steps:
        if step not in STEP_MEASUREMENTS:
            found.append(
                {
                    "step": step,
                    "supplier": COMPANY_SUPPLIER,
                    "endpoint": COMPANY_ENDPOINT,
                    "built": True,
                }
            )
            continue
        measurement, variant = STEP_MEASUREMENTS[step]
        entry = measurements[measurement]
        candidates = entry.variants.get(variant, []) if variant is not None else entry.routes
        for supplier in candidates or entry.suppliers():
            found.append(
                {
                    "step": step,
                    "supplier": labels[supplier.supplier],
                    "endpoint": supplier.endpoint,
                    "built": supplier.built,
                }
            )
    return found


def _parameters(step: str, raw: RawAudit) -> dict[str, Any]:
    business = _business(raw)
    if step == "website":
        return {"start_url": str(raw.website.start_url) if raw.website else None}
    if step == "company":
        return {"source": "Impressum der Website"}
    if step == "pagespeed":
        return {"url": str(raw.pagespeed.url) if raw.pagespeed else None, "strategy": "mobile"}
    if step in {"search_google", "search_bing", "search_images"}:
        return _search_parameters(step, raw)
    if step == "place":
        return {"business": business, "place_id": raw.place.place_id if raw.place else None}
    if step == "links":
        return {"domain": raw.paid.customer_domain if raw.paid else None}
    if step == "assistants":
        questions = list(dict.fromkeys(answer.question for answer in raw.assistant_answers))
        return {"questions": questions}
    if step == "listings":
        return {"business": business}
    if step == "ads_google":
        ads = raw.ads_google
        return {"target": ads.target, "location_code": ads.location_code} if ads else {}
    ads_meta = raw.ads_meta
    return {"searched_name": ads_meta.searched_name, "actor": ads_meta.actor} if ads_meta else {}


def _search_parameters(step: str, raw: RawAudit) -> dict[str, Any]:
    if step == "search_google":
        chosen = [s for s in raw.serps if s.engine == "google" and s.vertical == "web"]
    elif step == "search_bing":
        chosen = [s for s in raw.serps if s.engine == "bing"]
    else:
        chosen = [s for s in raw.serps if s.vertical == "images"]
    return {
        "phrases": [serp.phrase for serp in chosen],
        "language_code": sorted({s.language_code for s in chosen if s.language_code}),
        "location_code": sorted({s.location_code for s in chosen if s.location_code is not None}),
    }
