from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, model_validator

from radarauditor.db.app_settings import Source
from radarauditor.feedback.events import RouteReport

DataRoute = Literal["browser", "places_api", "dataforseo"]


LinkKind = Literal["href", "anchor", "javascript", "no_href", "onclick"]


class ImageInfo(BaseModel):
    source: str | None
    file_name: str | None
    alt: str | None
    decorative: bool = False


class LinkInfo(BaseModel):
    kind: LinkKind
    text: str
    url: str | None = None
    in_navigation: bool = False
    image_alt: str | None = None
    aria_label: str | None = None


class PageSnapshot(BaseModel):
    url: HttpUrl
    status_code: int
    title: str | None
    meta_description: str | None
    canonical: HttpUrl | None
    lang: str | None
    headings: list[tuple[int, str]]
    word_count: int
    json_ld: list[dict[str, object]]
    open_graph: dict[str, str]
    internal_links: list[HttpUrl]
    images_without_alt: int
    tel_links: list[str]
    mailto_links: list[str]
    text_excerpt: str
    has_viewport_meta: bool = False
    meta_robots: str | None = None
    x_robots_tag: str | None = None
    external_links: list[HttpUrl] = Field(default_factory=list)
    third_party_hosts: list[str] = Field(default_factory=list)
    script_sources: list[str] | None = None
    inline_script_signatures: list[str] | None = None
    google_tag_ids: list[str] | None = None
    ads_conversions: list[str] | None = None
    meta_robots_bing: str | None = None
    last_modified: str | None = None
    body_text: str | None = None
    first_text: str | None = None
    images: list[ImageInfo] | None = None
    links: list[LinkInfo] | None = None
    has_data_nosnippet: bool = False


LegalNoticeWay = Literal[
    "address", "heading", "link", "foreign_link", "frame", "section", "text", "browser", "direct"
]


class LegalNoticeCandidate(BaseModel):
    url: HttpUrl
    way: LegalNoticeWay


class LegalNotice(BaseModel):
    url: HttpUrl | None = None
    way: LegalNoticeWay | None = None
    rendered: bool = False
    lines: list[str] = Field(default_factory=list)
    candidates: list[LegalNoticeCandidate] = Field(default_factory=list)


class SitemapEntry(BaseModel):
    url: str
    lastmod: str | None = None


class UrlProbe(BaseModel):
    url: str
    status: int | None
    chain: list[int] = Field(default_factory=list)
    location: str | None = None
    final_url: str | None = None
    error: str | None = None


class AgentFetch(BaseModel):
    agent: str
    status: int | None
    text: str = ""
    error: str | None = None


class WebsiteSnapshot(BaseModel):
    start_url: HttpUrl
    uses_https: bool
    redirects_to_single_host: bool
    robots_txt: str | None
    sitemap_urls: list[HttpUrl]
    legal_notice_url: HttpUrl | None
    privacy_url: HttpUrl | None
    footer_nap: dict[str, str]
    opening_hours_text: str | None
    pages: list[PageSnapshot]
    crawl_errors: list[str]
    robots_skipped: list[str] = Field(default_factory=list)
    sitemap_pages: list[str] = Field(default_factory=list)
    crawl_capped: bool = False
    legal_notice: LegalNotice | None = None
    robots_status: int | None = None
    sitemap_entries: list[SitemapEntry] | None = None
    sitemap_probes: list[UrlProbe] | None = None
    variant_probes: list[UrlProbe] | None = None
    agent_fetches: list[AgentFetch] | None = None


FieldScope = Literal["url", "origin"]


class PageSpeedLab(BaseModel):
    runs: int
    lcp_ms: int
    cls: float
    tbt_ms: int


class PageSpeedField(BaseModel):
    scope: FieldScope
    lcp_ms: int | None
    cls: float | None
    inp_ms: int | None


class PageSpeedSnapshot(BaseModel):
    url: HttpUrl
    mobile_performance: int
    desktop_performance: int
    accessibility: int
    lab: PageSpeedLab | None = None
    field_data: PageSpeedField | None = None
    older_format: bool = False

    @model_validator(mode="before")
    @classmethod
    def _mark_single_run_values(cls, data: Any) -> Any:
        if isinstance(data, dict) and "lab" not in data and "lcp_ms" in data:
            return {**data, "older_format": True}
        return data


PLACES_API_FIELDS = frozenset(
    {
        "place_id",
        "name",
        "formatted_address",
        "phone",
        "website",
        "primary_type",
        "types",
        "rating",
        "review_count",
        "has_opening_hours",
        "photo_count",
        "recent_reviews",
        "business_status",
        "google_maps_uri",
    }
)


