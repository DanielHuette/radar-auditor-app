from collections.abc import Iterator
from uuid import uuid4

import pytest
from db.postgres import (
    BIN_DIR_VARIABLE,
    Caller,
    Database,
    DatabaseError,
    PostgresCluster,
    find_bin_dir,
    runs_as_root,
)
from db.supabase import build_project, migration, migrations_through, schema_fingerprint

BIN_DIR = find_bin_dir()
TEMPLATE = "radar_through_024"
SERVICE = Caller("service_role")

pytestmark = [
    pytest.mark.skipif(
        BIN_DIR is None, reason=f"No PostgreSQL binaries; set {BIN_DIR_VARIABLE} to their directory"
    ),
    pytest.mark.skipif(runs_as_root(), reason="PostgreSQL refuses to run as root"),
]

INSERT_CHECK = (
    "insert into public.check_results (audit_id, check_id, area, outcome, weight{columns}) "
    "select id, 'f1', 'f', 'passed', 1{values} from public.audits"
)
SELECT_CHECK = "select {column} from public.check_results"
CROSSED_FIRST = (
    ", control_state, verdict_collected_ok, verdict_collected_reason",
    ", 'done', false, 'no answer'",
)
TICKED = {
    "collected": "verdict_collected_ok, verdict_collected_evidence",
    "data": "verdict_data_ok, verdict_data_evidence",
    "evaluation": "verdict_evaluation_ok, verdict_evaluation_evidence",
}


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresCluster]:
    assert BIN_DIR is not None
    with PostgresCluster(BIN_DIR, tmp_path_factory.mktemp("postgres")) as running:
        yield running


@pytest.fixture(scope="module")
def template(cluster: PostgresCluster) -> str:
    build_project(cluster.create_database(TEMPLATE), migrations_through("024"))
    return TEMPLATE


@pytest.fixture
def database(cluster: PostgresCluster, template: str) -> Iterator[Database]:
    database = cluster.create_database(f"test_{uuid4().hex}", template=template)
    database.run(
        "insert into public.customers (name, industry, city) "
        "values ('Katja Karsch', 'Maler', 'Münster')"
    )
    database.run("insert into public.audits (customer_id) select id from public.customers")
    try:
        yield database
    finally:
        cluster.drop_database(database.name)


def _insert_check(database: Database, extra: tuple[str, str] = ("", "")) -> None:
    columns, values = extra
    database.run(INSERT_CHECK.format(columns=columns, values=values), SERVICE)


def _set_disabled(database: Database, ids: str) -> None:
    database.run(f"update public.audits set disabled_checks = {ids}", SERVICE)


def test_an_audit_can_be_in_the_controlling_state(database: Database) -> None:
    database.run("update public.audits set status = 'controlling'", SERVICE)

    assert database.value("select status::text from public.audits") == "controlling"


def test_a_check_is_done_when_the_first_verdict_is_a_cross_and_the_others_are_empty(
    database: Database,
) -> None:
    _insert_check(database, CROSSED_FIRST)

    assert database.rows(
        "select control_state, verdict_collected_ok, verdict_data_ok, verdict_evaluation_ok "
        "from public.check_results"
    ) == [
        {
            "control_state": "done",
            "verdict_collected_ok": False,
            "verdict_data_ok": None,
            "verdict_evaluation_ok": None,
        }
    ]


def test_a_check_is_not_done_without_the_first_verdict(database: Database) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert_check(database, (", control_state", ", 'done'"))


@pytest.mark.parametrize("missing", ["data", "evaluation"])
def test_a_ticked_first_verdict_still_needs_the_other_two_to_be_done(
    database: Database, missing: str
) -> None:
    given = [name for name in TICKED if name != missing]
    columns = ", ".join(TICKED[name] for name in given)
    values = ", ".join("true, 'proof'" for _ in given)

    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert_check(database, (f", control_state, {columns}", f", 'done', {values}"))


def test_a_check_with_three_ticks_is_done(database: Database) -> None:
    columns = ", ".join(TICKED.values())
    values = ", ".join("true, 'proof'" for _ in TICKED)

    _insert_check(database, (f", control_state, {columns}", f", 'done', {values}"))

    assert database.value(SELECT_CHECK.format(column="control_state")) == "done"


@pytest.mark.parametrize(
    ("ids", "count"),
    [
        ("array['a2']", 1),
        ("array['a2','b10','p1']", 3),
        ("array['z99']", 1),
        ("array[]::text[]", 0),
    ],
)
def test_a_selection_of_check_ids_is_stored(database: Database, ids: str, count: int) -> None:
    _set_disabled(database, ids)

    assert database.value("select cardinality(disabled_checks) from public.audits") == count


@pytest.mark.parametrize(
    "ids",
    [
        "array['A2']",
        "array['2a']",
        "array['a']",
        "array['a2','']",
        "array['a2 ']",
        "array['a2',null]",
    ],
)
def test_an_id_that_is_not_a_letter_and_a_number_is_refused(database: Database, ids: str) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        _set_disabled(database, ids)


def test_running_024_again_changes_nothing(database: Database) -> None:
    before = schema_fingerprint(database)
    database.run_file(migration("024"))
    assert schema_fingerprint(database) == before
