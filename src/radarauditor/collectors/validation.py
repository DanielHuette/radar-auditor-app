from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import HttpUrl

from radarauditor.collectors.engine_hosts import only_engine_hosts
from radarauditor.collectors.identity import identity, place_identity, same_business
from radarauditor.collectors.patterns import host_of, normalise_phone, site_key
from radarauditor.collectors.snapshot import (
    AssistantAnswer,
    BusinessFacts,
    DirectoryHit,
    GoogleAdsSnapshot,
    MeasurementRecord,
    MetaAdsSnapshot,
    PageSpeedSnapshot,
    PaidSnapshot,
    PlaceSnapshot,
    SerpResult,
    WebsiteSnapshot,
)
from radarauditor.feedback.routes import load_measurements, supplier_labels

STATUS_OK = "ok"
HTTP_OK = 200
MIN_PAGESPEED_RUNS = 3
MAX_MILLISECONDS = 120_000
MAX_LAYOUT_SHIFT = 10
SEARCH_LANGUAGE = "de"
VALID_REASON = "Alle Plausibilitätsprüfungen bestanden"
NO_PROFILE_REASON = "Das Profil wurde gesucht, es gibt keines"
OLDER_PAGESPEED_REASON = (
    "Die PageSpeed-Daten stammen aus einer älteren Messung ohne Laborwerte aus mehreren Läufen"
)
INCOMPLETE_FACTS_REASON = "nicht gemessen: Profil mit unvollständigen Firmendaten gesucht"
COMPANY_SUPPLIER = "Impressum"
FAILED_STATUS = "failed"
ROUTES_FILE = Path("config") / "messwege.yaml"

STEP_WEBSITE = "website"
STEP_COMPANY = "company"
STEP_PAGESPEED = "pagespeed"
STEP_GOOGLE_SEARCH = "search_google"
STEP_BING_SEARCH = "search_bing"
STEP_IMAGE_SEARCH = "search_images"
STEP_PLACE = "place"
STEP_LINKS = "links"
STEP_ASSISTANTS = "assistants"
STEP_LISTINGS = "listings"
STEP_ADS_GOOGLE = "ads_google"
STEP_ADS_META = "ads_meta"
GOOGLE_ADS_FORMAT_REASON = "nicht gemessen: Antwortformat unbestätigt"
META_ADS_FORMAT_REASON = "nicht gemessen: Ausgabeformat noch nicht an echtem Abruf bestätigt"
NO_LISTINGS_REASON = "Weder Apple Karten noch Bing Karten haben eine Antwort geliefert"

STEP_MEASUREMENTS: dict[str, tuple[str, str | None]] = {
    STEP_WEBSITE: ("crawler", None),
    STEP_PAGESPEED: ("pagespeed", None),
    STEP_GOOGLE_SEARCH: ("serp", None),
    STEP_BING_SEARCH: ("serp", "bing"),
    STEP_IMAGE_SEARCH: ("serp", "google_images"),
    STEP_PLACE: ("places", None),
    STEP_LINKS: ("paid", None),
    STEP_ASSISTANTS: ("assistants", None),
    STEP_LISTINGS: ("directories", None),
    STEP_ADS_GOOGLE: ("ads_google", None),
    STEP_ADS_META: ("ads_meta", None),
}

CHECK_STEPS: dict[str, tuple[str, ...]] = {
    "crawler": (STEP_WEBSITE,),
    "pagespeed": (STEP_PAGESPEED,),
    "places": (STEP_PLACE,),
    "assistants": (STEP_ASSISTANTS,),
    "c4": (STEP_WEBSITE, STEP_COMPANY),
    "i1": (STEP_LINKS,),
    "j3": (STEP_PLACE,),
    "j7": (STEP_BING_SEARCH,),
    "j8": (STEP_IMAGE_SEARCH,),
    "h1": (STEP_LISTINGS,),
    "h2": (STEP_LISTINGS,),
    "p1": (STEP_ADS_GOOGLE,),
    "p2": (STEP_ADS_META,),
    "j5": (STEP_LINKS,),
    "j9": (STEP_GOOGLE_SEARCH,),
    "c12": (STEP_LINKS,),
    "j1": (STEP_GOOGLE_SEARCH,),
    "j2": (STEP_GOOGLE_SEARCH,),
    "j4": (STEP_GOOGLE_SEARCH,),
    "j6": (STEP_GOOGLE_SEARCH,),
    "p3": (STEP_GOOGLE_SEARCH,),
    "p4": (STEP_GOOGLE_SEARCH,),
    "b18": (STEP_WEBSITE, STEP_COMPANY),
    "b21": (STEP_WEBSITE, STEP_COMPANY),
}