class PlaceSnapshot(BaseModel):
    place_id: str
    name: str
    formatted_address: str
    phone: str | None
    website: HttpUrl | None
    primary_type: str | None
    types: list[str]
    rating: float | None
    review_count: int
    has_opening_hours: bool
    photo_count: int
    recent_reviews: list[dict[str, object]]
    business_status: str | None
    google_maps_uri: HttpUrl | None = None
    description: str | None = None
    services: list[str] | None = None
    products: list[str] | None = None
    attributes: list[str] | None = None
    service_areas: list[str] | None = None
    post_ages_days: list[int] | None = None
    question_count: int | None = None
    photo_newest_date: date | None = None
    photo_dated_count: int | None = None
    value_routes: dict[str, DataRoute] = Field(default_factory=dict)

    def route_of(self, field: str) -> DataRoute | None:
        if not self.value_routes:
            return "places_api" if field in PLACES_API_FIELDS else None
        return self.value_routes.get(field)


class MapRanking(BaseModel):
    phrase: str
    position: int | None
    listed: list[str] = Field(default_factory=list)
    route: DataRoute


class ListedBusiness(BaseModel):
    name: str
    review_count: int | None
    rating: float | None = None
    place_id: str | None = None
    is_customer: bool = False


class MapResultList(BaseModel):
    phrase: str
    businesses: list[ListedBusiness]
    route: DataRoute


class SerpResult(BaseModel):
    phrase: str
    organic: list[tuple[int, str]]
    local_pack: list[tuple[int, str]]
    ai_overview_present: bool
    ai_overview_mentions: list[str]
    people_also_ask: list[str]
    measured_at: datetime
    engine: Literal["google", "bing"] = "google"
    ads: list[str] = Field(default_factory=list)
    knowledge_panel: bool = False
    vertical: Literal["web", "images"] = "web"
    route: Literal["browser", "dataforseo", "apify"] = "browser"
    returned_keyword: str | None = None
    language_code: str | None = None
    location_code: int | None = None


class PositionChange(BaseModel):
    phrase: str
    kind: Literal["organic", "local_pack"]
    before: int | None
    after: int | None


class RankingHistory(BaseModel):
    previous_audit_id: UUID
    previous_requested_at: datetime
    changes: list[PositionChange]


class DeferredSearch(BaseModel):
    phrase: str
    vertical: Literal["web", "images"] = "web"


class DirectoryEntry(BaseModel):
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None


class DirectoryHit(BaseModel):
    directory: str
    found: bool
    listing_url: HttpUrl | None
    name: str | None
    address: str | None
    phone: str | None
    delivered: int | None = None
    search_text: str | None = None
    entries: list[DirectoryEntry] | None = None


AnswerRoute = Literal["browser", "api", "claude_code", "manual", "dataforseo"]


class AssistantAnswer(BaseModel):
    provider: str
    question: str
    mentioned: bool
    cited_sources: list[str]
    answer_excerpt: str
    route: AnswerRoute | None = None


class GridPoint(BaseModel):
    key: str = Field(pattern=r"^r[1-9]\d*c[1-9]\d*$")
    latitude: float
    longitude: float
    position: int | None
    route: DataRoute | None = None


class SearchQuery(BaseModel):
    query: str
    clicks: int
    impressions: int
    ctr: float
    position: float


class AnalyticsSnapshot(BaseModel):
    property_id: str
    sessions: int
    engaged_sessions: int
    key_events: int


class AccessSnapshot(BaseModel):
    profile_metrics: dict[str, int] = Field(default_factory=dict)
    profile_metrics_previous: dict[str, int] = Field(default_factory=dict)
    search_queries: list[SearchQuery] = Field(default_factory=list)
    search_totals: dict[str, float] = Field(default_factory=dict)
    analytics: AnalyticsSnapshot | None = None
    owner_reviews: list[dict[str, object]] = Field(default_factory=list)
    collected_at: datetime


class BacklinkSummary(BaseModel):
    domain: str
    referring_domains: int
    referring_domains_local_share: float | None = None
    spam_score: int | None = None
    referring_domains_spam_share: float | None = None


class GapKeyword(BaseModel):
    phrase: str
    volume: int
    competitor_domain: str
    competitor_position: int | None


class PaidSnapshot(BaseModel):
    customer_domain: str | None = None
    volumes: dict[str, int] = Field(default_factory=dict)
    primary_phrases: list[str] = Field(default_factory=list)
    backlinks: dict[str, BacklinkSummary] = Field(default_factory=dict)
    ranked_keywords: dict[str, int] = Field(default_factory=dict)
    keyword_gap: list[GapKeyword] | None = None
    historic_visibility: list[tuple[str, float]] = Field(default_factory=list)
    collected_at: datetime


IdentifiedField = Literal[
    "name", "street", "postal_code", "city", "phone", "google_place_id", "industry", "keywords"
]

IdentificationSource = Literal[
    "json_ld",
    "impressum",
    "footer",
    "google_profile",
    "google_types",
    "name_words",
    "industry_services",
]


