import pytest

from ladder import engine


@pytest.fixture(autouse=True)
def no_daily_drop_unless_asked(monkeypatch):
    """Tests use fixed timestamps and the real clock; keep the daily drop out of them
    unless a test turns it on (see the decay tests)."""
    monkeypatch.setattr(engine, "DECAY_FROM", "2100-01-01")
