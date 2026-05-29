# SystemTradingS3

SystemTradingS3 is a simulated trading systems skeleton. It is not an alpha-discovery bot and must not promise profit.

The current baseline has three CLI layers:

1. `audit`: read-only structural audit for generated or provided simulation datasets.
2. `simulate`: thin paper-trading loop using simulated market prices.
3. `validate_run`: replay validation for exported simulation run artifacts.

The long-term thesis is that strategies are presets running on common base rules. A future system should evaluate outcomes relative to target market or factor exposure, not only by absolute profit. The current code intentionally stops before richer metrics, strategy selection, risk rules, or live integration.

## Quickstart

```powershell
rtk python -m unittest discover -s tests

# MVP0: audit an output-shaped dataset
rtk python -m system_trading_s3.audit tests/fixtures/valid_minimal
rtk python -m system_trading_s3.audit --strict tests/fixtures/valid_minimal

# MVP1: run the thin paper-trading loop without writing artifacts
rtk python -m system_trading_s3.simulate tests/fixtures/valid_complete

# MVP2: export deterministic run artifacts
$run = "$env:TEMP\systemtraders3-docs-smoke"
rtk python -m system_trading_s3.simulate tests/fixtures/valid_complete --export-dir $run --run-id docs-smoke --overwrite

# MVP2 self-check and MVP3 replay validation
rtk python -m system_trading_s3.audit $run
rtk python -m system_trading_s3.validate_run $run
```

All commands are local and simulated. `audit` and `validate_run` are read-only. `simulate --export-dir` writes deterministic artifacts only to the requested output directory.

## Current Status

- MVP0 completed: dataset audit for `equity_curve.csv` and `trades.csv`.
- MVP1 completed: one-symbol simulated paper-trading loop with a trivial buy/hold/sell strategy.
- MVP2 completed: deterministic run artifact export.
- MVP3 completed: run artifact replay and baseline accounting validation.
- MVP4 is not implemented.

## MVP Responsibilities

MVP0 audit:

- validates required generated/provided files: `equity_curve.csv`, `trades.csv`;
- treats missing `benchmark.csv` and `factor_exposure.csv` as optional readiness gaps;
- reports `PASS`, `INCONCLUSIVE`, or `FAIL`.

MVP1 simulate:

- reads `market_prices.csv` as the true simulation input;
- runs one built-in strategy, `buy_and_hold_one_unit`;
- uses `decimal.Decimal` accounting;
- prints final account state without exporting files unless requested.

MVP2 export:

- writes deterministic artifacts: `run_manifest.json`, `equity_curve.csv`, `trades.csv`, `orders.csv`, `fills.csv`, `account_summary.json`, and `audit_summary.json`;
- does not fabricate benchmark or factor files;
- records optional audit gaps as gaps, not failures.

MVP3 validate_run:

- reads an exported run without rerunning simulation;
- validates required artifacts, manifest/account consistency, order/fill consistency, accounting replay, final equity, current trades/fills mapping, and audit summary status.

## Audit Status

- `PASS`: required files and required fields are valid, and no optional readiness gaps were found.
- `INCONCLUSIVE`: required data is valid, but optional benchmark, factor, or cost inputs are missing, so future analyses are unavailable.
- `FAIL`: required files, headers, CSV parseability, timestamp order, duplicate keys, required date/numeric fields, enum values, or trade readiness groups are invalid.

Exit codes:

- `0`: `PASS`, or `INCONCLUSIVE` without `--strict`.
- `1`: `FAIL`, or `INCONCLUSIVE` with `--strict`.
- `2`: usage errors or unexpected internal errors.

## MVP0 Inputs

Required:

- `equity_curve.csv`
- `trades.csv`

Optional:

- `benchmark.csv`
- `factor_exposure.csv`

CSV is parsed as `utf-8-sig` so UTF-8 BOM headers from spreadsheet exports are accepted. Header whitespace is trimmed, header names remain case-sensitive, unknown extra columns are allowed, and blank required cells are treated as missing rather than zero.

## Non-Goals

- No live trading.
- No broker or exchange API integration.
- No real order placement or automation.
- No leverage automation.
- No optimization.
- No opaque ML prediction.
- No dashboard.
- No benchmark-relative metrics yet.
- No performance metrics such as return, drawdown, Sharpe, beta, tracking error, win rate, or turnover yet.
- No claim that any strategy is profitable.

See `schemas/` for the exact contracts and `docs/mvp-foundation.md` for the staged roadmap and limitations.
