import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from radarauditor.errors import LoginWallError, SearchBlockedError

UNKNOWN_REASON = "Grund unbekannt"
LISTED_CHECKS = 3
HOST_LABELS = {"google": "Google", "bing": "Bing"}
BOT_CHECK = re.compile(r"(\S+) shows a bot check")
HTTP_STATUS = re.compile(r"HTTP (\d{3})")
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
DEFAULT_CODE_REASON = "Es lagen keine auswertbaren Daten vor"


class Result(StrEnum):
    MEASURED = "gemessen"
    FAILED = "fehlgeschlagen"
    SUSPENDED = "ausgesetzt"
    WITH_PERSON = "Schritt mit Daniel"
    OPEN = "offen"
    RESTARTED = "neu gestartet"
    WARNING = "Warnung"
    RUNNING = "läuft"
    VALID = "gültig"
    INVALID = "ungültig"


class Plain(str):
    __slots__ = ()


class AffectedCheck(BaseModel):
    number: int
    id: str
    name: str


class RouteReport(BaseModel):
    route: str
    result: Result
    reason: str | None = None


class AuditEvent(BaseModel):
    time: datetime
    kind: Literal["route", "check", "repair", "measurement"]
    phase: Literal["vorab", "messung", "auswertung"]
    result: Result
    reason: str | None = None
    stage: int | None = None
    route: str | None = None
    label: str
    checks: list[AffectedCheck] = Field(default_factory=list)
    check_number: int | None = None
    check_id: str | None = None
    protocol: dict[str, Any] | None = None

    def to_row(self, audit_id: UUID) -> dict[str, Any]:
        row = self.model_dump(mode="json")
        row["occurred_at"] = row.pop("time")
        row["audit_id"] = str(audit_id)
        return row


MEASUREMENT_LABELS = {
    "website": "Website lesen",
    "company": "Firmendaten aus dem Impressum",
    "pagespeed": "Ladezeit (PageSpeed)",
    "search_google": "Google-Suche",
    "search_bing": "Bing-Suche",
    "search_images": "Google-Bildersuche",
    "place": "Google-Profil",
    "links": "Verweise anderer Websites",
    "assistants": "KI-Antworten",
    "listings": "Karten-Einträge (Apple, Bing)",
    "ads_google": "Google-Anzeigen",
    "ads_meta": "Facebook- und Instagram-Anzeigen",
}
INVALID_REASON_CODE = "measurement_invalid"
NOT_BUILT_REASON = "not_built"

