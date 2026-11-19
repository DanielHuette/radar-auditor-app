"""Migration 017 (events after every measurement, audit status halted) against a real PostgreSQL.

Context: The worker writes the events with the service key, the app reads them, nobody else may.
Skipped when no PostgreSQL binaries are found (set RADAR_PG_BIN to their directory). |
Input: supabase/migrations 001-017. | Output: Assertions.
"""

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
from db.supabase import (
    ANON,
    READER,
    build_project,
    migration,
    migrations_through,
    schema_fingerprint,
)

BIN_DIR = find_bin_dir()
TEMPLATE = "radar_through_017"
SERVICE = Caller("service_role")

pytestmark = [
    pytest.mark.skipif(
        BIN_DIR is None, reason=f"No PostgreSQL binaries; set {BIN_DIR_VARIABLE} to their directory"
    ),
    pytest.mark.skipif(runs_as_root(), reason="PostgreSQL refuses to run as root"),
]

INSERT_EVENT = (
    "insert into public.audit_events (audit_id, occurred_at, kind, phase, result, reason, stage, "
    "route, label, checks) select id, now(), 'route', 'messung', :'result', 'Google zeigt eine "
    "Sicherheitsabfrage', 1, 'google_search', 'Google-Suche', '[]' from public.audits"
)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresCluster]:
    assert BIN_DIR is not None
    with PostgresCluster(BIN_DIR, tmp_path_factory.mktemp("postgres")) as running:
        yield running


@pytest.fixture(scope="module")
def template(cluster: PostgresCluster) -> str:
    build_project(cluster.create_database(TEMPLATE), migrations_through("017"))
    return TEMPLATE


@pytest.fixture
def database(cluster: PostgresCluster, template: str) -> Iterator[Database]:
    """A fresh copy of the project after 017 with one customer and one audit."""
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


def test_an_audit_can_wait_for_daniel(database: Database) -> None:
    database.run("update public.audits set status = 'halted'")

    assert database.value("select status::text from public.audits") == "halted"


def test_the_worker_writes_events_and_the_app_reads_them(database: Database) -> None:
    database.run(INSERT_EVENT, SERVICE, result="fehlgeschlagen")

    assert database.rows("select route, result, stage from public.audit_events", READER) == [
        {"route": "google_search", "result": "fehlgeschlagen", "stage": 1}
    ]


def test_the_app_cannot_change_events(database: Database) -> None:
    database.run(INSERT_EVENT, SERVICE, result="gemessen")

    for statement in (
        INSERT_EVENT,
        "update public.audit_events set result = 'gemessen'",
        "delete from public.audit_events",
    ):
        with pytest.raises(DatabaseError, match="permission denied"):
            database.run(statement, READER, result="gemessen")
    with pytest.raises(DatabaseError, match="permission denied"):
        database.run("select count(*) from public.audit_events", ANON)


def test_a_result_outside_the_five_words_is_refused(database: Database) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        database.run(INSERT_EVENT, SERVICE, result="vielleicht")


def test_events_go_with_their_audit(database: Database) -> None:
    database.run(INSERT_EVENT, SERVICE, result="gemessen")

    database.run("delete from public.audits")

    assert database.value("select count(*) from public.audit_events") == 0


def test_running_017_again_changes_nothing(database: Database) -> None:
    before = schema_fingerprint(database)
    database.run_file(migration("017"))
    assert schema_fingerprint(database) == before
