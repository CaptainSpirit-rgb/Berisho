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

    # Text of recent 8-Ks (main document + press-release exhibits) for the briefs.
    import datetime as dt

    from market_intel.collectors import filing_text

    days = int(settings.get("digest", {}).get("filings_days", 7))
    done, text_failed = filing_text.collect_filing_text(
        con,
        client,
        [s.symbol for s in config.load_watchlist()],
        dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=days),
    )
    log.info("Fetched text for %d recent 8-K filing(s)", done)
    if text_failed:
        log.warning("Filing text failed for %d filing(s); retried next run", len(text_failed))
    return not failed


def _collect_news(con) -> bool:
    from market_intel.collectors import news

    results = news.collect_news(con, [s.symbol for s in config.load_watchlist()])
    failed = [s for s, n in results.items() if n < 0]
    log.info("Stored %d new headline(s)", sum(n for n in results.values() if n > 0))
    if failed:
        log.error("News collection failed for: %s", ", ".join(failed))
    return not failed


def _export_data(con, d, a, settings: dict) -> bool:
    """Push the day's data file to the digest-data branch for the scheduled Claude run."""
    from market_intel.delivery import export

    exp = settings.get("export", {})
    files = export.export_files(export.build_export(con, d, a, settings))
    try:
        commit = export.push_to_branch(
            config.REPO_ROOT,
            files,
            branch=exp.get("branch", "digest-data"),
            message=f"Digest data {d.as_of or ''}".strip(),
        )
    except (export.GitPushError, OSError) as exc:
        log.error("Could not push the digest data file: %s", exc)
        return False
    log.info("Digest data pushed to %s (%s)", exp.get("branch", "digest-data"), commit[:8])
    return True


def _score_sentiment(con, settings: dict, required: bool = False) -> bool:
    """FinBERT-score new headlines, rebuild the daily summary and write sentiment signals.
    Without the optional FinBERT install this is skipped (an error only if ``required``)."""
    from market_intel.signals import regime, sentiment

    cfg = sentiment.sentiment_settings(settings)
    if not cfg["enabled"] and not required:
        return True
    pending = con.execute("SELECT count(*) FROM news WHERE sentiment_score IS NULL").fetchone()[0]
    if pending:
        if not sentiment.finbert_available():
            msg = "FinBERT isn't installed (pip install -e .[sentiment]); %d headlines unscored"
            (log.error if required else log.info)(msg, pending)
            if required:
                return False
        else:
            scorer = sentiment.FinBertScorer(cfg["model"], int(cfg["batch_size"]))
            sentiment.score_unscored(con, scorer)
    sentiment.refresh_daily(con)
    bench = settings.get("benchmark", {})
    reg = regime.current_regime(
        con, bench.get("symbol", "SPY"), int(bench.get("regime_ma_days", 200))
    )
    symbols = [s.symbol for s in config.load_watchlist()]
    results = sentiment.detect(con, symbols, settings, regime=reg)
    for r in results:
        log.info("Sentiment %-6s %s", r.symbol, sentiment.describe(r, cfg))
        for sig in r.signals:
            log.info("  SIGNAL %s", sentiment.flag_text(sig))
    return True


def _build(con, args: argparse.Namespace):
    from market_intel.delivery import analysis, digest

    config.load_env()
    settings = config.load_settings()
    d = digest.build_digest(con, config.load_watchlist(), settings)
    if getattr(args, "no_llm", False):
        a, reason = None, "--no-llm was given"
    else:
        from market_intel.llm import briefs

        a, reason = briefs.generate(con, d, settings)
    if a is None:
        from market_intel.collectors.filing_text import filing_text_lookup

        log.info("Using rule-based notes: %s", reason)
        a = analysis.rule_based(d, settings, note=reason, filing_text=filing_text_lookup(con))
    return d, a, settings