def route_text(routes: Iterable[str], labels_path: Path = ROUTES_FILE) -> str:
    labels = supplier_labels(labels_path)
    names = [labels.get(route, route) for route in dict.fromkeys(routes)]
    return " → ".join(names) if names else "keiner"


def start_supplier(step: str, path: Path = ROUTES_FILE) -> str:
    measurement, variant = STEP_MEASUREMENTS[step]
    entry = load_measurements(path)[measurement]
    suppliers = entry.variants[variant] if variant is not None else entry.routes
    if not suppliers:
        return entry.label
    return supplier_labels(path)[suppliers[0].supplier]


def _record(
    step: str,
    supplier: str,
    route: str,
    status: str,
    count: int,
    problems: list[str],
    measured_at: datetime | None = None,
    valid_reason: str = VALID_REASON,
) -> MeasurementRecord:
    return MeasurementRecord(
        step=step,
        supplier=supplier,
        route=route,
        measured_at=measured_at or datetime.now(UTC),
        technical_status=status,
        record_count=count,
        valid=not problems,
        reason="; ".join(problems) if problems else valid_reason,
    )


def not_delivered(step: str, supplier: str, reason: str) -> MeasurementRecord:
    return _record(step, supplier, route_text([]), FAILED_STATUS, 0, [reason])


def _same_address(first: str, second: str) -> bool:
    left, right = urlsplit(first), urlsplit(second)
    return site_key(left.hostname or "") == site_key(right.hostname or "") and (
        left.path.rstrip("/") or "/"
    ) == (right.path.rstrip("/") or "/")


def validate_website(
    site: WebsiteSnapshot | None, requested: HttpUrl, status: str = STATUS_OK
) -> MeasurementRecord:
    problems: list[str] = []
    count = 0
    if site is None or not site.pages:
        problems.append("Die Website wurde nicht abgerufen")
    else:
        count = len(site.pages)
        start = site.pages[0]
        if start.status_code != HTTP_OK:
            problems.append(
                f"Die Startseite antwortet mit Status {start.status_code}, erwartet 200"
            )
        if not start.text_excerpt.strip():
            problems.append("Die Startseite enthält keinen sichtbaren Text")
        asked, answered = host_of(requested), host_of(start.url)
        if asked is None or answered is None or site_key(asked) != site_key(answered):
            problems.append(
                f"Die Startseite stammt von {answered}, angefragt war {asked}, "
                "und die Weiterleitung führt auf einen anderen Host"
            )
    return _record(STEP_WEBSITE, "Website selbst", "Direktabruf", status, count, problems)


def validate_company(business: BusinessFacts | None) -> MeasurementRecord:
    problems: list[str] = []
    if business is None or not (business.name or "").strip():
        problems.append("Der Firmenname wurde nicht gefunden")
    if (
        business is None
        or not (business.postal_code or "").strip()
        or not (business.city or "").strip()
    ):
        problems.append("Postleitzahl und Ort wurden nicht gefunden")
    return _record(
        STEP_COMPANY, COMPANY_SUPPLIER, "Direktabruf", STATUS_OK, 1 if business else 0, problems
    )


def _in_range(value: float, upper: float) -> bool:
    return 0 <= value <= upper


def validate_pagespeed(
    snapshot: PageSpeedSnapshot | None, requested: HttpUrl, status: str = STATUS_OK
) -> MeasurementRecord:
    problems: list[str] = []
    count = 0
    if snapshot is not None and snapshot.older_format:
        problems.append(OLDER_PAGESPEED_REASON)
    elif snapshot is None or snapshot.lab is None:
        problems.append("PageSpeed hat keine Laborwerte geliefert")
    else:
        lab, field = snapshot.lab, snapshot.field_data
        count = lab.runs
        if lab.runs < MIN_PAGESPEED_RUNS:
            problems.append(f"Nur {lab.runs} von 5 Läufen waren erfolgreich, mindestens 3 nötig")
        if not _in_range(lab.lcp_ms, MAX_MILLISECONDS):
            problems.append(f"Ladezeit LCP {lab.lcp_ms} ms liegt außerhalb von 0 bis 120000")
        if not _in_range(lab.cls, MAX_LAYOUT_SHIFT):
            problems.append(f"Layoutsprung CLS {lab.cls} liegt außerhalb von 0 bis 10")
        if not _in_range(lab.tbt_ms, MAX_MILLISECONDS):
            problems.append(f"Blockierzeit TBT {lab.tbt_ms} ms liegt außerhalb von 0 bis 120000")
        if (
            field is not None
            and field.inp_ms is not None
            and not _in_range(field.inp_ms, MAX_MILLISECONDS)
        ):
            problems.append(f"Reaktionszeit INP {field.inp_ms} ms liegt außerhalb von 0 bis 120000")
        if not _same_address(str(snapshot.url), str(requested)):
            problems.append(f"Geprüft wurde {snapshot.url}, angefragt war {requested}")
    return _record(
        STEP_PAGESPEED,
        "Google PageSpeed Insights",
        "Google PageSpeed Insights",
        status,
        count,
        problems,
    )