CODE_REASONS = {
    NOT_BUILT_REASON: "Bewertung noch nicht gebaut",
    INVALID_REASON_CODE: "Die zugrunde liegende Messung war ungültig",
    "no_location": "Der Betrieb hat keinen Standort",
    "no_valid_result_lists": (
        "Die Trefferlisten der Suche bestanden nur aus Adressen der Suchmaschine und sind "
        "unbrauchbar"
    ),
    "robots_txt_server_error": (
        "Die robots.txt antwortete mit einem Serverfehler (Status 5xx); wie Google damit "
        "umgeht, hängt vom Zeitpunkt ab, ein einzelner Abruf kann das nicht feststellen"
    ),
    "robots_txt_rate_limited": (
        "Die robots.txt antwortete mit Status 429 (zu viele Anfragen); ein einzelner Abruf "
        "kann nicht feststellen, was Google daraus macht"
    ),
    "robots_txt_no_answer": (
        "Die robots.txt konnte nicht abgerufen werden (keine Antwort oder kein gespeicherter "
        "Status)"
    ),
    "directory_search_not_documented": (
        "Die Suche im Verzeichnis ist nicht belegt (Suchtext oder Trefferzahl fehlen)"
    ),
    "pagespeed_data_outdated": (
        "Die PageSpeed-Daten stammen aus einer älteren Messung ohne Laborwerte aus mehreren "
        "Läufen; bitte neu messen"
    ),
    "profile_not_provable": (
        "Die Suche hat kein Google-Profil gefunden; das beweist nicht, dass keines existiert"
    ),
    "no_profile": "Der Betrieb hat kein Google-Profil",
    "attributes_not_read": "Die Merkmale des Google-Profils wurden nicht gelesen",
    "categories_not_read": "Die Kategorien des Google-Profils wurden nicht gelesen",
    "collector_failed": "Die Messung ist technisch fehlgeschlagen",
    "description_not_read": "Die Beschreibung des Google-Profils wurde nicht gelesen",
    "directories_not_checked": "Die Verzeichnisse wurden nicht abgefragt",
    "directory_not_checked": "Das Verzeichnis wurde nicht abgefragt",
    "mixed_category_sources": "Die Kategorien stammen aus unterschiedlichen Quellen",
    "needs_error_page": "Es fehlt eine Fehlerseite zum Prüfen",
    "needs_history": "Erst ab der zweiten Prüfung messbar",
    "agent_judgement": "Der Audit-Agent beurteilt das beim Lesen der Seiten",
    "no_access": "Es gibt keinen Kundenzugang",
    "no_address": "Es ist keine Adresse hinterlegt",
    "no_ai_overview": "Google hat keine KI-Übersicht gezeigt",
    "no_analytics": "Es liegen keine Analytics-Daten vor",
    "no_backlink_data": "Es liegen keine Daten zu Verlinkungen vor",
    "no_bing_serp": "Die Bing-Suche hat nichts geliefert",
    "no_brand_search": "Die Suche nach dem Firmennamen hat nichts geliefert",
    "no_business_name": "Der Firmenname fehlt",
    "no_checkable_answers": "Keine Antwort der KI-Assistenten war prüfbar",
    "no_cited_sources": "Die KI-Assistenten haben keine Quellen genannt",
    "no_city": "Der Ort fehlt",
    "no_competitor_backlinks": "Es liegen keine Verlinkungen der Mitbewerber vor",
    "no_competitor_keywords": "Es liegen keine Suchbegriffe der Mitbewerber vor",
    "no_competitors": "Es wurden keine Mitbewerber gefunden",
    "no_dated_photos": "Die Fotos des Profils tragen kein Datum",
    "no_footer_nap": "Im Fußbereich der Website stehen keine Firmendaten",
    "no_grid": "Die Kartenmessung rund um den Standort fehlt",
    "no_image_serp": "Die Google-Bildersuche hat nichts geliefert",
    "no_keyword_gap": "Es liegt kein Vergleich der Suchbegriffe vor",
    "no_keywords": "Für den Kunden sind keine Suchbegriffe hinterlegt",
    "no_listed_businesses": "Google Maps hat keine Betriebe aufgelistet",
    "no_listing_data": "Die Verzeichnisse haben keine Einträge geliefert",
    "no_local_pack": "Google hat keine Kartenergebnisse gezeigt",
    "no_negative_reviews": "Es gibt keine schlechten Bewertungen zum Prüfen",
    "no_owner_reviews": "Es liegen keine Antworten des Inhabers vor",
    "no_pagespeed": "PageSpeed hat keinen Wert geliefert",
    "no_place": "Es wurde kein Google-Profil gelesen",
    "no_problem_answers": "Die KI-Assistenten wurden nicht zu Problemen befragt",
    "no_profile_metrics": "Es liegen keine Profilzahlen vor",
    "no_profile_nap": "Das Google-Profil nennt keine Firmendaten",
    "no_questions": "Es wurden keine Fragen gestellt",
    "no_ranked_keywords": "Es liegen keine Platzierungen vor",
    "no_recommendation_answers": "Die KI-Assistenten wurden nicht nach Empfehlungen gefragt",
    "no_reference_facts": "Es fehlen Vergleichsdaten",
    "no_review_counts": "Die Zahl der Bewertungen fehlt",
    "no_review_dates": "Die Daten der Bewertungen fehlen",
    "no_review_platforms_checked": "Die Bewertungsportale wurden nicht abgefragt",
    "no_review_texts": "Die Texte der Bewertungen fehlen",
    "no_reviews": "Es liegen keine Bewertungen vor",
    "no_search_console": "Es liegen keine Daten der Search Console vor",
    "no_search_phrases": "Es gibt keine Suchbegriffe zum Prüfen",
    "no_serps": "Die Google-Suche hat nichts geliefert",
    "no_service_pages": "Die Website hat keine Leistungsseiten",
    "no_shared_rankings": "Die beiden Prüfungen haben keine gemeinsamen Platzierungen",
    "no_start_page": "Die Startseite konnte nicht gelesen werden",
    "no_structured_data": "Die Website hat keine strukturierten Daten",
    "no_subpages": "Es wurden keine Unterseiten gelesen",
    "no_volumes": "Es liegen keine Suchvolumen vor",
    "no_account": "Es wurde kein Konto gefunden",
    "no_follower_data": "Die Zahl der Follower fehlt",
    "no_interaction_data": "Die Zahl der Reaktionen auf Beiträge fehlt",
    "no_post_data": "Die Beiträge des Kontos wurden nicht gelesen",
    "social_not_measured": "Die sozialen Netzwerke wurden nicht gemessen",
    "social_search_failed": "Die Suche nach den Konten ist fehlgeschlagen",
    "no_website": "Die Website konnte nicht gelesen werden",
    "paid_disabled": "Der Bezahldienst DataForSEO ist ausgesetzt",
    "photos_not_read": "Die Fotos des Profils wurden nicht gelesen",
    "posts_not_read": "Die Beiträge des Profils wurden nicht gelesen",
    "privacy_not_crawled": "Die Datenschutzseite wurde nicht gelesen",
    "products_not_read": "Die Produkte des Profils wurden nicht gelesen",
    "questions_not_shown": "Google zeigt keine Fragen und Antworten",
    "scripts_not_recorded": "Die Skripte der Website wurden nicht aufgezeichnet",
    "service_areas_not_read": "Die Einzugsgebiete des Profils wurden nicht gelesen",
    "services_not_read": "Die Leistungen des Profils wurden nicht gelesen",
    "short_history": "Die Website ist zu jung für diese Prüfung",
    "too_few_pages": "Die Website hat zu wenige Seiten für diese Prüfung",
    "crawl_data_outdated": "Die Website-Daten stammen aus einem älteren Abruf ohne diese Angaben",
    "no_agent_fetches": "Die Startseite wurde nicht mit den Kennungen der Suchdienste abgerufen",
    "visitor_fetch_not_200": "Die Startseite antwortete normalen Besuchern nicht mit Status 200",
    "bot_fetch_failed": "Der Abruf mit der Kennung des Suchdienstes ist technisch fehlgeschlagen",
    "bot_fetch_not_200": "Der Abruf mit der Kennung des Suchdienstes lieferte nicht Status 200",
    "bot_status_unclear": "Die Antwort auf den Abruf mit der Kennung des Suchdienstes ist unklar",
    "no_comparable_text": "Die Startseite enthält zu wenig Text zum Vergleichen",
    "no_topic_pages": "Die Website hat keine Inhaltsseiten zum Prüfen",
    "no_variant_probes": "Die Adress-Varianten der Website wurden nicht abgerufen",
    "no_variant_answered": "Keine Adress-Variante der Website hat geantwortet",
    "no_sitemap_probes": "Die Einträge der Sitemap wurden nicht abgerufen",
    "sitemap_not_readable": "Die Sitemap konnte nicht gelesen werden",
    "not_verifiable_from_outside": "Von außen nicht prüfbar",
    "no_dates": "Die Seiten tragen kein Änderungsdatum",
    "no_name_sources": "Der Firmenname steht an keiner prüfbaren Stelle",
    "no_links": "Die Seiten enthalten keine Links",
    "no_content_images": "Die Seiten enthalten keine Inhaltsbilder",
    "field_not_delivered": "Die Quelle liefert diese Angabe des Google-Profils nicht",
    "ownership_not_verifiable": "Ob das Profil bestätigt ist, ist von außen nicht feststellbar",
    "no_peer_ratings": "Es liegen keine Bewertungen der Betriebe auf der Karte vor",
    "ads_google_not_measured": "Der Abruf der Google-Anzeigen hat nichts geliefert",
    "ads_meta_not_measured": "Der Abruf der Facebook- und Instagram-Anzeigen hat nichts geliefert",
    "ads_account_not_connected": "Das Anzeigenkonto des Kunden ist nicht verbunden",
}