def _deliver_digest(con, args: argparse.Namespace, export: bool = False) -> bool:
    """Print the digest (--dry-run) or send it to Telegram. If sending fails, the digest is
    printed to the console instead so the run's output isn't lost, and False is returned.
    With ``export`` (run-daily, not dry run), the day's data file is also pushed."""
    from market_intel.delivery import render

    d, a, settings = _build(con, args)
    exported = True
    if export and not args.dry_run and settings.get("export", {}).get("push", False):
        exported = _export_data(con, d, a, settings)
    text = render.render_text(d, a)
    if args.dry_run:
        print(text)
        return True

    from market_intel.delivery import telegram

    try:
        client = telegram.TelegramClient(telegram.TelegramConfig.from_env())
        n = client.send_html(render.render_html(d, a))
    except telegram.TelegramError as exc:
        log.error("%s", exc)
        log.error("Digest not sent; printing it here instead.")
        print(text)
        return False
    log.info("Digest sent to Telegram (%d message%s)", n, "" if n == 1 else "s")
    return exported


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


def cmd_score_sentiment(args: argparse.Namespace) -> int:
    con = _open_db(args)
    ok = _score_sentiment(con, config.load_settings(), required=True)
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
    ok_news = _collect_news(con)
    ok_sentiment = _score_sentiment(con, settings)
    ok_sent = _deliver_digest(con, args, export=True)
    con.close()
    return 0 if ok_prices and ok_filings and ok_news and ok_sentiment and ok_sent else 1


def cmd_collect_news(args: argparse.Namespace) -> int:
    con = _open_db(args)
    ok = _collect_news(con)
    con.close()
    return 0 if ok else 1


def cmd_export_data(args: argparse.Namespace) -> int:
    """Build the digest from stored data and push the day's data file (no Telegram)."""
    con = _open_db(args)
    d, a, settings = _build(con, args)
    ok = _export_data(con, d, a, settings)
    con.close()
    return 0 if ok else 1


# ------------------------------------------------------------------ weekly screener
PRICE_LOOKBACK_DAYS = 400  # enough for the 200-day average in the market overview


def _open_screen_db(args: argparse.Namespace):
    from market_intel import screener

    con = _open_db(args)
    screener.init_schema(con)
    return con


def _sec_client(settings: dict):
    from market_intel.collectors import edgar

    config.load_env()
    sec = settings.get("sec", {})
    return edgar.SecClient(max_requests_per_second=float(sec.get("max_requests_per_second", 10)))


def _collect_screen_data(con, settings: dict, zip_path: str | None, download: bool) -> bool:
    """Universe + SEC financials. Default universe is the S&P 500 (financials fetched per
    company); ``screener.universe: all_listed`` uses every listed stock and the bulk zip."""
    from pathlib import Path

    from market_intel.collectors import edgar
    from market_intel.screener import facts, universe

    try:
        client = _sec_client(settings)
    except edgar.MissingUserAgentError as exc:
        log.error("%s", exc)
        return False
    scfg = settings.get("screener", {}) or {}
    if scfg.get("universe", "sp500") == "sp500":
        try:
            n = universe.refresh_sp500(con)
        except Exception as exc:  # noqa: BLE001 - fall back to last week's list
            n = con.execute("SELECT count(*) FROM screen_universe").fetchall()[0][0]
            log.error("S&P 500 list refresh failed (%s); using the stored list of %d", exc, n)
            if not n:
                return False
        else:
            log.info("Universe: %d S&P 500 companies", n)
        ciks = [r[0] for r in con.execute("SELECT cik FROM screen_universe").fetchall()]
        rows, failed = facts.fetch_companies(con, client, ciks)
        log.info("Financials: %d facts loaded; %d companies failed", rows, len(failed))
        return len(failed) < len(ciks)

    n = universe.refresh(con, client, scfg.get("exchanges") or universe.DEFAULT_EXCHANGES)
    log.info("Universe: %d listed companies", n)

    path = Path(zip_path) if zip_path else config.REPO_ROOT / "data" / "companyfacts.zip"
    if download or not path.exists():
        log.info("Downloading SEC companyfacts.zip (over 1 GB, takes a while) to %s", path)
        facts.download_bulk_zip(client, path)
    ciks = {r[0] for r in con.execute("SELECT cik FROM screen_universe").fetchall()}
    companies, rows = facts.ingest_bulk_zip(con, path, ciks)
    log.info("Financials: %d companies, %d facts loaded", companies, rows)

    with_facts = {r[0] for r in con.execute("SELECT DISTINCT cik FROM xbrl_facts").fetchall()}
    todo = universe.missing_sic(con, with_facts)
    if todo:
        log.info("Looking up industry codes for %d companies (first run is slow)", len(todo))
        universe.fill_sic(con, client, todo)
    return True