def _positions_ascending(organic: list[tuple[int, str]]) -> bool:
    positions = [position for position, _ in organic]
    return all(later > earlier for earlier, later in zip(positions, positions[1:], strict=False))


def result_problems(result: SerpResult) -> list[str]:
    problems: list[str] = []
    returned = result.returned_keyword
    if returned is not None and returned.strip().casefold() != result.phrase.strip().casefold():
        problems.append(f"zurückgegeben wurde ein anderer Suchbegriff („{returned}“)")
    if result.language_code is not None and result.language_code != SEARCH_LANGUAGE:
        problems.append(f"Sprache {result.language_code}, angefragt war {SEARCH_LANGUAGE}")
    if not result.organic and not result.local_pack:
        problems.append("die Ergebnisliste ist leer")
    if not _positions_ascending(result.organic):
        problems.append("Positionen sind nicht aufsteigend und eindeutig")
    if only_engine_hosts(result.organic, result.engine):
        problems.append("die Trefferliste besteht nur aus Adressen der Suchmaschine")
    return problems


def _quoted(phrases: Iterable[str]) -> str:
    return ", ".join(f"„{phrase}“" for phrase in phrases)


def _location_problems(results: list[SerpResult], expected_location_code: int | None) -> list[str]:
    if expected_location_code is None:
        return []
    wrong = [
        result.phrase
        for result in results
        if result.location_code is not None and result.location_code != expected_location_code
    ]
    if not wrong:
        return []
    return [f"Ort weicht vom angefragten Ort {expected_location_code} ab: {_quoted(wrong)}"]


def _exclusion_lines(results: list[SerpResult], missing: list[str]) -> list[str]:
    by_problem: dict[str, list[str]] = {}
    for result in results:
        for problem in result_problems(result):
            by_problem.setdefault(problem, []).append(result.phrase)
    lines = [f"{problem}: {_quoted(phrases)}" for problem, phrases in by_problem.items()]
    if missing:
        lines.append(f"keine Antwort für: {_quoted(missing)}")
    return lines


def validate_search(
    step: str,
    supplier: str,
    results: list[SerpResult],
    phrases: list[str],
    status: str = STATUS_OK,
    expected_location_code: int | None = None,
) -> MeasurementRecord:
    problems = [] if status == STATUS_OK else [f"Anbieterstatus {status}"]
    problems.extend(_location_problems(results, expected_location_code))
    answered = {result.phrase for result in results}
    missing = [phrase for phrase in phrases if phrase not in answered]
    usable = [result for result in results if not result_problems(result)]
    total = len({*phrases, *answered})
    lines = _exclusion_lines(results, missing)
    if not usable:
        problems.append(f"Keine gültige Trefferliste bei {total} Suchbegriffen")
        problems.extend(lines)
    routes = route_text(result.route for result in results)
    count = sum(len(result.organic) for result in results)
    valid_reason = (
        f"Gültig für {len(usable)} von {total} Suchbegriffen; ausgeschlossen: {'; '.join(lines)}"
        if lines
        else VALID_REASON
    )
    return _record(step, supplier, routes, status, count, problems, valid_reason=valid_reason)


def _search_was_complete(business: BusinessFacts | None) -> bool:
    if business is None:
        return False
    located = bool((business.postal_code or "").strip() or (business.street or "").strip())
    return bool((business.name or "").strip() and (business.city or "").strip() and located)


