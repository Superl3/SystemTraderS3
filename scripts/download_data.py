"""Download Yahoo Finance prices into simulator-ready CSV fixtures.

This script is intentionally outside the stdlib-only core. It requires the
optional packages `yfinance` and `pandas` only when an actual download is run.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download Yahoo Finance prices, benchmark prices, and a simple momentum factor dataset."
    )
    parser.add_argument(
        "--symbols",
        default="005930.KS,000660.KS,035420.KS",
        help="Comma-separated symbols, for example AAPL,MSFT,GOOG or 005930.KS,000660.KS.",
    )
    parser.add_argument(
        "--benchmark",
        default="^KS11",
        help="Benchmark ticker, for example ^GSPC or ^KS11. Use an empty value to skip.",
    )
    parser.add_argument("--start", default="2020-01-01", help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--end", default="2025-12-31", help="End date in YYYY-MM-DD format.")
    parser.add_argument(
        "--output-dir",
        default="tests/fixtures/historical_data",
        help="Directory where simulator-ready CSV files will be written.",
    )
    parser.add_argument(
        "--offline-smoke",
        action="store_true",
        help="Write a tiny deterministic simulator-ready dataset without network or optional dependencies.",
    )
    args = parser.parse_args(argv)

    output_path = Path(args.output_dir)
    if args.offline_smoke:
        _write_offline_smoke_dataset(output_path)
        print(f"wrote offline smoke dataset: {output_path.resolve()}")
        return 0

    try:
        import pandas as pd
        import yfinance as yf
    except ImportError as exc:
        missing_name = exc.name or "required package"
        print(f"Missing optional dependency: {missing_name}", file=sys.stderr)
        print("Install optional downloader dependencies with:", file=sys.stderr)
        print("  pip install yfinance pandas", file=sys.stderr)
        return 2

    symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
    benchmark = args.benchmark.strip()
    output_path.mkdir(parents=True, exist_ok=True)

    print("SystemTradingS3 Yahoo Finance downloader")
    print(f"symbols: {', '.join(symbols)}")
    print(f"benchmark: {benchmark or 'none'}")
    print(f"date range: {args.start} to {args.end}")
    print(f"output directory: {output_path.resolve()}")

    all_prices: list[Any] = []
    for symbol in symbols:
        downloaded = _download_symbol(yf, symbol, args.start, args.end)
        if downloaded is None:
            continue
        downloaded["timestamp"] = _iso_midnight_timestamps(downloaded["Date"])
        downloaded["symbol"] = symbol
        downloaded["price"] = downloaded["Close"].round(2)

        safe_symbol = _safe_file_stem(symbol)
        symbol_file = output_path / f"{safe_symbol}_prices.csv"
        downloaded[["timestamp", "symbol", "price"]].to_csv(symbol_file, index=False, encoding="utf-8")
        print(f"wrote {symbol_file.name}: {len(downloaded)} rows")
        all_prices.append(downloaded)

    if not all_prices:
        print("No symbol price data was downloaded.", file=sys.stderr)
        return 1

    if benchmark:
        benchmark_data = _download_symbol(yf, benchmark, args.start, args.end)
        if benchmark_data is not None:
            benchmark_data["timestamp"] = _iso_midnight_timestamps(benchmark_data["Date"])
            benchmark_data["symbol"] = benchmark
            benchmark_data["price"] = benchmark_data["Close"].round(2)
            benchmark_file = output_path / "benchmark_prices.csv"
            benchmark_data[["timestamp", "symbol", "price"]].to_csv(benchmark_file, index=False, encoding="utf-8")
            print(f"wrote {benchmark_file.name}: {len(benchmark_data)} rows")

    factors = _build_momentum_factors(pd, all_prices)
    if not factors.empty:
        factors_file = output_path / "factors.csv"
        factors.to_csv(factors_file, index=False, encoding="utf-8")
        print(f"wrote {factors_file.name}: {len(factors)} rows")
    else:
        print("No factor rows were generated; downloaded history may be too short.")

    return 0


def _download_symbol(yf: Any, symbol: str, start: str, end: str) -> Any | None:
    print(f"downloading {symbol}...")
    try:
        frame = yf.Ticker(symbol).history(start=start, end=end)
    except Exception as exc:
        print(f"warning: failed to download {symbol}: {exc}", file=sys.stderr)
        return None
    if frame.empty:
        print(f"warning: no rows for {symbol}", file=sys.stderr)
        return None
    return frame.reset_index()


def _iso_midnight_timestamps(date_series: Any) -> Any:
    # The simulator requires ISO datetimes, not date-only strings.
    return date_series.dt.strftime("%Y-%m-%dT00:00:00")


def _build_momentum_factors(pd: Any, price_frames: list[Any]) -> Any:
    factor_rows: list[dict[str, object]] = []
    for frame in price_frames:
        symbol = frame["symbol"].iloc[0]
        working = frame.copy()
        working["momentum"] = working["Close"].pct_change(periods=20).round(6)
        for _, row in working.dropna(subset=["momentum"]).iterrows():
            factor_rows.append(
                {
                    "timestamp": row["timestamp"],
                    "symbol": symbol,
                    "factor_name": "momentum",
                    "factor_value": row["momentum"],
                }
            )
    if not factor_rows:
        return pd.DataFrame(columns=["timestamp", "symbol", "factor_name", "factor_value"])
    return pd.DataFrame(factor_rows).sort_values(by=["timestamp", "symbol", "factor_name"])


def _safe_file_stem(symbol: str) -> str:
    return "".join(char for char in symbol if char.isalnum() or char in "-_") or "symbol"


def _write_offline_smoke_dataset(output_path: Path) -> None:
    output_path.mkdir(parents=True, exist_ok=True)
    timestamps = ["2024-01-02T00:00:00", "2024-01-03T00:00:00", "2024-01-04T00:00:00"]
    symbols = ["SMOKE_A", "SMOKE_B"]

    with (output_path / "market_prices.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["timestamp", "symbol", "price"])
        for index, timestamp in enumerate(timestamps):
            writer.writerow([timestamp, "SMOKE_A", f"{100 + index * 2:.2f}"])
            writer.writerow([timestamp, "SMOKE_B", f"{50 + index:.2f}"])

    with (output_path / "benchmark_prices.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["timestamp", "symbol", "price"])
        for index, timestamp in enumerate(timestamps):
            writer.writerow([timestamp, "SMOKE_BENCH", f"{100 + index:.2f}"])

    with (output_path / "factors.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["timestamp", "symbol", "factor_name", "factor_value"])
        for index, timestamp in enumerate(timestamps):
            writer.writerow([timestamp, "SMOKE_A", "momentum", f"{0.10 + index * 0.01:.4f}"])
            writer.writerow([timestamp, "SMOKE_B", "momentum", f"{0.05 + index * 0.01:.4f}"])

    manifest = {
        "data_policy": "offline deterministic downloader contract smoke; not real market data",
        "dataset_id": "offline_smoke",
        "files": ["market_prices.csv", "benchmark_prices.csv", "factors.csv"],
        "schema_version": "download_data.offline_smoke.v1",
        "symbol_count": len(symbols),
        "symbols": symbols,
        "timestamp_start": timestamps[0],
        "timestamp_end": timestamps[-1],
    }
    (output_path / "dataset_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    raise SystemExit(main())
