import pytest

from market_intel.storage import db


@pytest.fixture(autouse=True)
def no_real_claude_key(monkeypatch):
    """Tests never call the real Claude API, even when a developer's .env has a key:
    an empty env var wins over .env (load_dotenv doesn't override)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")


@pytest.fixture
def con():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()
