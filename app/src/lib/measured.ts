import { formatDate } from "./format";

export interface MeasuredLine {
  key: string;
  label: string;
  value: string;
}

type Kind =
  | "yesno" | "found" | "count" | "number" | "words" | "chars" | "searches" | "clicks" | "place"
  | "ms" | "decimal" | "score" | "share" | "change" | "rating" | "days" | "perMonth" | "date"
  | "month" | "url" | "text" | "term" | "urls" | "hosts" | "terms" | "quotes" | "positions" | "counts"
  | "shares" | "changes" | "metrics" | "searchTotals" | "urlCounts" | "urlReasons" | "urlDays" | "urlGroups"
  | "listings" | "gaps" | "reason" | "sources" | "businesses" | "hidden" | "note" | "rankChanges"
  | "ratings" | "examples" | "agentStates" | "phraseBreakdown" | "adPhrases" | "hours";

interface Field {
  label: string;
  kind: Kind;
}

const LISTED = 4;
const NOT_FOUND = "nicht gefunden";

const integer = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 0 });
const oneDecimal = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
const twoDecimals = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2 });

const FIELDS: Readonly<Record<string, Field>> = {
  reason: { label: "Grund", kind: "reason" },
  sources: { label: "Ausgeschöpft", kind: "sources" },
  capped_sources: { label: "Ohne Antworten von (Tageslimit)", kind: "sources" },
  route: { label: "Datenweg", kind: "hidden" },
  routes: { label: "Datenwege", kind: "hidden" },
  pages_total: { label: "Seiten geprüft", kind: "count" },
  lcp_ms: { label: "Hauptinhalt sichtbar nach", kind: "ms" },
  lcp_source: { label: "Hauptinhalt gemessen bei", kind: "term" },
  lcp_rating: { label: "Hauptinhalt bewertet als", kind: "term" },
  cls: { label: "Verrutschen beim Laden (CLS)", kind: "decimal" },
  cls_source: { label: "Verrutschen gemessen bei", kind: "term" },
  cls_rating: { label: "Verrutschen bewertet als", kind: "term" },
  inp_ms: { label: "Reaktion auf Tippen", kind: "ms" },
  inp_source: { label: "Reaktion gemessen bei", kind: "term" },
  inp_rating: { label: "Reaktion bewertet als", kind: "term" },
  tbt_ms: { label: "Blockierte Zeit beim Laden (TBT)", kind: "ms" },
  tbt_source: { label: "Blockierte Zeit gemessen bei", kind: "term" },
  tbt_rating: { label: "Blockierte Zeit bewertet als", kind: "term" },
  lab_runs: { label: "Labormessungen am Handy", kind: "count" },
  mobile_performance: { label: "Tempo-Wertung Handy", kind: "score" },
  desktop_performance: { label: "Tempo-Wertung Computer", kind: "score" },
  start_page_affected: { label: "Startseite betroffen", kind: "yesno" },
  robots_txt_present: { label: "robots.txt vorhanden", kind: "yesno" },
  robots_status: { label: "Antwortstatus der robots.txt", kind: "count" },
  pages_affected: { label: "Betroffene Seiten", kind: "urls" },
  pages_affected_count: { label: "Anzahl betroffener Seiten", kind: "count" },
  pages_affected_reasons: { label: "Grund je Seite", kind: "urlReasons" },
  agent: { label: "Kennung des Abrufers", kind: "text" },
  agents: { label: "Abrufer", kind: "agentStates" },
  site_blocked: { label: "Ganze Website gesperrt", kind: "yesno" },
  pages_blocked: { label: "Gesperrte Seiten", kind: "urls" },
  pages_blocked_count: { label: "Anzahl gesperrter Seiten", kind: "count" },
  changes_take_effect_hours: { label: "Änderungen wirken nach etwa", kind: "hours" },
  bot_status: { label: "Antwort an den Abrufer (Statuscode)", kind: "number" },
  reason_blocked: { label: "Grund der Sperre", kind: "text" },
  similarity: { label: "Übereinstimmung mit der Besucheransicht", kind: "share" },
  min_similarity: { label: "Ziel: Übereinstimmung mindestens", kind: "share" },
  visitor_words: { label: "Wörter in der Besucheransicht", kind: "count" },
  bot_words: { label: "Wörter in der Ansicht des Abrufers", kind: "count" },
  images_total: { label: "Inhaltsbilder", kind: "count" },
  images_described: { label: "Davon beschrieben", kind: "count" },
  missing_by_page: { label: "Bilder ohne Beschreibung je Seite", kind: "urlCounts" },
  missing_files: { label: "Dateien ohne Beschreibung", kind: "urls" },
  blocked_by_robots: { label: "Für Google gesperrt (robots.txt)", kind: "urls" },
  wrong_status: { label: "Öffnen sich mit Fehler", kind: "urls" },
  noindex_pages: { label: "Mit „nicht aufnehmen“ gekennzeichnet", kind: "urls" },
  other_canonical_pages: { label: "Verweisen auf eine andere Adresse (Canonical)", kind: "urls" },
  images_without_alt: { label: "Bilder ohne Beschreibung (Alt-Text)", kind: "count" },
  pages_with_issues: { label: "Seiten mit Bildproblemen", kind: "urls" },
  min_volume: { label: "Ziel: Nachfrage ab", kind: "searches" },
  min_words: { label: "Ziel: mindestens", kind: "words" },
  navigation_measured: { label: "Menü ausgewertet", kind: "yesno" },
  navigation_items: { label: "Menüpunkte", kind: "count" },
  navigation_without_href: { label: "Menüpunkte ohne lesbaren Link", kind: "terms" },
  navigation_without_href_count: { label: "Anzahl Menüpunkte ohne lesbaren Link", kind: "count" },
  pages_not_linked: { label: "Seiten ohne Link von einer anderen Seite", kind: "urls" },
  pages_only_in_sitemap: { label: "Nur in der Sitemap, ohne Link", kind: "urls" },
  pages_unreachable_count: { label: "Anzahl nicht verlinkter Seiten", kind: "count" },
  city: { label: "Ort", kind: "text" },
  sitemap_found: { label: "Sitemap gefunden", kind: "yesno" },
  entries_total: { label: "Einträge in der Sitemap", kind: "count" },
  entries_checked: { label: "Davon geprüft", kind: "count" },
  entries_with_problem: { label: "Einträge mit Mangel", kind: "urlReasons" },
  entries_with_problem_count: { label: "Anzahl Einträge mit Mangel", kind: "count" },
  entries_with_lastmod: { label: "Einträge mit Änderungsdatum", kind: "count" },
  links_total: { label: "Links geprüft", kind: "count" },
  links_without_href: { label: "Links ohne lesbare Adresse", kind: "terms" },
  links_without_href_count: { label: "Anzahl Links ohne lesbare Adresse", kind: "count" },
  links_without_label: { label: "Links ohne Beschriftung", kind: "terms" },
  links_without_label_count: { label: "Anzahl Links ohne Beschriftung", kind: "count" },
  variants_checked: { label: "Adress-Varianten geprüft", kind: "count" },
  duplicate_addresses: { label: "Doppelt erreichbar unter", kind: "urls" },
  temporary_redirects: { label: "Nur vorübergehend weitergeleitet", kind: "terms" },
  unreachable_variants: { label: "Nicht erreichbar", kind: "urls" },
  pages_without_text: { label: "Seiten ohne Text im Seitencode", kind: "urls" },
  pages_without_text_count: { label: "Anzahl Seiten ohne Text im Seitencode", kind: "count" },
  system_paths_blocked: { label: "Gesperrte Systempfade", kind: "terms" },
  system_paths_blocked_count: { label: "Anzahl gesperrter Systempfade", kind: "count" },
  min_overlap: { label: "Mindestübereinstimmung für den Vergleich", kind: "hidden" },
  compare_from_words: { label: "Vergleich ab Wörtern", kind: "hidden" },
  difference: { label: "Unterschied", kind: "term" },
  href: { label: "Link", kind: "hidden" },
  entries_without_lastmod: { label: "Einträge ohne Änderungsdatum", kind: "urls" },
  entries_without_lastmod_count: { label: "Anzahl Einträge ohne Änderungsdatum", kind: "count" },
  newest_change_date: { label: "Neueste Änderung am", kind: "date" },
  change_dates: { label: "Änderungsdaten", kind: "hidden" },
  ignored_without_offer_link: { label: "Ignoriert, weil ein Link zum Angebot fehlt", kind: "count" },
  delivered: { label: "Daten geliefert", kind: "yesno" },
  search_text: { label: "Suchtext", kind: "text" },
  competitor_median: { label: "Mittelwert der Wettbewerber", kind: "number" },
  words_in_html: { label: "Wörter im Seitencode", kind: "urlCounts" },
  pages_with_problem: { label: "Seiten mit Mangel", kind: "urls" },
  problems_by_page: { label: "Mangel je Seite", kind: "urlReasons" },
  duplicate_titles: { label: "Gleiche Titel", kind: "urlGroups" },
  images_with_generic_name: { label: "Bilder mit nichtssagendem Dateinamen", kind: "count" },
  generic_names: { label: "Beispiele für Dateinamen", kind: "terms" },
  pages_without_service_and_city: { label: "Seiten ohne Leistung und Ort oben", kind: "urls" },
  pages_dated: { label: "Seiten mit Änderungsdatum", kind: "count" },
  pages_stale: { label: "Seiten über der Altersgrenze", kind: "urlDays" },
  newest_age_days: { label: "Neueste Änderung vor", kind: "days" },
  values_checked: { label: "Angaben verglichen", kind: "count" },
  mismatch_count: { label: "Anzahl Abweichungen", kind: "count" },
  mismatching_sources: { label: "Abweichend geschrieben in", kind: "terms" },
  phrases_with_own_ad: { label: "Suchbegriffe mit eigener Anzeige", kind: "count" },
  phrases: { label: "Anzeigen je Suchbegriff", kind: "adPhrases" },
  competitors_on_brand: { label: "Wettbewerber mit Anzeige auf den Firmennamen", kind: "hosts" },
  competitors_on_brand_count: { label: "Anzahl Wettbewerber mit Anzeige", kind: "count" },
  by_phrase: { label: "Fundstellen je Suchbegriff", kind: "phraseBreakdown" },
  pages_without_title: { label: "Seiten ohne Titel", kind: "urls" },
  pages_with_same_title: { label: "Seiten mit gleichem Titel", kind: "urlGroups" },
  title_problem_count: { label: "Anzahl Seiten mit Titelmangel", kind: "count" },
  note: { label: "Hinweis", kind: "note" },
  previous_audit_id: { label: "Vorherige Prüfung", kind: "hidden" },
  previous_requested_at: { label: "Verglichen mit der Prüfung vom", kind: "date" },
  improved: { label: "Verbesserte Plätze", kind: "count" },
  worsened: { label: "Verschlechterte Plätze", kind: "count" },
  unchanged: { label: "Unveränderte Plätze", kind: "count" },
  changes: { label: "Veränderungen je Suchbegriff", kind: "rankChanges" },
  gap_keywords: { label: "Suchbegriffe nur bei Wettbewerbern", kind: "count" },
  top_gap_keywords: { label: "Wichtigste davon", kind: "gaps" },
  mismatches: { label: "Abweichungen", kind: "terms" },
  postal_code: { label: "Postleitzahl", kind: "text" },
  profile_found: { label: "Google-Profil gefunden", kind: "yesno" },
  business_status: { label: "Status", kind: "term" },
  primary_type: { label: "Hauptkategorie", kind: "text" },
  competitor_types: { label: "Hauptkategorien der Wettbewerber", kind: "terms" },
  has_opening_hours: { label: "Öffnungszeiten eingetragen", kind: "yesno" },
  photo_count: { label: "Fotos", kind: "count" },
  services: { label: "Eingetragene Leistungen", kind: "terms" },
  description_length: { label: "Länge der Beschreibung", kind: "chars" },
  entries: { label: "Anzahl Einträge", kind: "count" },
  examples: { label: "Beispiele", kind: "examples" },
  special_hours: { label: "Sonderöffnungszeiten", kind: "term" },
  max_age_days: { label: "Ziel: höchstens so alt", kind: "days" },
  attributes: { label: "Eingetragene Merkmale", kind: "terms" },
  customer_reviews: { label: "Eigene Bewertungen", kind: "count" },
  competitors: { label: "Wettbewerber", kind: "counts" },
  competitor_average: { label: "Schnitt der Wettbewerber", kind: "number" },
  compared_businesses: { label: "Verglichene Betriebe", kind: "businesses" },
  comparison_source: { label: "Verglichen mit", kind: "term" },
  median_rating: { label: "Mittlerer Wert der verglichenen Betriebe", kind: "rating" },
  partial_margin_stars: { label: "Teilweise bis höchstens so weit darunter", kind: "rating" },
  peers: { label: "Bewertung der verglichenen Betriebe", kind: "ratings" },
  phrase: { label: "Suchbegriff der Kartenliste", kind: "text" },
  without_review_count: { label: "Ohne lesbare Bewertungszahl", kind: "terms" },
  rating: { label: "Durchschnitt", kind: "rating" },
  review_count: { label: "Bewertungen", kind: "count" },
  review_source: { label: "Quelle der Bewertungen", kind: "term" },
  listings: { label: "Einträge", kind: "listings" },
  found: { label: "Gefunden", kind: "found" },
  listing_url: { label: "Eintrag", kind: "url" },
  referring_domains: { label: "Verweisende Websites", kind: "count" },
  best_competitor: { label: "Stärkster Wettbewerber", kind: "text" },
  share_of_best: { label: "Anteil am Stärksten", kind: "share" },
  positions: { label: "Platz je Suchbegriff", kind: "positions" },
  in_top: { label: "Suchbegriffe ganz oben", kind: "count" },
  in_good: { label: "Suchbegriffe auf Seite 1", kind: "count" },
  in_visible: { label: "Suchbegriffe sichtbar", kind: "count" },
  phrases_total: { label: "Suchbegriffe geprüft", kind: "count" },
  phrases_evaluated: { label: "Suchbegriffe mit gültiger Ergebnisliste", kind: "count" },
  phrases_missing: { label: "Suchbegriffe ohne gültige Ergebnisliste", kind: "terms" },
  competitor_values: { label: "Wettbewerberwerte", kind: "hidden" },
  strongest_competitor: { label: "Stärkster Wettbewerber", kind: "hidden" },
  top: { label: "Ganz oben heißt bis", kind: "place" },
  good: { label: "Seite 1 heißt bis", kind: "place" },
  visible: { label: "Sichtbar heißt bis", kind: "place" },
  phrases_with_map: { label: "Suchbegriffe mit Kartenblock", kind: "count" },
  maps_phrases: { label: "Platz aus Google Maps statt Kartenblock", kind: "terms" },
  business_name: { label: "Firmenname", kind: "text" },
  points_total: { label: "Punkte im Kartenraster", kind: "count" },
  points_in_top: { label: "davon ganz oben", kind: "count" },
  share_in_top: { label: "Anteil ganz oben", kind: "share" },
  brand_phrase: { label: "Suche nach dem Namen", kind: "text" },
  position: { label: "Platz", kind: "place" },
  knowledge_panel: { label: "Infokasten neben den Treffern", kind: "yesno" },
  phrases_with_overview: { label: "Suchbegriffe mit KI-Übersicht", kind: "count" },
  phrases_mentioning_business: { label: "davon mit Nennung", kind: "count" },
  source_positions: { label: "Platz in der Bildersuche", kind: "positions" },
  sources_seen: { label: "Bildquellen je Suchbegriff", kind: "counts" },
  ranked_keywords: { label: "Suchbegriffe mit Platzierung", kind: "count" },
  mention_rate: { label: "Genannt in", kind: "share" },
  mentioned: { label: "Antworten mit Nennung", kind: "count" },
  answers: { label: "Antworten", kind: "count" },
  answers_by_provider: { label: "Antworten je Assistent", kind: "counts" },
  by_provider: { label: "Genannt je Assistent", kind: "shares" },
  min_rate: { label: "Ziel: genannt in mindestens", kind: "share" },
  metrics: { label: "Google-Profil, letzte 28 Tage", kind: "metrics" },
  metrics_previous: { label: "Google-Profil, 28 Tage davor", kind: "metrics" },
  change_percent: { label: "Veränderung", kind: "changes" },
  actions: { label: "Anrufe, Routen und Website-Klicks", kind: "count" },
  actions_previous: { label: "28 Tage davor", kind: "count" },
  actions_change_percent: { label: "Veränderung", kind: "change" },
  matching_queries: { label: "Suchanfragen mit Ihren Leistungen", kind: "clicks" },
  top_queries: { label: "Häufigste Suchanfragen", kind: "clicks" },
  queries_total: { label: "Suchanfragen", kind: "count" },
  totals: { label: "Search Console gesamt", kind: "searchTotals" },
  tracked_phrases: { label: "Verfolgte Suchbegriffe", kind: "terms" },
  visitor_status: { label: "Antwort an den Besucher (Statuscode)", kind: "number" },
  bot_error: { label: "Fehler beim Abruf als Suchroboter", kind: "text" },
  sitemap_urls: { label: "Adressen der Sitemap", kind: "urls" },
  target: { label: "Geprüfte Adresse", kind: "text" },
  window_days: { label: "Zeitraum der Anzeigen in Tagen", kind: "count" },
  shows_ads: { label: "Schaltet Anzeigen", kind: "yesno" },
  ads_found: { label: "Gefundene Anzeigen", kind: "count" },
  advertisers: { label: "Werbetreibende", kind: "terms" },
  formats: { label: "Anzeigenformate", kind: "terms" },
  first_shown: { label: "Zuerst gezeigt", kind: "date" },
  last_shown: { label: "Zuletzt gezeigt", kind: "date" },
  searched_name: { label: "Gesuchter Name", kind: "text" },
  own_ads: { label: "Eigene Anzeigen", kind: "count" },
  own_pages: { label: "Beworbene Seiten", kind: "urls" },
  competitor_ad_counts: { label: "Anzeigen je Wettbewerber", kind: "counts" },
  step: { label: "Messschritt", kind: "hidden" },
  invalid_reason: { label: "Grund der ungültigen Messung", kind: "text" },
};

