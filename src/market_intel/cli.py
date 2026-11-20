"""Command-line entry point: `market-intel <command>`."""

from __future__ import annotations

import argparse
import logging

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


def _print_digest(con, args: argparse.Namespace) -> None:
    from market_intel.delivery import digest

    if not args.dry_run:
        raise SystemExit("Sending is not implemented yet; use --dry-run to print the digest.")
    d = digest.build_digest(con, config.load_watchlist(), config.load_settings())
    print(digest.render_text(d))


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
    _print_digest(con, args)
    con.close()
    return 0


def cmd_run_daily(args: argparse.Namespace) -> int:
    """Collect everything, then print the digest. A failed collector doesn't stop the
    digest (it runs on whatever is stored), but the exit code reports the failure."""
    settings = config.load_settings()
    con = _open_db(args)
    ok_prices = _collect_prices(con, settings)
    ok_filings = _collect_filings(con, settings)
    _print_digest(con, args)
    con.close()
    return 0 if ok_prices and ok_filings else 1


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
        ("digest", cmd_digest, "Build the digest from stored data"),
        ("run-daily", cmd_run_daily, "Collect prices and filings, then build the digest"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", help="Override the database path")
        p.add_argument("--dry-run", action="store_true",
                       help="Print the digest to the console instead of sending it")
        p.set_defaults(func=func)
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