def reason_of_code(code: object) -> str:
    if not isinstance(code, str) or not code:
        return DEFAULT_CODE_REASON
    if not CODE_PATTERN.fullmatch(code):
        return code
    return CODE_REASONS.get(code, f"{DEFAULT_CODE_REASON} ({code})")


def plain_reason(error: str | BaseException | None) -> str:
    if error is None:
        return UNKNOWN_REASON
    if isinstance(error, Plain):
        return str(error)
    text = str(error).strip() or type(error).__name__
    known = _known_reason(error, text)
    return known if known is not None else f"Technischer Fehler: {text}"


def _known_reason(error: str | BaseException, text: str) -> str | None:
    lowered = text.lower()
    bot_check = BOT_CHECK.search(text)
    if bot_check is not None:
        host = bot_check.group(1)
        label = next((name for key, name in HOST_LABELS.items() if key in host.lower()), host)
        return f"{label} zeigt eine Sicherheitsabfrage"
    if isinstance(error, SearchBlockedError):
        return "Die Seite zeigt eine Sicherheitsabfrage"
    if isinstance(error, LoginWallError) or re.search(r"sign in|log in|login wall", lowered):
        return "Die Seite verlangt eine Anmeldung"
    if "no assistant answered" in lowered:
        return "Kein Assistent hat geantwortet"
    return _known_status(text, lowered) or _known_state(lowered)


