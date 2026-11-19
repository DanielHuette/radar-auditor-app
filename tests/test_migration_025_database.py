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
TEMPLATE = "radar_through_025"
SERVICE = Caller("service_role")

pytestmark = [
    pytest.mark.skipif(
        BIN_DIR is None, reason=f"No PostgreSQL binaries; set {BIN_DIR_VARIABLE} to their directory"
    ),
    pytest.mark.skipif(runs_as_root(), reason="PostgreSQL refuses to run as root"),
]

INSERT_CHECK = (
    "insert into public.check_results "
    "(audit_id, check_id, area, outcome, weight, control_state{columns}) "
    "select id, 'f1', 'google', '{outcome}', 1, 'done'{values} from public.audits"
)
FIRST_TICKED = (", verdict_collected_ok, verdict_collected_evidence", ", true, 'proof'")
FIRST_CROSSED = (", verdict_collected_ok, verdict_collected_reason", ", false, 'no answer'")


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresCluster]:
    assert BIN_DIR is not None
    with PostgresCluster(BIN_DIR, tmp_path_factory.mktemp("postgres")) as running:
        yield running


@pytest.fixture(scope="module")
def template(cluster: PostgresCluster) -> str:
    build_project(cluster.create_database(TEMPLATE), migrations_through("025"))
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


def _insert(database: Database, outcome: str, extra: tuple[str, str]) -> None:
    columns, values = extra
    database.run(INSERT_CHECK.format(outcome=outcome, columns=columns, values=values), SERVICE)


def test_a_not_measured_check_is_done_with_a_ticked_first_verdict_and_empty_others(
    database: Database,
) -> None:
    _insert(database, "not_measurable", FIRST_TICKED)

    assert database.value("select control_state from public.check_results") == "done"


def test_a_measured_check_is_not_done_with_a_ticked_first_verdict_and_empty_others(
    database: Database,
) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert(database, "passed", FIRST_TICKED)


def test_a_not_measured_check_is_not_done_without_the_first_verdict(database: Database) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert(database, "not_measurable", ("", ""))


def test_a_crossed_first_verdict_is_done_for_every_outcome(database: Database) -> None:
    _insert(database, "failed", FIRST_CROSSED)

    assert database.value("select control_state from public.check_results") == "done"


def test_running_025_again_changes_nothing(database: Database) -> None:
    before = schema_fingerprint(database)
    database.run_file(migration("025"))
    assert schema_fingerprint(database) == before
