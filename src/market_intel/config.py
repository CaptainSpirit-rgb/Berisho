"""Configuration loading: .env for secrets and paths, YAML for watchlist and tunables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "market_intel.duckdb"


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str
    cik: int | None = None
    sector_etf: str | None = None


def load_env() -> None:
    """Load .env from the repo root if present. Existing env vars win."""
    load_dotenv(REPO_ROOT / ".env", override=False)


def db_path() -> Path:
    load_env()
    raw = os.environ.get("MARKET_INTEL_DB_PATH")
    if not raw:
        return DEFAULT_DB_PATH
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def load_watchlist(path: Path | None = None) -> list[Security]:
    path = path or CONFIG_DIR / "watchlist.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    entries = data.get("tickers") or []
    securities = [
        Security(
            symbol=str(e["symbol"]).upper(),
            name=e.get("name", ""),
            cik=int(e["cik"]) if e.get("cik") is not None else None,
            sector_etf=e.get("sector_etf"),
        )
        for e in entries
    ]
    symbols = [s.symbol for s in securities]
    if len(symbols) != len(set(symbols)):
        raise ValueError(f"Duplicate symbols in watchlist: {symbols}")
    return securities


def load_settings(path: Path | None = None) -> dict[str, Any]:
    path = path or CONFIG_DIR / "settings.yaml"
    return yaml.safe_load(path.read_text()) or {}