const TERMS: Readonly<Record<string, string>> = {
  name: "Name",
  street: "Straße",
  postal_code: "Postleitzahl",
  city: "Ort",
  phone: "Telefon",
  email: "E-Mail",
  address: "Adresse",
  phone_or_email: "Telefon oder E-Mail",
  responsible: "Verantwortliche Person",
  title: "Titel",
  main_heading: "Hauptüberschrift",
  image_description: "Bildbeschreibung",
  link_text: "Linktext",
  title_or_heading_with_city: "Titel oder Überschrift mit Ort",
  text_with_city: "Text mit Ort",
  description: "Beschreibung",
  image: "Bild",
  telephone: "Telefon",
  url: "Website",
  geo: "Koordinaten",
  openingHours: "Öffnungszeiten",
  openingHoursSpecification: "Öffnungszeiten",
  not_determinable: "von außen nicht feststellbar",
  OPERATIONAL: "geöffnet",
  CLOSED_TEMPORARILY: "vorübergehend geschlossen",
  CLOSED_PERMANENTLY: "dauerhaft geschlossen",
  places: "öffentliche Stichprobe von Google",
  profile: "eigenes Google-Profil",
  browser: "neueste Bewertungen auf Google Maps",
  places_api: "Stichprobe der Google-Schnittstelle",
  dataforseo: "neueste Bewertungen mit Antworten des Inhabers über DataForSEO",
  maps_result_list: "allen Betrieben der Kartenliste bei Google Maps",
  competitor_profiles: "den drei stärksten hinterlegten Wettbewerbern",
  google_ai: "Google KI-Modus",
  chatgpt: "ChatGPT",
  claude: "Claude",
  BUSINESS_IMPRESSIONS_DESKTOP_MAPS: "Aufrufe Karte am Computer",
  BUSINESS_IMPRESSIONS_MOBILE_MAPS: "Aufrufe Karte am Handy",
  BUSINESS_IMPRESSIONS_DESKTOP_SEARCH: "Aufrufe Suche am Computer",
  BUSINESS_IMPRESSIONS_MOBILE_SEARCH: "Aufrufe Suche am Handy",
  CALL_CLICKS: "Anrufe",
  WEBSITE_CLICKS: "Website-Klicks",
  BUSINESS_DIRECTION_REQUESTS: "Routenanfragen",
  BUSINESS_CONVERSATIONS: "Nachrichten",
  BUSINESS_BOOKINGS: "Buchungen",
  field_url: "echten Besuchern dieser Seite (Google-Felddaten)",
  field_origin: "echten Besuchern der ganzen Website (Google-Felddaten)",
  lab: "Google-Labormessung, mittlerer Wert der Läufe",
  good: "gut",
  needs_improvement: "mittel",
  poor: "schlecht",
};

