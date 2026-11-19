from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl
from typer.testing import CliRunner

from radarauditor import cli
from radarauditor.collectors import social
from radarauditor.collectors.snapshot import (
    RawAudit,
    SocialAccount,
    SocialGap,
    SocialSnapshot,
)
from radarauditor.config import Settings

runner = CliRunner()

SNAPSHOT = SocialSnapshot(
    accounts=[
        SocialAccount(
            platform="instagram",
            url="https://www.instagram.com/katjakarsch/",
            handle="katjakarsch",
            source="website_link",
            status="confirmed",
            followers=159,
        )
    ],
    gaps=[SocialGap(platform="x", reason="no_account_found")],
    collected_at=datetime(2026, 11, 13, tzinfo=UTC),
)


@pytest.fixture
def measured(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, list[str], int]]:
    calls: list[tuple[str, list[str], int]] = []

    async def measure_website(
        settings: Settings, start_url: HttpUrl, names: list[str], max_pages: int
    ) -> SocialSnapshot:
        calls.append((str(start_url), list(names), max_pages))
        return SNAPSHOT

    monkeypatch.setattr(social, "measure_website", measure_website)
    return calls


def test_social_check_prints_accounts_and_gaps(measured: list[tuple[str, list[str], int]]) -> None:
    result = runner.invoke(cli.app, ["social", "check", "https://www.katjakarsch.de/"])

    assert result.exit_code == 0
    assert "Instagram: katjakarsch" in result.stdout
    assert "Follower: 159" in result.stdout
    assert "X: kein Konto gefunden" in result.stdout
    assert measured == [("https://www.katjakarsch.de/", [], 20)]


def test_social_check_passes_names_and_page_limit(
    measured: list[tuple[str, list[str], int]],
) -> None:
    result = runner.invoke(
        cli.app,
        [
            "social",
            "check",
            "https://www.katjakarsch.de/",
            "--name",
            "Katja Karsch",
            "--name",
            "Atelier",
            "--max-pages",
            "5",
        ],
    )

    assert result.exit_code == 0
    assert measured == [("https://www.katjakarsch.de/", ["Katja Karsch", "Atelier"], 5)]


def test_social_check_refuses_a_page_limit_below_one(
    measured: list[tuple[str, list[str], int]],
) -> None:
    result = runner.invoke(cli.app, ["social", "check", "https://x.de/", "--max-pages", "0"])

    assert result.exit_code != 0
    assert measured == []


def test_social_check_refuses_an_address_that_is_not_a_web_address(
    measured: list[tuple[str, list[str], int]],
) -> None:
    result = runner.invoke(cli.app, ["social", "check", "katjakarsch"])

    assert result.exit_code != 0
    assert "not a web address" in result.output
    assert measured == []


def test_social_check_reports_a_missing_token_and_exits_with_one(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crawl(url: HttpUrl, max_pages: int) -> None:
        raise AssertionError("crawled")

    monkeypatch.setattr(social, "crawl_website", crawl)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)

    result = runner.invoke(cli.app, ["social", "check", "https://www.katjakarsch.de/"])

    assert result.exit_code == 1
    assert "APIFY_TOKEN is not set" in result.stderr


def test_the_raw_audit_keeps_the_social_snapshot_through_json(
    sample_raw_audit: RawAudit,
) -> None:
    assert sample_raw_audit.social is None

    stored = sample_raw_audit.model_copy(update={"social": SNAPSHOT}).model_dump_json()

    assert RawAudit.model_validate_json(stored).social == SNAPSHOT
