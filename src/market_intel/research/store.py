"""Store each research run: specialists' and CIO reports (company research memory) and
every stance given (for the scorecard). Re-running a day replaces that day only."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import duckdb


def save_run(
    con: duckdb.DuckDBPyConnection,
    manifest: dict[str, Any],
    final: dict[str, Any] | None,
    validation: dict[str, Any] | None,
    status: str,
    notes: str = "",
    benchmark: str = "SPY",
) -> None:
    day = dt.date.fromisoformat(manifest["run_date"])
    con.execute("DELETE FROM research_runs WHERE run_date = ?", [day])
    con.execute("DELETE FROM agent_reports WHERE run_date = ?", [day])
    con.execute("DELETE FROM stances WHERE run_date = ?", [day])
    con.execute(
        "INSERT INTO research_runs (run_date, tickers, status, notes, validation) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            day,
            json.dumps(manifest["tickers"]),
            status,
            notes,
            json.dumps((validation or {}).get("summary")),
        ],
    )
    if not final:
        return
    issues = (validation or {}).get("issues", [])
    for key, report in (final.get("specialists") or {}).items():
        agent, ticker = key.split("/", 1)
        mine = [i for i in issues if i["where"].startswith(key)]
        con.execute(
            "INSERT INTO agent_reports (run_date, agent, ticker, report, issues) "
            "VALUES (?, ?, ?, ?, ?)",
            [day, agent, ticker, json.dumps(report), json.dumps(mine)],
        )
    for item in final.get("items", []):
        ticker = item["ticker"]
        mine = [i for i in issues if i["where"].startswith(f"final.{ticker}")]
        report = {k: v for k, v in item.items() if k != "_sources"}
        con.execute(
            "INSERT INTO agent_reports (run_date, agent, ticker, report, issues) "
            "VALUES (?, 'cio', ?, ?, ?)",
            [day, ticker, json.dumps(report), json.dumps(mine)],
        )
        if item.get("stance"):
            closes = []
            for sym in (ticker, benchmark):
                row = con.execute(
                    "SELECT close FROM prices_daily WHERE symbol = ? AND trade_date <= ? "
                    "ORDER BY trade_date DESC LIMIT 1",
                    [sym, dt.date.fromisoformat(manifest["data_as_of"])],
                ).fetchone()
                closes.append(row[0] if row else None)
            con.execute(
                "INSERT INTO stances (run_date, ticker, held, stance, confidence, "
                "close_at_stance, benchmark_close) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    day,
                    ticker,
                    bool(item.get("held")),
                    item["stance"],
                    item.get("confidence"),
                    closes[0],
                    closes[1],
                ],
            )