const SOURCES: Readonly<Record<string, string>> = {
  places: "Google Places API",
  google_search: "Google-Suche",
  google_maps: "Google Maps",
  dataforseo: "DataForSEO",
  apify: "Apify",
  google_ai: "Google KI-Modus",
  chatgpt: "ChatGPT",
  claude: "Claude",
};

const REASONS: Readonly<Record<string, string>> = {
  no_website: "Keine Website gefunden oder nicht erreichbar.",
  no_pagespeed: "Google PageSpeed hat keine Werte geliefert.",
  no_place: "Kein Google-Unternehmensprofil gefunden.",
  collector_failed: "Der Abruf ist fehlgeschlagen.",
  no_competitors: "Keine Wettbewerber hinterlegt.",
  no_location: "Der Betrieb hat keinen Standort.",
  measurement_invalid: "Die Messung war ungültig.",
  visitor_fetch_not_200: "Die Seite antwortet Besuchern nicht mit Erfolg.",
  no_agent_fetches: "Es liegt kein Abruf mit den Kennungen der Suchdienste und KI-Assistenten vor.",
  robots_txt_server_error:
    "Die robots.txt antwortete mit einem Serverfehler (Status 5xx); wie Google damit umgeht, hängt vom Zeitpunkt ab, ein einzelner Abruf kann das nicht feststellen.",
  robots_txt_rate_limited:
    "Die robots.txt antwortete mit Status 429 (zu viele Anfragen); ein einzelner Abruf kann nicht feststellen, was Google daraus macht.",
  robots_txt_no_answer:
    "Die robots.txt konnte nicht abgerufen werden (keine Antwort oder kein gespeicherter Status).",
  profile_not_provable:
    "Die Suche hat kein Google-Profil gefunden; das beweist nicht, dass keines existiert.",
  directory_search_not_documented:
    "Die Suche im Verzeichnis ist nicht belegt (Suchtext oder Trefferzahl fehlen).",
  no_valid_result_lists:
    "Die Trefferlisten der Suche bestanden nur aus Adressen der Suchmaschine und sind unbrauchbar.",
  pagespeed_data_outdated:
    "Die PageSpeed-Daten stammen aus einer älteren Messung ohne Laborwerte aus mehreren Läufen; bitte neu messen.",
  bot_fetch_failed: "Der Abruf als Suchroboter ist fehlgeschlagen.",
  bot_fetch_unavailable: "Der Abruf als Suchroboter war nicht möglich.",
  service_delivered_nothing: "Der Dienst hat keine Daten geliefert.",
  bot_fetch_not_200: "Der Suchroboter bekam statt der Seite eine Fehlermeldung.",
  social_not_measured: "Die sozialen Netzwerke wurden nicht gemessen.",
  social_search_failed: "Die Suche nach den Konten ist fehlgeschlagen.",
  no_post_data: "Die Beiträge des Kontos wurden nicht gelesen.",
  no_follower_data: "Die Zahl der Follower wurde nicht gelesen.",
  no_interaction_data: "Die Reaktionen auf Beiträge wurden nicht gelesen.",
  no_faq_sections: "Es wurde keine Seite mit Fragen und Antworten gefunden.",
  legal_notice_not_crawled: "Das Impressum war nicht abrufbar.",
  privacy_not_crawled: "Die Datenschutzerklärung war nicht abrufbar.",
  no_comparable_text: "Es gab keinen Text zum Vergleichen.",
  bot_status_unclear: "Die Antwort an den Suchroboter war nicht eindeutig.",
  no_content_images: "Die Seiten haben keine Inhaltsbilder.",
  sitemap_not_readable: "Die Sitemap ließ sich nicht lesen.",
  no_sitemap_probes: "Es wurden keine Sitemap-Einträge abgerufen.",
  not_verifiable_from_outside: "Von außen nicht feststellbar.",
  no_variant_probes: "Die Adress-Varianten wurden nicht abgerufen.",
  no_variant_answered: "Keine Adress-Variante hat geantwortet.",
  no_topic_pages: "Keine Seiten zu den Leistungen gefunden.",
  no_dates: "Auf den Seiten steht kein Änderungsdatum.",
  no_name_sources: "Es gibt keine Quellen für die Schreibweise des Namens.",
  no_links: "Auf den Seiten wurden keine Links gefunden.",
  no_structured_data: "Keine strukturierten Angaben gefunden.",
  crawl_data_outdated: "Die Website-Daten stammen aus einer älteren Messung; bitte neu prüfen.",
  ownership_not_verifiable: "Die Inhaberschaft lässt sich von außen nicht prüfen.",
  field_not_delivered: "Google hat keine Felddaten geliefert.",
  no_peer_ratings: "Keine Bewertungen der Vergleichsbetriebe verfügbar.",
  ads_google_not_measured: "Die Google-Anzeigen wurden nicht gemessen.",
  ads_meta_not_measured: "Die Meta-Anzeigen wurden nicht gemessen.",
  ads_account_not_connected: "Das Anzeigenkonto ist nicht verbunden.",
  no_access: "Nur mit Zugang zu Ihren Konten bei den Anbietern (zum Beispiel Google, Meta) messbar.",
  no_profile: "Kein Google-Unternehmensprofil vorhanden.",
  no_paid_data: "Braucht den Bezahldienst, der ausgeschaltet ist.",
  paid_disabled: "Braucht den Bezahldienst, der ausgeschaltet ist.",
  needs_error_page: "Die Fehlerseite ließ sich nicht testen.",
  needs_history: "Erst ab der zweiten Prüfung messbar.",
  agent_judgement: "Der Audit-Agent beurteilt das beim Lesen der Seiten.",
  no_shared_rankings: "Die beiden Prüfungen haben keine gemeinsamen Platzierungen.",
  too_few_pages: "Zu wenige Seiten zum Vergleichen.",
  no_subpages: "Keine Unterseiten gefunden.",
  no_search_phrases: "Keine Suchbegriffe hinterlegt.",
  no_volumes: "Für die Suchbegriffe ist keine Nachfrage bekannt.",
  no_service_pages: "Keine Leistungsseiten gefunden.",
  no_questions: "Google hat keine Fragen unter „Nutzer fragen auch“ gezeigt.",
  no_city: "Der Ort ist unbekannt.",
  no_keyword_gap: "Keine Suchbegriffe der Wettbewerber verfügbar.",
  no_footer_nap: "Keine Firmendaten im Fußbereich der Website.",
  no_profile_nap: "Im Google-Profil fehlen Telefon und Adresse.",
  no_address: "Die Adresse ist unbekannt.",
  no_start_page: "Die Startseite war nicht erreichbar.",
  no_reviews: "Keine Bewertungen vorhanden.",
  no_review_dates: "Die Bewertungen haben kein Datum.",
  no_negative_reviews: "Keine kritischen Bewertungen vorhanden.",
  no_review_texts: "Keine Bewertungen mit Text vorhanden.",
  no_keywords: "Keine Suchwörter zum Abgleich vorhanden.",
  no_review_platforms_checked: "Keine Bewertungsportale geprüft.",
  directory_not_checked: "Das Verzeichnis wurde nicht geprüft.",
  directories_not_checked: "Die Verzeichnisse wurden nicht geprüft.",
  no_listing_data: "Keine Einträge zum Vergleichen gefunden.",
  no_backlink_data: "Keine Linkdaten verfügbar.",
  no_competitor_backlinks: "Keine Linkdaten der Wettbewerber verfügbar.",
  short_history: "Der Verlauf ist kürzer als zwölf Monate.",
  no_business_name: "Der Firmenname ist unbekannt.",
  no_brand_search: "Die Suche nach dem Firmennamen fehlt.",
  no_serps: "Keine Suchergebnisse erfasst.",
  no_local_pack: "Google hat keinen Kartenblock gezeigt.",
  no_grid: "Das Kartenraster wurde nicht gemessen.",
  description_not_read: "Die Beschreibung steht nur auf Google Maps, das diesmal nicht lesbar war.",
  services_not_read: "Die Leistungen stehen nur auf Google Maps, das diesmal nicht lesbar war.",
  products_not_read: "Die Produkte stehen nur auf Google Maps, das diesmal nicht lesbar war.",
  posts_not_read: "Die Beiträge stehen nur auf Google Maps, das diesmal nicht lesbar war.",
  questions_not_shown: "Google Maps zeigt für dieses Profil keine Fragen und Antworten.",
  service_areas_not_read: "Das Einzugsgebiet steht nur auf Google Maps, das diesmal nicht lesbar war.",
  attributes_not_read: "Die Merkmale stehen nur auf Google Maps, das diesmal nicht lesbar war.",
  categories_not_read: "Google Maps zeigt nur die Hauptkategorie; die weiteren liefert nur die Google-Schnittstelle.",
  mixed_category_sources: "Kategorien von Google Maps und der Google-Schnittstelle lassen sich nicht vergleichen.",
  no_ai_overview: "Google hat keine KI-Übersicht gezeigt.",
  no_bing_serp: "Keine Bing-Suche erfasst.",
  no_image_serp: "Keine Bildersuche erfasst.",
  no_ranked_keywords: "Keine Platzierungsdaten verfügbar.",
  no_competitor_keywords: "Keine Platzierungsdaten der Wettbewerber verfügbar.",
  no_recommendation_answers: "Keine Antworten auf Empfehlungsfragen erfasst.",
  no_problem_answers: "Keine Antworten auf Problemfragen erfasst.",
  no_reference_facts: "Telefon und Postleitzahl sind unbekannt.",
  no_checkable_answers: "Keine Antwort nannte Telefon oder Postleitzahl.",
  no_cited_sources: "Die Assistenten haben keine Quellen genannt.",
  no_analytics: "Kein Zugang zu Google Analytics.",
  no_search_console: "Kein Zugang zur Search Console.",
  no_profile_metrics: "Keine Kennzahlen aus dem Google-Profil.",
  no_owner_reviews: "Keine Bewertungen im eigenen Profil.",
  photos_not_read: "Fotos bei Google Maps nicht gelesen.",
  no_dated_photos: "Kein Foto mit Datum gefunden.",
  scripts_not_recorded: "Startseite wurde vor dieser Prüfung erfasst; neu prüfen.",
  no_listed_businesses: "Keine Betriebe in der Kartenliste gefunden.",
  no_review_counts: "Bewertungszahlen der Betriebe nicht lesbar.",
};

