import pytest
import requests

from market_intel import cli, config
from market_intel.delivery import telegram

CFG = telegram.TelegramConfig(token="123:SECRET", chat_id="42")


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = {"ok": True, "result": {}} if data is None else data

    def json(self):
        return self._data


class FakeSession:
    """Returns queued responses (or raises queued exceptions) and records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        r = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(r, Exception):
            raise r
        return r


def client(*responses):
    sleeps = []
    c = telegram.TelegramClient(CFG, session=FakeSession(*responses), sleep=sleeps.append)
    return c, c.session, sleeps


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " 123:abc ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    assert telegram.TelegramConfig.from_env() == telegram.TelegramConfig("123:abc", "-100")


def test_config_missing_names_the_variables(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(telegram.MissingTelegramConfigError, match="TELEGRAM_CHAT_ID"):
        telegram.TelegramConfig.from_env()


def test_short_text_is_one_escaped_pre_message():
    [msg] = telegram.format_messages("S&P <500>\n  AAPL  +1.0%")
    assert msg == "<pre>S&amp;P &lt;500&gt;\n  AAPL  +1.0%</pre>"


def test_long_text_splits_on_lines_under_the_limit():
    lines = [f"line {i:04d} " + "x" * 80 for i in range(200)]  # ~18k chars
    msgs = telegram.format_messages("\n".join(lines))
    assert len(msgs) > 1
    assert all(len(m) <= telegram.MAX_MESSAGE_CHARS for m in msgs)
    assert msgs[0].startswith(f"(1/{len(msgs)})\n<pre>")
    # Every line survives intact and in order.
    rejoined = "\n".join(m.split("<pre>", 1)[1].removesuffix("</pre>") for m in msgs)
    assert rejoined.split("\n") == lines


def test_oversized_line_with_ampersands_is_hard_cut():
    msgs = telegram.format_messages("&" * 10_000)
    assert all(len(m) <= telegram.MAX_MESSAGE_CHARS for m in msgs)
    assert sum(m.count("&amp;") for m in msgs) == 10_000


def test_send_text_posts_html_to_chat():
    c, session, _ = client()
    assert c.send_text("hello") == 1
    [(url, payload)] = session.calls
    assert url == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert payload == {
        "chat_id": "42",
        "text": "<pre>hello</pre>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


def test_retries_after_rate_limit():
    limited = FakeResponse(429, {"ok": False, "parameters": {"retry_after": 7}})
    c, session, sleeps = client(limited, FakeResponse())
    c.send_message("<pre>x</pre>")
    assert len(session.calls) == 2 and sleeps == [7.0]


def test_api_error_raises_with_description():
    c, _, _ = client(FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"}))
    with pytest.raises(telegram.TelegramError, match="chat not found"):
        c.send_message("x")


def test_network_error_never_leaks_the_token():
    err = requests.ConnectionError("https://api.telegram.org/bot123:SECRET/sendMessage")
    c, session, _ = client(err, err, err, err)
    with pytest.raises(telegram.TelegramError) as exc_info:
        c.send_message("x")
    assert "SECRET" not in str(exc_info.value) and len(session.calls) == 4


def test_recent_chats_dedupes_and_keeps_latest():
    updates = [
        {"message": {"chat": {"id": 1, "type": "private", "first_name": "Luka"}}},
        {"channel_post": {"chat": {"id": -100, "type": "channel", "title": "Digest"}}},
        {"message": {"chat": {"id": 1, "type": "private", "first_name": "Luka"}}},
        {"my_chat_member": {}},
    ]
    c, _, _ = client(FakeResponse(data={"ok": True, "result": updates}))
    assert [ch["id"] for ch in c.recent_chats()] == [-100, 1]


# --- CLI wiring -------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_env", lambda: None)  # ignore any real .env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent = []

    def fake_send_text(self, text):
        sent.append(text)
        return 1

    monkeypatch.setattr(telegram.TelegramClient, "send_text", fake_send_text)
    return str(tmp_path / "t.duckdb"), sent


def test_dry_run_prints_and_does_not_send(cli_env, capsys):
    db, sent = cli_env
    assert cli.main(["digest", "--dry-run", "--db", db]) == 0
    assert "MARKET INTEL DIGEST" in capsys.readouterr().out
    assert sent == []


def test_digest_is_sent_to_telegram(cli_env, capsys):
    db, sent = cli_env
    assert cli.main(["digest", "--db", db]) == 0
    assert len(sent) == 1 and "MARKET INTEL DIGEST" in sent[0]
    assert capsys.readouterr().out == ""


def test_missing_config_prints_digest_and_fails(cli_env, monkeypatch, capsys):
    db, sent = cli_env
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert cli.main(["digest", "--db", db]) == 1
    assert "MARKET INTEL DIGEST" in capsys.readouterr().out
    assert sent == []
