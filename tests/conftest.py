"""Shared fixtures: one small generated database reused by every test module."""
from datetime import date

import pytest

from src import db
from src.generate_data import generate

TODAY = date(2026, 10, 22)


@pytest.fixture(scope="session")
def gen_db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("data") / "test.db"
    generate(n_customers=600, seed=7, today=TODAY, db_path=path)
    return path


@pytest.fixture(scope="session")
def conn(gen_db_path):
    c = db.connect(gen_db_path)
    yield c
    c.close()