const SEARCH_TOTALS: readonly (readonly [string, string, (value: number) => string])[] = [
  ["clicks", "Klicks", (value) => integer.format(value)],
  ["impressions", "Einblendungen", (value) => integer.format(value)],
  ["ctr", "Klickrate", (value) => percent(value)],
  ["position", "Platz im Schnitt", (value) => oneDecimal.format(value)],
];

const GRID_KEY = /^r\d+c\d+$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function percent(share: number): string {
  return `${oneDecimal.format(share * 100)} %`;
}

function term(value: string): string {
  return TERMS[value] ?? value;
}

export function reasonText(reason: string): string {
  return REASONS[reason] ?? (/^[a-z0-9_]+$/.test(reason) ? "Nicht messbar." : reason);
}

export function shortUrl(value: string): string {
  const scheme = /^(tel|mailto):/i.exec(value);
  if (scheme !== null) return decodeURIComponent(value.slice(scheme[0].length));
  try {
    const url = new URL(value.includes("://") ? value : `https://${value}`);
    const host = url.hostname.replace(/^www\./, "");
    const path = decodeURIComponent(url.pathname).replace(/\/$/, "");
    return path === "" ? host : path;
  } catch {
    return value;
  }
}

function fullUrl(value: string): string {
  try {
    const url = new URL(value);
    return `${url.hostname.replace(/^www\./, "")}${decodeURIComponent(url.pathname).replace(/\/$/, "")}`;
  } catch {
    return value;
  }
}

