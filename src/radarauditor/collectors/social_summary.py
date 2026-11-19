from datetime import UTC, datetime

from radarauditor.collectors.snapshot import (
    SocialAccount,
    SocialGap,
    SocialGapReason,
    SocialPlatform,
    SocialPost,
    SocialSnapshot,
    SocialSource,
    SocialStatus,
)

LATEST_POSTS_SHOWN = 5
PLATFORM_LABELS: dict[SocialPlatform, str] = {
    "instagram": "Instagram",
    "facebook": "Facebook-Seite",
    "linkedin_company": "LinkedIn-Firmenseite",
    "linkedin_person": "LinkedIn-Personenprofil",
    "x": "X",
}
SOURCE_LABELS: dict[SocialSource, str] = {
    "website_link": "Link auf der Webseite",
    "legal_notice_link": "Link im Impressum",
    "google_search": "Google-Suche",
}
STATUS_LABELS: dict[SocialStatus, str] = {
    "confirmed": "bestätigt",
    "unsure": "unsicher, nicht gemessen",
    "unreachable": "nicht erreichbar",
}
GAP_LABELS: dict[SocialGapReason, str] = {
    "no_account_found": "kein Konto gefunden",
    "search_failed": "Google-Suche fehlgeschlagen, nicht vollständig gesucht",
}
NEVER = datetime.min.replace(tzinfo=UTC)


def render_social(snapshot: SocialSnapshot) -> str:
    blocks = [_account_lines(account) for account in snapshot.accounts]
    blocks.extend([_gap_line(gap)] for gap in snapshot.gaps)
    return "\n\n".join("\n".join(block) for block in blocks)


def _gap_line(gap: SocialGap) -> str:
    detail = f" ({gap.detail})" if gap.detail else ""
    return f"{PLATFORM_LABELS[gap.platform]}: {GAP_LABELS[gap.reason]}{detail}"


def _account_lines(account: SocialAccount) -> list[str]:
    lines = [
        f"{PLATFORM_LABELS[account.platform]}: {account.handle} ({account.url})",
        f"  Quelle: {SOURCE_LABELS[account.source]}; Status: {STATUS_LABELS[account.status]}",
    ]
    if account.proof:
        lines.append(f"  Nachweis: {account.proof}")
    if account.reason:
        lines.append(f"  Hinweis: {account.reason}")
    figures = _figures(account)
    if figures:
        lines.append("  " + "; ".join(figures))
    if account.description:
        lines.append(f"  Beschreibung: {' '.join(account.description.split())}")
    if account.posts:
        lines.append("  Letzte Beiträge:")
        newest = sorted(account.posts, key=lambda post: post.published_at or NEVER, reverse=True)
        lines.extend(f"    {_post_line(post)}" for post in newest[:LATEST_POSTS_SHOWN])
    return lines


def _figures(account: SocialAccount) -> list[str]:
    numbers = [
        ("Follower", account.followers),
        ("Folgt", account.following),
        ("Kontakte", account.connections),
        ("Beiträge", account.posts_count),
        ("Seiten-Likes", account.page_likes),
        ("Mitarbeiter", account.employees),
        ("Bewertung", account.rating),
    ]
    figures = [f"{label}: {value}" for label, value in numbers if value is not None]
    if account.is_business is not None:
        figures.append(f"Business-Konto: {'ja' if account.is_business else 'nein'}")
    if account.is_private is not None:
        figures.append(f"Privat: {'ja' if account.is_private else 'nein'}")
    if account.category:
        figures.append(f"Kategorie: {account.category}")
    if account.advertising:
        figures.append(f"Werbung: {account.advertising}")
    return figures


def _post_line(post: SocialPost) -> str:
    when = post.published_at.date().isoformat() if post.published_at else "ohne Datum"
    interactions = [
        f"{value} {label}"
        for label, value in (
            ("Likes", post.likes),
            ("Kommentare", post.comments),
            ("Teilungen", post.shares),
            ("Aufrufe", post.views),
        )
        if value is not None
    ]
    parts = [when, post.kind or "Beitrag", ", ".join(interactions)]
    return "  ".join(part for part in parts if part)