def _collect_screen_prices(con, settings: dict, start) -> bool:
    import datetime as dt

    from market_intel.screener import backtest, market, prices

    tickers = [r[0] for r in con.execute("SELECT ticker FROM screen_universe").fetchall()]
    tickers += market.market_symbols(settings)
    bench = backtest.bt_settings(settings).get("benchmark")
    if bench:
        tickers.append(bench)
    today = dt.datetime.now(dt.UTC).date()
    rows, missing = prices.collect(con, tickers, start, today + dt.timedelta(days=1))
    log.info("Prices: %d rows; %d tickers returned nothing", rows, len(missing))
    return rows > 0


def _screen_and_deliver(con, args: argparse.Namespace, settings: dict) -> bool:
    import datetime as dt

    from market_intel.screener import market, screen

    as_of = (
        dt.date.fromisoformat(args.date)
        if getattr(args, "date", None)
        else (dt.datetime.now(dt.UTC).date())
    )
    cfg = screen.screen_settings(settings)
    top_n = int(cfg["top_n"])
    result = screen.run_screen(con, as_of, settings)
    previous = screen.previous_tickers(con, as_of)
    screen.save_results(con, result, top_n)
    watch = {s.symbol for s in config.load_watchlist()}
    text = (
        market.render_overview(*market.overview(con, as_of, settings))
        + "\n\n"
        + screen.render_section(result, top_n, previous, watch)
    )
    log.info("Screen: %d passed; excluded %s", len(result.candidates), result.excluded)
    if args.dry_run:
        print(text)
        return True
    from market_intel.delivery import telegram

    config.load_env()
    try:
        telegram.TelegramClient(telegram.TelegramConfig.from_env()).send_text(text)
    except telegram.TelegramError as exc:
        log.error("%s", exc)
        log.error("Screen not sent; printing it here instead.")
        print(text)
        return False
    log.info("Screen sent to Telegram")
    return True


def cmd_screen_collect(args: argparse.Namespace) -> int:
    con = _open_screen_db(args)
    ok = _collect_screen_data(con, config.load_settings(), args.zip, args.download)
    con.close()
    return 0 if ok else 1


def cmd_screen_prices(args: argparse.Namespace) -> int:
    import datetime as dt

    con = _open_screen_db(args)
    start = (
        dt.date.fromisoformat(args.start)
        if args.start
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=PRICE_LOOKBACK_DAYS)
    )
    ok = _collect_screen_prices(con, config.load_settings(), start)
    con.close()
    return 0 if ok else 1


def cmd_screen(args: argparse.Namespace) -> int:
    con = _open_screen_db(args)
    ok = _screen_and_deliver(con, args, config.load_settings())
    con.close()
    return 0 if ok else 1


def cmd_run_weekly(args: argparse.Namespace) -> int:
    """Refresh universe, financials and latest prices, then send (or print) the screen."""
    import datetime as dt

    settings = config.load_settings()
    con = _open_screen_db(args)
    ok_data = _collect_screen_data(con, settings, None, download=True)
    ok_px = _collect_screen_prices(
        con, settings, dt.datetime.now(dt.UTC).date() - dt.timedelta(days=PRICE_LOOKBACK_DAYS)
    )
    ok_sent = _screen_and_deliver(con, args, settings)
    con.close()
    return 0 if ok_data and ok_px and ok_sent else 1


