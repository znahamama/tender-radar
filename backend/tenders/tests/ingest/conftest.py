import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
OPEN_FAKE = FIXTURES / "canadabuys_open_fake.csv"
FY_FAKE = FIXTURES / "canadabuys_fy_fake.csv"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Ingest tests must never open a socket (DB access is already blocked by pytest-django)."""

    def guard(*args, **kwargs):
        raise RuntimeError("network access attempted in an ingest test")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
