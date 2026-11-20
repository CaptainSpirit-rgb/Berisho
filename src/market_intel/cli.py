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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-intel", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="Create the DuckDB schema and load the watchlist")
    p.add_argument("--db", help="Override the database path")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("watchlist", help="Print the configured watchlist")
    p.set_defaults(func=cmd_watchlist)
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