def _known_status(text: str, lowered: str) -> str | None:
    status = HTTP_STATUS.search(text)
    if status is not None:
        code = int(status.group(1))
        if code == 429:
            return "Der Dienst hat zu viele Anfragen abgelehnt (Fehler 429)"
        if code in {401, 403}:
            return f"Der Dienst hat den Zugriff verweigert (Fehler {code})"
        if code >= 500:
            return f"Der Dienst ist gerade gestört (Fehler {code})"
        return f"Der Dienst hat mit Fehler {code} geantwortet"
    if re.search(r"timed out|timeout", lowered):
        return "Die Antwort kam nicht rechtzeitig (Zeitüberschreitung)"
    return None


def _known_state(lowered: str) -> str | None:
    pairs = (
        ("monthly cost cap", "Das Kostenlimit dieser Quelle ist erreicht"),
        ("daily cap", "Das Tageslimit dieser Quelle ist erreicht"),
        ("switched off", "Die Quelle ist in den Einstellungen ausgeschaltet"),
        ("credentials", "Die Zugangsdaten fehlen"),
        ("authorisation", "Es ist keine Google-Freigabe hinterlegt"),
        ("has no keywords", "Für den Kunden sind keine Suchbegriffe hinterlegt"),
        ("has no website", "Der Kunde hat keine Website angegeben"),
        ("has no location", "Für den Kunden ist kein Standort hinterlegt"),
        ("has no coordinates", "Für den Standort fehlen die Koordinaten"),
    )
    return next((sentence for marker, sentence in pairs if marker in lowered), None)


def _checks_text(event: AuditEvent) -> str:
    listed = ", ".join(f"Nr {check.number} {check.id}" for check in event.checks[:LISTED_CHECKS])
    rest = len(event.checks) - LISTED_CHECKS
    return f"{listed} und {rest} weitere" if rest > 0 else listed


def failure_line(event: AuditEvent) -> str:
    reason = event.reason or UNKNOWN_REASON
    if not event.checks:
        return f"{event.label}: {reason}"
    return f"{event.label} ({_checks_text(event)}): {reason}"


def check_line(event: AuditEvent) -> str:
    return (
        f"Nr {event.check_number} {event.check_id} {event.label}: {event.reason or UNKNOWN_REASON}"
    )


def overview_table(events: list[AuditEvent]) -> str:

    def cell(text: str | None) -> str:
        return (text or "").replace("|", "/").replace("\n", " ")

    rows = [
        f"| {event.check_number} | {event.check_id} | {cell(event.label)} | "
        f"{event.result.value} | {cell(event.reason)} |"
        for event in events
    ]
    return "\n".join(
        ["| Nr | Kennung | Prüfung | Ergebnis | Grund |", "|---|---|---|---|---|", *rows]
    )