def validate_place(
    place: PlaceSnapshot | None,
    business: BusinessFacts | None,
    route: str,
    status: str = STATUS_OK,
    *,
    none_found: bool = False,
) -> MeasurementRecord:
    problems: list[str] = []
    if place is None and none_found:
        if not _search_was_complete(business):
            return _record(STEP_PLACE, "Google Places", route, status, 0, [INCOMPLETE_FACTS_REASON])
        return _record(
            STEP_PLACE, "Google Places", route, status, 0, [], valid_reason=NO_PROFILE_REASON
        )
    if place is None:
        problems.append("Es wurde kein Google-Profil gelesen")
    elif business is None or not (business.name or "").strip():
        problems.append("Es fehlen Firmendaten zum Vergleich")
    else:
        own = identity(
            name=business.name,
            phone=business.phone,
            street=business.street,
            postal_code=business.postal_code,
        )
        same_company = same_business(own, place_identity(place))
        code = (business.postal_code or "").strip()
        same_code = bool(code) and code in place.formatted_address
        phone = normalise_phone(business.phone or "")
        same_phone = bool(phone) and normalise_phone(place.phone or "") == phone
        if not (same_company and (same_code or same_phone)):
            problems.append(
                f"ungültig: fremdes Profil „{place.name}“, {place.formatted_address}"
                f" passt nicht zu {business.name}"
            )
    return _record(STEP_PLACE, "Google Places", route, status, 1 if place else 0, problems)


def validate_links(
    paid: PaidSnapshot | None, host: str | None, status: str = STATUS_OK
) -> MeasurementRecord:
    problems = [] if status == STATUS_OK else [f"Anbieterstatus {status}"]
    count = 0
    if paid is None:
        problems.append("Es liegen keine Daten zu Verweisen vor")
    else:
        count = len(paid.backlinks)
        delivered = site_key(paid.customer_domain) if paid.customer_domain else None
        if host is None or delivered != site_key(host):
            problems.append(f"Geliefert wurde {delivered}, geprüft werden sollte {host}")
        if delivered is not None and not any(site_key(key) == delivered for key in paid.backlinks):
            problems.append("Zur eigenen Domain wurden keine Verweisdaten geliefert")
    return _record(STEP_LINKS, "DataForSEO Backlinks", "DataForSEO", status, count, problems)


def validate_assistants(
    answers: list[AssistantAnswer], questions: list[str], status: str = STATUS_OK
) -> MeasurementRecord:
    problems = [] if status == STATUS_OK else [f"Anbieterstatus {status}"]
    if not answers:
        problems.append("Es wurde keine Antwort geliefert")
    asked = {question.strip().casefold() for question in questions}
    for answer in answers:
        if not answer.answer_excerpt.strip():
            problems.append(f"{answer.provider}: Antworttext ist leer")
        if answer.question.strip().casefold() not in asked:
            problems.append(
                f"{answer.provider}: beantwortet wurde „{answer.question}“, "
                "das ist keine gestellte Frage"
            )
    routes = route_text(answer.route for answer in answers if answer.route)
    return _record(STEP_ASSISTANTS, "KI-Assistenten", routes, status, len(answers), problems)


def validate_listings(hits: list[DirectoryHit]) -> MeasurementRecord:
    answered = [hit for hit in hits if hit.found or hit.delivered != 0]
    problems = [] if answered else [NO_LISTINGS_REASON]
    return _record(
        STEP_LISTINGS, "Apple Karten, Bing Karten", "Apify", STATUS_OK, len(hits), problems
    )


def validate_google_ads(
    snapshot: GoogleAdsSnapshot, host: str | None, status: str = STATUS_OK
) -> MeasurementRecord:
    problems = [] if status == STATUS_OK else [f"Anbieterstatus {status}"]
    if snapshot.format_problem is not None:
        problems.append(f"{GOOGLE_ADS_FORMAT_REASON}: {snapshot.format_problem}")
    returned = snapshot.returned_location_code
    if returned is not None and returned != snapshot.location_code:
        problems.append(f"Ort {returned}, angefragt war {snapshot.location_code}")
    if host is None or site_key(snapshot.target) != site_key(host):
        problems.append(f"Geprüft wurde {snapshot.target}, angefragt war {host}")
    return _record(
        STEP_ADS_GOOGLE,
        "DataForSEO Google-Anzeigen",
        "DataForSEO",
        status,
        len(snapshot.ads),
        problems,
    )


def validate_meta_ads(snapshot: MetaAdsSnapshot, status: str = STATUS_OK) -> MeasurementRecord:
    problems = [] if status == STATUS_OK else [f"Anbieterstatus {status}"]
    if not snapshot.confirmed:
        problems.append(META_ADS_FORMAT_REASON)
    return _record(
        STEP_ADS_META,
        "Apify Facebook-Anzeigenbibliothek",
        "Apify",
        status,
        snapshot.items_total,
        problems,
    )


def steps_of_check(check_id: str, measurement: str) -> tuple[str, ...]:
    if check_id in CHECK_STEPS:
        return CHECK_STEPS[check_id]
    return CHECK_STEPS.get(measurement, ())