def cmd_screen_backtest(args: argparse.Namespace) -> int:
    import datetime as dt
    from pathlib import Path

    from market_intel.screener import backtest, screen

    settings = config.load_settings()
    con = _open_screen_db(args)
    top_n = args.top_n or int(screen.screen_settings(settings)["top_n"])
    try:
        periods, summary = backtest.run_backtest(
            con,
            settings,
            start=dt.date.fromisoformat(args.start) if args.start else None,
            end=dt.date.fromisoformat(args.end) if args.end else None,
            top_n=top_n,
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 1
    finally:
        con.close()
    report = backtest.render_report(periods, summary, top_n, settings)
    out = (
        Path(args.out)
        if args.out
        else (config.REPO_ROOT / "data" / "backtests" / f"screen_backtest_{dt.date.today()}.md")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    log.info("Report written to %s", out)
    return 0


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

    p = sub.add_parser("collect-news", help="Fetch Yahoo Finance headlines for the watchlist")
    p.add_argument("--db", help="Override the database path")
    p.set_defaults(func=cmd_collect_news)

    p = sub.add_parser(
        "export-data", help="Push today's digest data file to the digest-data branch"
    )
    p.add_argument("--db", help="Override the database path")
    p.set_defaults(func=cmd_export_data)

    p = sub.add_parser(
        "score-sentiment",
        help="FinBERT-score new headlines and detect sentiment shifts / news-volume spikes",
    )
    p.add_argument("--db", help="Override the database path")
    p.set_defaults(func=cmd_score_sentiment)

    for name, func, help_ in (
        ("digest", cmd_digest, "Send the digest built from stored data to Telegram"),
        ("run-daily", cmd_run_daily, "Collect prices, filings, news; send digest; push data"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", help="Override the database path")
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the digest to the console instead of sending it",
        )
        p.add_argument(
            "--no-llm",
            action="store_true",
            help="Skip Claude and use the rule-based notes (no API cost)",
        )
        p.set_defaults(func=func)

    p = sub.add_parser("telegram-chat-id", help="Show chat ids that messaged your bot")
    p.set_defaults(func=cmd_telegram_chat_id)

    p = sub.add_parser("telegram-test", help="Send a test message to TELEGRAM_CHAT_ID")
    p.set_defaults(func=cmd_telegram_test)

    p = sub.add_parser("screen-collect", help="Load the S&P 500 list and its SEC financials")
    p.add_argument("--db", help="Override the database path")
    p.add_argument("--zip", help="all_listed universe only: use this companyfacts.zip")
    p.add_argument("--download", action="store_true", help="all_listed only: fresh zip")
    p.set_defaults(func=cmd_screen_collect)

    p = sub.add_parser("screen-prices", help="Fetch prices for the whole screener universe")
    p.add_argument("--db", help="Override the database path")
    p.add_argument("--start", help="First date (YYYY-MM-DD); default 400 days ago")
    p.set_defaults(func=cmd_screen_prices)

    for name, func, help_ in (
        ("screen", cmd_screen, "Run the screen on stored data and send the candidates"),
        ("run-weekly", cmd_run_weekly, "Refresh screener data, then send the candidates"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", help="Override the database path")
        p.add_argument("--dry-run", action="store_true", help="Print instead of sending")
        if name == "screen":
            p.add_argument("--date", help="Screen as of this date (YYYY-MM-DD)")
        p.set_defaults(func=func)

    p = sub.add_parser("screen-backtest", help="Replay the screen on past dates (point-in-time)")
    p.add_argument("--db", help="Override the database path")
    p.add_argument("--start", help="First rebalance date (default from settings)")
    p.add_argument("--end", help="Last rebalance date (default: latest price)")
    p.add_argument("--top-n", type=int, help="Picks per date (default from settings)")
    p.add_argument("--out", help="Where to write the Markdown report")
    p.set_defaults(func=cmd_screen_backtest)
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
