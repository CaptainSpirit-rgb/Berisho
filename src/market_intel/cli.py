"""Command-line entry point: `market-intel <command>`."""

from __future__ import annotations

import argparse
import logging
import os

from market_intel import config
from market_intel.storage import db

log = logging.getLogger("market_intel")


def cmd_init_db(args: argparse.Namespace) -> int:
    path = args.db or config.db_path()
    con = db.connect(path)
    db.init_schema(con)
    n = db.sync_watchlist(con, config.load_watchlist())
    log.info("Initialised %s (%d tables, %d watchlist tickers)", path, len(db.list_tables(con)), n)
    con.close()
    return 0


def cmd_watchlist(args: argparse.Namespace) -> int:
    for s in config.load_watchlist():
        print(f"{s.symbol:<6} CIK {s.cik or '-':<8} {s.sector_etf or '-':<4} {s.name}")
    return 0


def _open_db(args: argparse.Namespace):
    con = db.connect(args.db or config.db_path())
    db.init_schema(con)
    db.sync_watchlist(con, config.load_watchlist())
    return con


def _price_symbols(settings: dict) -> list[str]:
    """Watchlist plus the regime benchmark (SPY by default)."""
    symbols = [s.symbol for s in config.load_watchlist()]
    benchmark = settings.get("benchmark", {}).get("symbol")
    return symbols + ([benchmark] if benchmark and benchmark not in symbols else [])


def _collect_prices(con, settings: dict) -> bool:
    from market_intel.collectors import prices

    results = prices.collect_prices(
        con,
        _price_symbols(settings),
        lookback_days=int(settings.get("prices", {}).get("lookback_days", 400)),
    )
    failed = [s for s, n in results.items() if n < 0]
    if failed:
        log.error("Price collection failed for: %s", ", ".join(failed))
    return not failed


def _collect_filings(con, settings: dict) -> bool:
    from market_intel.collectors import edgar

    config.load_env()
    sec = settings.get("sec", {})
    try:
        client = edgar.SecClient(
            max_requests_per_second=float(sec.get("max_requests_per_second", 10))
        )
    except edgar.MissingUserAgentError as exc:
        log.error("%s", exc)
        return False
    results = edgar.collect_filings(
        con,
        config.load_watchlist(),
        client,
        forms=sec.get("forms") or edgar.DEFAULT_FORMS,
        lookback_days=int(sec.get("filings_lookback_days", 365)),
        form4_lookback_days=int(sec.get("form4_lookback_days", 90)),
    )
    failed = [r.symbol for r in results if r.error]
    if failed:
        log.error("Filings collection failed for: %s", ", ".join(failed))
    return not failed


def _deliver_digest(con, args: argparse.Namespace) -> bool:
    """Print the digest (--dry-run) or send it to Telegram. If sending fails, the digest is
    printed to the console instead so the run's output isn't lost, and False is returned."""
    from market_intel.delivery import digest

    d = digest.build_digest(con, config.load_watchlist(), config.load_settings())
    text = digest.render_text(d)
    if args.dry_run:
        print(text)
        return True

    from market_intel.delivery import telegram

    config.load_env()
    try:
        client = telegram.TelegramClient(telegram.TelegramConfig.from_env())
        n = client.send_text(text)
    except telegram.TelegramError as exc:
        log.error("%s", exc)
        log.error("Digest not sent; printing it here instead.")
        print(text)
        return False
    log.info("Digest sent to Telegram (%d message%s)", n, "" if n == 1 else "s")
    return True


def _telegram_client():
    from market_intel.delivery import telegram

    config.load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first (from @BotFather).")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return telegram, telegram.TelegramClient(telegram.TelegramConfig(token, chat_id))


def cmd_telegram_chat_id(args: argparse.Namespace) -> int:
    """Print the chat ids of recent conversations with the bot."""
    telegram, client = _telegram_client()
    try:
        bot = client.get_me()
        chats = client.recent_chats()
    except telegram.TelegramError as exc:
        log.error("%s", exc)
        return 1
    print(f"Bot: @{bot.get('username')}")
    if not chats:
        print(
            f"No messages found. Open Telegram, send any message to @{bot.get('username')}, "
            "then run this again."
        )
        return 1
    for chat in chats:
        who = (
            chat.get("title")
            or " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
            or chat.get("username", "")
        )
        print(f"  TELEGRAM_CHAT_ID={chat['id']}   ({chat.get('type')}: {who})")
    print("Copy the line for your own chat into .env.")
    return 0


def cmd_telegram_test(args: argparse.Namespace) -> int:
    """Send a short test message to TELEGRAM_CHAT_ID."""
    telegram, client = _telegram_client()
    if not client.cfg.chat_id:
        raise SystemExit(
            "Set TELEGRAM_CHAT_ID in .env first (run `market-intel telegram-chat-id`)."
        )
    try:
        client.send_text("market-intel: Telegram delivery works.")
    except telegram.TelegramError as exc:
        log.error("%s", exc)
        return 1
    print("Test message sent. Check Telegram.")
    return 0


def cmd_collect_prices(args: argparse.Namespace) -> int:
    con = _open_db(args)
    ok = _collect_prices(con, config.load_settings())
    con.close()
    return 0 if ok else 1


def cmd_collect_filings(args: argparse.Namespace) -> int:
    con = _open_db(args)
    ok = _collect_filings(con, config.load_settings())
    con.close()
    return 0 if ok else 1


def cmd_digest(args: argparse.Namespace) -> int:
    con = _open_db(args)
    ok = _deliver_digest(con, args)
    con.close()
    return 0 if ok else 1


def cmd_run_daily(args: argparse.Namespace) -> int:
    """Collect everything, then send (or with --dry-run, print) the digest. A failed
    collector doesn't stop the digest (it runs on whatever is stored), but the exit code
    reports the failure."""
    settings = config.load_settings()
    con = _open_db(args)
    ok_prices = _collect_prices(con, settings)
    ok_filings = _collect_filings(con, settings)
    ok_sent = _deliver_digest(con, args)
    con.close()
    return 0 if ok_prices and ok_filings and ok_sent else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-intel", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="Create the DuckDB schema and load the watchlist")
    p.add_argument("--db", help="Override the database path")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("watchlist", help="Print the configured watchlist")
    p.set_defaults(func=cmd_watchlist)

    p = sub.add_parser("collect-prices", help="Fetch daily prices for the watchlist + benchmark")
    p.add_argument("--db", help="Override the database path")
    p.set_defaults(func=cmd_collect_prices)

    p = sub.add_parser("collect-filings", help="Fetch SEC EDGAR filings and Form 4 trades")
    p.add_argument("--db", help="Override the database path")
    p.set_defaults(func=cmd_collect_filings)

    for name, func, help_ in (
        ("digest", cmd_digest, "Send the digest built from stored data to Telegram"),
        ("run-daily", cmd_run_daily, "Collect prices and filings, then send the digest"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", help="Override the database path")
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the digest to the console instead of sending it",
        )
        p.set_defaults(func=func)

    p = sub.add_parser("telegram-chat-id", help="Show chat ids that messaged your bot")
    p.set_defaults(func=cmd_telegram_chat_id)

    p = sub.add_parser("telegram-test", help="Send a test message to TELEGRAM_CHAT_ID")
    p.set_defaults(func=cmd_telegram_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
