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
TEMPLATE = "radar_through_023"
SERVICE = Caller("service_role")

pytestmark = [
    pytest.mark.skipif(
        BIN_DIR is None, reason=f"No PostgreSQL binaries; set {BIN_DIR_VARIABLE} to their directory"
    ),
    pytest.mark.skipif(runs_as_root(), reason="PostgreSQL refuses to run as root"),
]

INSERT_EVENT = (
    "insert into public.audit_events (audit_id, occurred_at, kind, phase, result, route, label, "
    "protocol) select id, now(), 'measurement', 'messung', :'result', 'messung:website', "
    "'Website', cast(:'protocol' as jsonb) from public.audits"
)
INSERT_CHECK = (
    "insert into public.check_results (audit_id, check_id, area, outcome, weight{columns}) "
    "select id, 'f1', 'f', cast(:'outcome' as public.check_outcome), 1{values} from public.audits"
)
SELECT_CHECK = "select {column} from public.check_results"
ALL_VERDICTS_DONE = (
    ", control_state, verdict_collected_ok, verdict_collected_evidence, verdict_data_ok, "
    "verdict_data_evidence, verdict_evaluation_ok, verdict_evaluation_evidence",
    ", 'done', true, 'a', true, 'b', true, 'c'",
)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresCluster]:
    assert BIN_DIR is not None
    with PostgresCluster(BIN_DIR, tmp_path_factory.mktemp("postgres")) as running:
        yield running


@pytest.fixture(scope="module")
def template(cluster: PostgresCluster) -> str:
    build_project(cluster.create_database(TEMPLATE), migrations_through("023"))
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


def _insert_check(
    database: Database, outcome: str = "passed", extra: tuple[str, str] = ("", "")
) -> None:
    columns, values = extra
    database.run(INSERT_CHECK.format(columns=columns, values=values), SERVICE, outcome=outcome)


@pytest.mark.parametrize("result", ["läuft", "gültig", "ungültig"])
def test_a_measurement_event_carries_its_protocol(database: Database, result: str) -> None:
    database.run(INSERT_EVENT, SERVICE, result=result, protocol='{"step": "website"}')

    assert database.value("select protocol->>'step' from public.audit_events") == "website"
    assert database.value("select result from public.audit_events") == result


def test_a_protocol_must_be_an_object(database: Database) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        database.run(INSERT_EVENT, SERVICE, result="gültig", protocol="[1]")


def test_a_result_outside_the_known_words_is_still_refused(database: Database) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        database.run(INSERT_EVENT, SERVICE, result="vielleicht", protocol="{}")


def test_a_check_can_be_not_applicable(database: Database) -> None:
    _insert_check(database, "not_applicable")

    assert database.value(SELECT_CHECK.format(column="outcome::text")) == "not_applicable"


def test_a_new_check_result_is_open_without_attempts(database: Database) -> None:
    _insert_check(database)

    assert database.rows(
        "select control_state, control_attempt, control_last_error, provider "
        "from public.check_results"
    ) == [
        {
            "control_state": "open",
            "control_attempt": 0,
            "control_last_error": None,
            "provider": None,
        }
    ]


def test_a_check_result_keeps_its_provider(database: Database) -> None:
    _insert_check(database, extra=(", provider", ", 'google'"))

    assert database.value(SELECT_CHECK.format(column="provider")) == "google"


def test_a_provider_must_be_a_lower_case_word(database: Database) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert_check(database, extra=(", provider", ", 'Google Maps'"))


@pytest.mark.parametrize("name", ["collected", "data", "evaluation"])
def test_a_confirmed_verdict_needs_its_evidence(database: Database, name: str) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert_check(database, extra=(f", verdict_{name}_ok", ", true"))


@pytest.mark.parametrize("name", ["collected", "data", "evaluation"])
def test_a_rejected_verdict_needs_its_reason(database: Database, name: str) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert_check(database, extra=(f", verdict_{name}_ok", ", false"))


@pytest.mark.parametrize("name", ["collected", "data", "evaluation"])
def test_a_verdict_with_evidence_or_reason_is_stored(database: Database, name: str) -> None:
    _insert_check(
        database,
        extra=(
            f", verdict_{name}_ok, verdict_{name}_evidence",
            ", true, 'recorded in the protocol'",
        ),
    )

    assert database.value(SELECT_CHECK.format(column=f"verdict_{name}_ok")) is True


def test_a_check_is_done_with_all_three_verdicts(database: Database) -> None:
    _insert_check(database, extra=ALL_VERDICTS_DONE)

    assert database.value(SELECT_CHECK.format(column="control_state")) == "done"


@pytest.mark.parametrize("missing", ["collected", "data", "evaluation"])
def test_a_check_is_not_done_while_a_verdict_is_missing(database: Database, missing: str) -> None:
    columns, values = ALL_VERDICTS_DONE
    kept = [
        (column, value)
        for column, value in zip(columns.split(", ")[1:], values.split(", ")[1:], strict=True)
        if f"verdict_{missing}" not in column
    ]
    extra = ("".join(f", {column}" for column, _ in kept), "".join(f", {v}" for _, v in kept))

    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert_check(database, extra=extra)


def test_a_control_state_outside_open_and_done_is_refused(database: Database) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert_check(database, extra=(", control_state", ", 'pending'"))


def test_the_attempts_cannot_be_negative(database: Database) -> None:
    with pytest.raises(DatabaseError, match="violates check constraint"):
        _insert_check(database, extra=(", control_attempt", ", -1"))


def test_running_023_again_changes_nothing(database: Database) -> None:
    before = schema_fingerprint(database)
    database.run_file(migration("023"))
    assert schema_fingerprint(database) == before
