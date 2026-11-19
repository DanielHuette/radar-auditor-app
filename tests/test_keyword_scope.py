from uuid import uuid4

import pytest

from radarauditor.db.models import Keyword, KeywordScope


def _keyword(scope: KeywordScope | None) -> Keyword:
    row = {
        "id": uuid4(),
        "customer_id": uuid4(),
        "phrase": "abstrakte kunst",
        "city": "M\u00fcnster",
        "monthly_volume": None,
        "is_primary": False,
    }
    if scope is not None:
        row["scope"] = scope
    return Keyword.model_validate(row)


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (KeywordScope.LOCAL, "abstrakte kunst M\u00fcnster"),
        (KeywordScope.NATIONWIDE, "abstrakte kunst"),
        (None, "abstrakte kunst M\u00fcnster"),
    ],
)
def test_the_search_phrase_carries_the_city_only_for_local_keywords(
    scope: KeywordScope | None, expected: str
) -> None:
    assert _keyword(scope).search_phrase == expected


def test_a_stored_row_with_the_scope_text_is_read() -> None:
    row = {
        "id": uuid4(),
        "customer_id": uuid4(),
        "phrase": "kunst",
        "city": "M\u00fcnster",
        "monthly_volume": None,
        "is_primary": False,
        "scope": "nationwide",
    }

    assert Keyword.model_validate(row).scope is KeywordScope.NATIONWIDE


def test_an_unknown_scope_is_refused() -> None:
    row = {
        "id": uuid4(),
        "customer_id": uuid4(),
        "phrase": "kunst",
        "city": "M\u00fcnster",
        "monthly_volume": None,
        "is_primary": False,
        "scope": "regional",
    }

    with pytest.raises(ValueError, match="scope"):
        Keyword.model_validate(row)
