"""DB errors must not quote bound values (job titles, descriptions) into public logs."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jobagent.db.session import make_engine
from tests.db.conftest import TEST_DATABASE_URL, needs_db


def test_engines_hide_parameters() -> None:
    assert make_engine("postgresql://user:pw@localhost/none").hide_parameters is True


@needs_db
def test_a_failing_statement_does_not_show_its_values() -> None:
    engine = make_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as conn, pytest.raises(DBAPIError) as info:
            conn.execute(text("SELECT CAST(:value AS integer)"), {"value": "PRIVATE-JOB-TITLE"})
    finally:
        engine.dispose()
    message = str(info.value)
    assert "PRIVATE-JOB-TITLE" not in message.split("[SQL:")[-1]  # not in the parameters section
    assert "hidden due to hide_parameters" in message
