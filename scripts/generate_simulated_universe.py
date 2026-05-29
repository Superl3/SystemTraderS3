"""Generate a deterministic historical-style multi-symbol simulator dataset.

The generated prices are synthetic and intended for simulator integration tests
and dashboard demos. They are not market data and must not be used as evidence
that any strategy is profitable.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("datasets/us_tech_100_simulated")
SYMBOLS = [f"USTECH{i:03d}" for i in range(1, 101)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a deterministic simulated US tech 100 dataset.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start", default="2024-01-02", help="First date in YYYY-MM-DD format.")
    parser.add_argument("--days", type=int, default=30, help="Number of business-day rows to generate.")
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    timestamps = [_iso_datetime(day) for day in _business_days(start, args.days)]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    _write_market_prices(args.output_dir / "market_prices.csv", timestamps)
    _write_benchmark_prices(args.output_dir / "benchmark_prices.csv", timestamps)
    _write_factors(args.output_dir / "factors.csv", timestamps)
    _write_manifest(args.output_dir / "dataset_manifest.json", timestamps)
    return 0


def _business_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _iso_datetime(day: date) -> str:
    return f"{day.isoformat()}T16:00:00"


def _write_market_prices(path: Path, timestamps: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["timestamp", "symbol", "price"])
        for index, timestamp in enumerate(timestamps):
            for symbol_index, symbol in enumerate(SYMBOLS, start=1):
                writer.writerow([timestamp, symbol, _price(symbol_index, index)])


def _write_benchmark_prices(path: Path, timestamps: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["timestamp", "symbol", "price"])
        for index, timestamp in enumerate(timestamps):
            writer.writerow([timestamp, "USTECH100_BENCH", _benchmark_price(index)])


def _write_factors(path: Path, timestamps: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["timestamp", "symbol", "factor_name", "factor_value"])
        for index, timestamp in enumerate(timestamps):
            for symbol_index, symbol in enumerate(SYMBOLS, start=1):
                writer.writerow([timestamp, symbol, "momentum", _momentum(symbol_index, index)])


def _write_manifest(path: Path, timestamps: list[str]) -> None:
    payload = {
        "dataset_id": "us_tech_100_simulated",
        "schema_version": "simulated_historical_universe.v1",
        "universe": "US technology 100 synthetic symbols",
        "symbol_count": len(SYMBOLS),
        "symbols": SYMBOLS,
        "timestamp_start": timestamps[0],
        "timestamp_end": timestamps[-1],
        "files": ["market_prices.csv", "benchmark_prices.csv", "factors.csv"],
        "data_policy": "synthetic deterministic historical-style data; not real market data",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _price(symbol_index: int, tick_index: int) -> str:
    base = Decimal("25") + Decimal(symbol_index) * Decimal("1.35")
    trend = Decimal(tick_index) * (Decimal("0.03") + Decimal(symbol_index % 7) * Decimal("0.01"))
    cycle = Decimal(((tick_index + symbol_index) % 9) - 4) * Decimal("0.07")
    return _money(base + trend + cycle)


def _benchmark_price(tick_index: int) -> str:
    base = Decimal("100")
    trend = Decimal(tick_index) * Decimal("0.11")
    cycle = Decimal((tick_index % 5) - 2) * Decimal("0.05")
    return _money(base + trend + cycle)


def _momentum(symbol_index: int, tick_index: int) -> str:
    value = Decimal(symbol_index % 17 - 8) * Decimal("0.0125") + Decimal(tick_index % 6) * Decimal("0.01")
    return str(value.quantize(Decimal("0.0001")))


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


if __name__ == "__main__":
    raise SystemExit(main())