export function joinListed(items: readonly string[], separator = ", "): string | null {
  if (items.length === 0) return null;
  const shown = items.slice(0, LISTED).join(separator);
  const rest = items.length - LISTED;
  return rest > 0 ? `${shown} und ${String(rest)} weitere` : shown;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function entries(value: unknown): [string, unknown][] {
  return isRecord(value) ? Object.entries(value) : [];
}

function place(value: unknown): string {
  return isNumber(value) ? `Platz ${integer.format(value)}` : NOT_FOUND;
}

function signed(value: unknown): string {
  if (!isNumber(value)) return "–";
  return `${value > 0 ? "+" : ""}${oneDecimal.format(value)} %`;
}

function listing(name: string, value: unknown): string {
  if (!isRecord(value)) return name;
  if (value.found !== true) return `${name}: ${NOT_FOUND}`;
  const differs = strings(value.mismatches).map((field) => `${term(field)} weicht ab`);
  return `${name}: ${differs.length === 0 ? "gefunden, Daten stimmen" : `gefunden, ${differs.join(", ")}`}`;
}

function business(value: unknown): string | null {
  if (!isRecord(value) || typeof value.name !== "string" || value.name === "") return null;
  const count = value.review_count;
  if (!isNumber(count)) return value.name;
  return `${value.name}: ${integer.format(count)} ${count === 1 ? "Bewertung" : "Bewertungen"}`;
}

function gap(value: unknown): string | null {
  if (!isRecord(value) || typeof value.phrase !== "string") return null;
  const details = [isNumber(value.volume) ? `${integer.format(value.volume)} Suchen im Monat` : ""];
  if (typeof value.competitor_domain === "string") {
    const where = isNumber(value.competitor_position) ? ` auf Platz ${integer.format(value.competitor_position)}` : "";
    details.push(`${shortUrl(value.competitor_domain)}${where}`);
  }
  const shown = details.filter(Boolean).join(", ");
  return shown === "" ? value.phrase : `${value.phrase} (${shown})`;
}

function rankChange(value: unknown): string | null {
  if (!isRecord(value) || typeof value.phrase !== "string") return null;
  const where = value.kind === "local_pack" ? "Kartenliste" : "Suche";
  return `${value.phrase} (${where}): ${place(value.before)} → ${place(value.after)}`;
}

function reasonOf(code: unknown): string {
  return typeof code === "string" ? reasonText(code) : "";
}

function withReason(url: string, code: unknown): string {
  const reason = reasonOf(code);
  return reason === "" ? shortUrl(url) : `${shortUrl(url)}: ${reason}`;
}

function example(item: unknown): string | null {
  if (typeof item === "string") return item === "" ? null : item;
  if (!isRecord(item) || typeof item.excerpt !== "string") return null;
  const who = typeof item.provider === "string" ? `${term(item.provider)}: ` : "";
  const verdict = item.mentioned === true ? " (genannt)" : " (nicht genannt)";
  return `${who}„${item.excerpt}“${verdict}`;
}

function agentState(agent: string, value: unknown): string {
  if (!isRecord(value)) return agent;
  if (value.site_blocked === true) return `${agent}: ganze Website gesperrt`;
  const blocked = value.pages_blocked_count;
  if (isNumber(blocked) && blocked > 0) return `${agent}: ${integer.format(blocked)} Seiten gesperrt`;
  return `${agent}: erlaubt`;
}

function breakdown(phrase: string, value: unknown): string | null {
  const parts = entries(value).flatMap(([where, found]) => {
    if (Array.isArray(found)) return found.length === 0 ? [] : [`${term(where)}: ${integer.format(found.length)}`];
    return typeof found === "string" && found !== "" ? [`${term(where)}: ${found}`] : [];
  });
  return parts.length === 0 ? `${phrase}: nirgends` : `${phrase}: ${parts.join(", ")}`;
}

function adPhrase(phrase: string, value: unknown): string | null {
  if (!isRecord(value)) return null;
  const own = value.customer_shown === true
    ? `eigene Anzeige${isNumber(value.customer_position) ? ` auf Platz ${integer.format(value.customer_position)}` : ""}`
    : "keine eigene Anzeige";
  const rivals = strings(value.competitors);
  return rivals.length === 0 ? `${phrase}: ${own}` : `${phrase}: ${own}, Wettbewerber ${joinListed(rivals.map(shortUrl)) ?? ""}`;
}

const NOTES: Readonly<Record<string, string>> = {
  no_account: "Kein Konto gefunden.",
};

function mapped(value: unknown, show: (entry: [string, unknown]) => string | null): string | null {
  return joinListed(entries(value).flatMap((entry) => show(entry) ?? []), " · ");
}

function listed(value: unknown, show: (item: unknown) => string | null, separator?: string): string | null {
  return Array.isArray(value) ? joinListed(value.flatMap((item: unknown) => show(item) ?? []), separator) : null;
}

const SCALAR: Partial<Record<Kind, (value: number) => string>> = {
  count: (value) => integer.format(value),
  number: (value) => oneDecimal.format(value),
  words: (value) => `${integer.format(value)} Wörter`,
  chars: (value) => `${integer.format(value)} Zeichen`,
  searches: (value) => `${integer.format(value)} Suchen im Monat`,
  ms: (value) => (value >= 1000 ? `${twoDecimals.format(value / 1000)} Sekunden` : `${integer.format(value)} Millisekunden`),
  decimal: (value) => twoDecimals.format(value),
  score: (value) => `${integer.format(value)} von 100`,
  share: (value) => percent(value),
  rating: (value) => `${oneDecimal.format(value)} Sterne`,
  days: (value) => `${oneDecimal.format(value)} ${value === 1 ? "Tag" : "Tage"}`,
  perMonth: (value) => twoDecimals.format(value),
  hours: (value) => `${integer.format(value)} ${value === 1 ? "Stunde" : "Stunden"}`,
};

function formatScalar(kind: Kind, value: unknown): string | null {
  const format = SCALAR[kind];
  if (format === undefined) return null;
  return isNumber(value) ? format(value) : null;
}

function formatText(kind: Kind, value: unknown): string | null {
  if (typeof value !== "string" || value.trim() === "") return null;
  switch (kind) {
    case "text":
      return value;
    case "term":
      return term(value);
    case "url":
      return fullUrl(value);
    case "reason":
      return reasonText(value);
    case "note":
      return NOTES[value] ?? value;
    case "date":
      return formatDate(value.slice(0, 10));
    case "month":
      return /^\d{4}-\d{2}/.test(value) ? `${value.slice(5, 7)}/${value.slice(0, 4)}` : value;
    default:
      return null;
  }
}

function formatList(kind: Kind, value: unknown): string | null {
  switch (kind) {
    case "urls":
      return joinListed(strings(value).map(shortUrl));
    case "hosts":
      return joinListed(strings(value).map((host) => host.replace(/^www\./, "")));
    case "terms":
      return joinListed(strings(value).map(term));
    case "sources":
      return joinListed(strings(value).map((source) => SOURCES[source] ?? source));
    case "quotes":
      return joinListed(strings(value).map((text) => `„${text}“`), " · ");
    case "examples":
      return listed(value, example, " · ");
    case "gaps":
      return listed(value, gap, " · ");
    case "rankChanges":
      return listed(value, rankChange, " · ");
    default:
      return null;
  }
}

const MAP_ENTRIES: Partial<Record<Kind, (entry: [string, unknown]) => string | null>> = {
  positions: ([key, position]) => `${key}: ${place(position)}`,
  counts: ([key, count]) => (isNumber(count) ? `${shortUrl(key)}: ${integer.format(count)}` : null),
  clicks: ([key, count]) => (isNumber(count) ? `${key}: ${integer.format(count)} Klicks` : null),
  shares: ([key, share]) => (isNumber(share) ? `${term(key)}: ${percent(share)}` : null),
  changes: ([key, change]) => `${term(key)}: ${signed(change)}`,
  metrics: ([key, count]) => (isNumber(count) ? `${term(key)}: ${integer.format(count)}` : null),
  urlCounts: ([url, words]) => (isNumber(words) ? `${shortUrl(url)}: ${integer.format(words)}` : null),
  urlReasons: ([url, code]) => withReason(url, code),
  urlDays: ([url, days]) => (isNumber(days) ? `${shortUrl(url)}: ${integer.format(days)} Tage alt` : null),
  urlGroups: ([title, urls]) => `„${title}“: ${joinListed(strings(urls).map(shortUrl)) ?? ""}`,
  listings: ([name, details]) => listing(name, details),
  ratings: ([name, stars]) => (isNumber(stars) ? `${name}: ${oneDecimal.format(stars)} Sterne` : null),
  agentStates: ([agent, state]) => agentState(agent, state),
  phraseBreakdown: ([phrase, where]) => breakdown(phrase, where),
  adPhrases: ([phrase, details]) => adPhrase(phrase, details),
};

function formatSearchTotals(value: unknown): string | null {
  const totals = isRecord(value) ? value : {};
  const shown = SEARCH_TOTALS.flatMap(([key, label, format]) => {
    const total = totals[key];
    return isNumber(total) ? [`${label}: ${format(total)}`] : [];
  });
  return shown.length === 0 ? null : shown.join(" · ");
}

function formatMap(kind: Kind, value: unknown): string | null {
  if (kind === "searchTotals") return formatSearchTotals(value);
  if (kind === "positions" && entries(value).some(([key]) => GRID_KEY.test(key))) return null;
  const entry = MAP_ENTRIES[kind];
  return entry === undefined ? null : mapped(value, entry);
}

function formatValue(kind: Kind, value: unknown): string | null {
  switch (kind) {
    case "hidden":
      return null;
    case "yesno":
      return typeof value === "boolean" ? (value ? "ja" : "nein") : null;
    case "found":
      if (typeof value === "boolean") return value ? "ja" : "nein";
      return joinListed(strings(value).map(term)) ?? "nirgends";
    case "place":
      return place(value);
    case "businesses":
      return isNumber(value) ? integer.format(value) : listed(value, business, " · ");
    case "change":
      return signed(value);
    default:
      return formatScalar(kind, value) ?? formatText(kind, value) ?? formatList(kind, value) ?? formatMap(kind, value);
  }
}

export function formatMeasured(measured: Readonly<Record<string, unknown>>): MeasuredLine[] {
  return Object.entries(FIELDS).flatMap(([key, field]) => {
    if (!(key in measured)) return [];
    const value = formatValue(field.kind, measured[key]);
    return value === null ? [] : [{ key, label: field.label, value }];
  });
}

export interface ReviewExample {
  when: string | null;
  stars: number | null;
  text: string;
}

function reviewWhen(value: unknown): string | null {
  if (typeof value !== "string" || value.trim() === "") return null;
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? formatDate(value) : value.trim();
}

function reviewStars(value: unknown): number | null {
  return isNumber(value) && value >= 0 && value <= 5 ? value : null;
}

export function starsLabel(stars: number): string {
  return `${oneDecimal.format(stars)} von 5 Sternen`;
}

export function unansweredReviews(measured: Readonly<Record<string, unknown>>): ReviewExample[] {
  const examples = measured.unanswered_examples;
  if (!Array.isArray(examples)) return [];
  return examples.flatMap((item: unknown) => {
    if (!isRecord(item)) return [];
    const text = typeof item.text === "string" ? item.text.trim() : "";
    const when = reviewWhen(item.date);
    const stars = reviewStars(item.stars);
    if (text === "" && when === null && stars === null) return [];
    return [{ when, stars, text }];
  });
}

export const MEASURED_KEYS: readonly string[] = Object.keys(FIELDS);
export const REASON_CODES: readonly string[] = Object.keys(REASONS);