class GoogleAd(BaseModel):
    advertiser_id: str | None = None
    creative_id: str | None = None
    advertiser_name: str | None = None
    format: str | None = None
    verified: bool | None = None
    first_shown: str | None = None
    last_shown: str | None = None
    url: str | None = None


class GoogleAdsSnapshot(BaseModel):
    target: str
    window_days: int
    location_code: int
    returned_location_code: int | None = None
    ads: list[GoogleAd]
    items_count: int | None = None
    format_problem: str | None = None
    raw_items: list[dict[str, Any]] = Field(default_factory=list)
    measured_at: datetime


class MetaAd(BaseModel):
    ad_id: str | None = None
    page_name: str | None = None


class MetaAdsSnapshot(BaseModel):
    searched_name: str
    actor: str
    confirmed: bool
    own_ads: list[MetaAd] = Field(default_factory=list)
    other_pages: list[str] = Field(default_factory=list)
    competitor_counts: dict[str, int] = Field(default_factory=dict)
    items_total: int = 0
    raw_items: list[dict[str, Any]] = Field(default_factory=list)
    measured_at: datetime


class IdentifiedValue(BaseModel):
    field: IdentifiedField
    value: str
    source: IdentificationSource


class RouteCheck(BaseModel):
    route: str
    ok: bool
    detail: str | None = None


class BusinessFacts(BaseModel):
    name: str | None = None
    city: str | None = None
    street: str | None = None
    postal_code: str | None = None
    phone: str | None = None


class PhaseTiming(BaseModel):
    phase: str
    seconds: float
    answers: int | None = None


SocialPlatform = Literal["instagram", "facebook", "linkedin_company", "linkedin_person", "x"]
SocialSource = Literal["website_link", "legal_notice_link", "google_search"]
SocialStatus = Literal["confirmed", "unsure", "unreachable"]
SocialGapReason = Literal["no_account_found", "search_failed"]


class SocialPost(BaseModel):
    published_at: datetime | None = None
    kind: str | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None


class SocialMeasurement(BaseModel):
    name: str | None = None
    description: str | None = None
    followers: int | None = None
    following: int | None = None
    connections: int | None = None
    posts_count: int | None = None
    page_likes: int | None = None
    employees: int | None = None
    is_business: bool | None = None
    category: str | None = None
    is_private: bool | None = None
    advertising: str | None = None
    rating: float | None = None
    website: str | None = None
    posts: list[SocialPost] = Field(default_factory=list)


class SocialAccount(SocialMeasurement):
    platform: SocialPlatform
    url: str
    handle: str
    source: SocialSource
    status: SocialStatus
    proof: str | None = None
    reason: str | None = None
    evidence_websites: list[str] = Field(default_factory=list)
    evidence_phones: list[str] = Field(default_factory=list)
    evidence_places: list[str] = Field(default_factory=list)


class SocialGap(BaseModel):
    platform: SocialPlatform
    reason: SocialGapReason
    detail: str | None = None


class SocialSnapshot(BaseModel):
    accounts: list[SocialAccount]
    gaps: list[SocialGap]
    collected_at: datetime


class MeasurementRecord(BaseModel):
    step: str
    supplier: str
    route: str
    measured_at: datetime
    technical_status: str
    record_count: int
    valid: bool
    reason: str


class RawAudit(BaseModel):
    website: WebsiteSnapshot | None
    pagespeed: PageSpeedSnapshot | None
    place: PlaceSnapshot | None
    competitor_places: list[PlaceSnapshot]
    serps: list[SerpResult]
    directories: list[DirectoryHit]
    assistant_answers: list[AssistantAnswer]
    collected_at: datetime
    warnings: list[str]
    grid: list[GridPoint] = Field(default_factory=list)
    access: AccessSnapshot | None = None
    paid: PaidSnapshot | None = None
    capped_sources: list[Source] = Field(default_factory=list)
    cost_capped_sources: list[Source] = Field(default_factory=list)
    deferred_searches: list[DeferredSearch] = Field(default_factory=list)
    domain_first_capture: date | None = None
    map_rankings: list[MapRanking] = Field(default_factory=list)
    map_results: MapResultList | None = None
    identified: list[IdentifiedValue] = Field(default_factory=list)
    timings: list[PhaseTiming] = Field(default_factory=list)
    preflight: list[RouteCheck] = Field(default_factory=list)
    business: BusinessFacts | None = None
    profile_missing: bool = False
    route_reports: list[RouteReport] = Field(default_factory=list)
    social: SocialSnapshot | None = None
    ranking_history: RankingHistory | None = None
    measurements: list[MeasurementRecord] = Field(default_factory=list)
    has_location: bool | None = None
    ads_google: GoogleAdsSnapshot | None = None
    ads_meta: MetaAdsSnapshot | None = None
