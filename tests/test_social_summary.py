from datetime import UTC, datetime, timedelta

from radarauditor.collectors.snapshot import (
    SocialAccount,
    SocialGap,
    SocialPost,
    SocialSnapshot,
)
from radarauditor.collectors.social_summary import LATEST_POSTS_SHOWN, render_social

NOW = datetime(2026, 11, 13, tzinfo=UTC)


def _account(**overrides: object) -> SocialAccount:
    values: dict[str, object] = {
        "platform": "instagram",
        "url": "https://www.instagram.com/katjakarsch/",
        "handle": "katjakarsch",
        "source": "legal_notice_link",
        "status": "confirmed",
        "proof": "linked from the legal notice",
        **overrides,
    }
    return SocialAccount.model_validate(values)


def _render(*accounts: SocialAccount, gaps: list[SocialGap] | None = None) -> str:
    return render_social(SocialSnapshot(accounts=list(accounts), gaps=gaps or [], collected_at=NOW))


def test_an_account_shows_source_status_figures_and_business_flag() -> None:
    text = _render(
        _account(
            followers=159,
            following=143,
            posts_count=80,
            is_business=False,
            description="Art\nObjects",
        )
    )

    assert text.splitlines() == [
        "Instagram: katjakarsch (https://www.instagram.com/katjakarsch/)",
        "  Quelle: Link im Impressum; Status: bestätigt",
        "  Nachweis: linked from the legal notice",
        "  Follower: 159; Folgt: 143; Beiträge: 80; Business-Konto: nein",
        "  Beschreibung: Art Objects",
    ]


def test_zero_counts_are_shown_and_missing_ones_are_left_out() -> None:
    text = _render(_account(followers=0, is_business=True, is_private=True))

    assert "  Follower: 0; Business-Konto: ja; Privat: ja" in text.splitlines()
    assert "Folgt" not in text


def test_all_figures_of_the_other_platforms_are_named() -> None:
    text = _render(
        _account(
            platform="facebook",
            connections=298,
            page_likes=395,
            employees=12,
            rating=4.5,
            category="Roofing",
            advertising="no ads",
        )
    )

    assert (
        "  Kontakte: 298; Seiten-Likes: 395; Mitarbeiter: 12; Bewertung: 4.5; "
        "Kategorie: Roofing; Werbung: no ads"
    ) in text.splitlines()
    assert text.startswith("Facebook-Seite:")


def test_posts_are_listed_newest_first_with_their_interactions() -> None:
    older = SocialPost(published_at=NOW - timedelta(days=9), kind="Image", likes=3)
    newer = SocialPost(
        published_at=NOW - timedelta(days=1), kind="Video", likes=19, comments=1, shares=2, views=7
    )
    undated = SocialPost(kind=None)

    text = _render(_account(posts=[older, undated, newer]))

    lines = text.splitlines()
    start = lines.index("  Letzte Beiträge:")
    assert lines[start + 1 :] == [
        "    2026-11-12  Video  19 Likes, 1 Kommentare, 2 Teilungen, 7 Aufrufe",
        "    2026-11-04  Image  3 Likes",
        "    ohne Datum  Beitrag",
    ]


def test_only_the_latest_posts_are_shown() -> None:
    posts = [SocialPost(published_at=NOW - timedelta(days=day), likes=day) for day in range(8)]

    text = _render(_account(posts=posts))

    assert text.count("Likes") == LATEST_POSTS_SHOWN
    assert "    2026-11-13  Beitrag  0 Likes" in text


def test_unsure_and_unreachable_accounts_show_their_reason() -> None:
    unsure = _account(status="unsure", source="google_search", proof=None, reason="no link")
    gone = _account(
        platform="facebook",
        url="https://www.facebook.com/gone",
        handle="gone",
        status="unreachable",
        reason="profile not available",
        proof=None,
    )

    text = _render(unsure, gone)

    assert "Status: unsicher, nicht gemessen" in text
    assert "Quelle: Google-Suche" in text
    assert "  Hinweis: no link" in text
    assert "Status: nicht erreichbar" in text
    assert "Nachweis" not in text


def test_gaps_name_the_platform_and_the_reason() -> None:
    gaps = [
        SocialGap(platform="facebook", reason="no_account_found"),
        SocialGap(platform="linkedin_company", reason="search_failed", detail="HTTP 403"),
    ]

    text = _render(gaps=gaps)

    assert text.splitlines() == [
        "Facebook-Seite: kein Konto gefunden",
        "",
        "LinkedIn-Firmenseite: Google-Suche fehlgeschlagen, nicht vollständig gesucht (HTTP 403)",
    ]


def test_accounts_come_before_gaps_separated_by_a_blank_line() -> None:
    text = _render(_account(), gaps=[SocialGap(platform="x", reason="no_account_found")])

    assert text.splitlines()[-2:] == ["", "X: kein Konto gefunden"]


def test_an_empty_snapshot_renders_nothing() -> None:
    assert _render() == ""
