# SystemTradingS3

SystemTradingS3 is a simulated trading systems skeleton. It is not an alpha-discovery bot and must not promise profit.

The current baseline has four CLI layers:

1. `audit`: read-only structural audit for generated or provided simulation datasets.
2. `simulate`: config-driven paper-trading loop using simulated market prices.
3. `validate_run`: replay validation for exported simulation run artifacts.
4. `metrics`: deterministic post-run baseline and benchmark-relative metrics from exported artifacts.

The long-term thesis is that strategies are presets running on common base rules. The system should evaluate outcomes relative to target market or factor exposure, not only by absolute profit. The current code intentionally stops before risk rules, factor attribution, loss classification, richer strategy modeling, or live integration.

## Quickstart

```powershell
rtk python -m unittest discover -s tests

# MVP0: audit an output-shaped dataset
rtk python -m system_trading_s3.audit tests/fixtures/valid_minimal
rtk python -m system_trading_s3.audit --strict tests/fixtures/valid_minimal

# MVP1: run the thin paper-trading loop without writing artifacts
rtk python -m system_trading_s3.simulate tests/fixtures/valid_complete

# MVP7-MVP9: run a multi-symbol periodic factor rebalance fixture
rtk python -m system_trading_s3.simulate tests/fixtures/valid_multisymbol --config tests/fixtures/sample_config.json

# MVP2: export deterministic run artifacts
$run = "$env:TEMP\systemtraders3-docs-smoke"
rtk python -m system_trading_s3.simulate tests/fixtures/valid_multisymbol --config tests/fixtures/sample_config.json --export-dir $run --run-id docs-smoke --overwrite

# MVP2 self-check and MVP3 replay validation
rtk python -m system_trading_s3.audit $run
rtk python -m system_trading_s3.validate_run $run

# MVP5/MVP6: write deterministic baseline and benchmark-relative metrics
rtk python -m system_trading_s3.metrics $run
```

## Interactive Dashboard

The repo includes an experimental local dashboard for visualizing simulation runs and launching new local backtests. The Python server uses stdlib only; the browser UI currently loads Chart.js, Lucide, and fonts from public CDNs.

```powershell
# Start the local dashboard server (from the project root directory)
rtk python dashboard/server.py
```
Once running, open your browser and navigate to **http://localhost:8000** to view the interactive UI.

All commands are local and simulated. `audit` and `validate_run` are read-only. `simulate --export-dir` writes deterministic artifacts only to the requested output directory.

## Current Status

- MVP0 completed: dataset audit for `equity_curve.csv` and `trades.csv`.
- MVP1 completed: one-symbol simulated paper-trading loop with a trivial buy/hold/sell strategy.
- MVP2 completed: deterministic run artifact export.
- MVP3 completed: run artifact replay and baseline accounting validation.
- MVP4 completed: config-driven strategy selection from a static registry.
- MVP5 completed: deterministic transaction friction and post-run baseline metrics.
- MVP6 completed: engine-integrated benchmark logging and benchmark-relative metrics.
- MVP7 completed: multi-symbol market ingestion and portfolio state accounting.
- MVP8 completed: target-weight strategy intents and integer-share portfolio rebalancing.
- MVP9 completed: optional factor data ingestion and periodic factor-weight rebalancing.

## MVP Responsibilities

MVP0 audit:

- validates required generated/provided files: `equity_curve.csv`, `trades.csv`;
- treats missing `benchmark.csv` and `factor_exposure.csv` as optional readiness gaps;
- reports `PASS`, `INCONCLUSIVE`, or `FAIL`.

MVP1 simulate:

- reads `market_prices.csv`, or sorted `*_prices.csv` files, as the true simulation input;
- forward-fills prices onto synchronized market timestamps and passes strategies a `{symbol: price}` market state;
- preserves the default legacy `buy_and_hold_one_unit` behavior when no config is provided;
- uses `decimal.Decimal` accounting;
- prints final account state without exporting files unless requested.

MVP4 config-driven execution:

- accepts `--config <json>`;
- reads `initial_cash`, `strategy_name`, and `strategy_params`;
- optionally reads `friction.fee_rate` and `friction.slippage_per_trade`;
- optionally reads `risk_free_rate` for Sharpe ratio calculations;
- supports a static registry with `BuyAndHold`, `MovingAverageCross`, `EqualWeightRebalance`, and `PeriodicFactorWeight`;
- allows strategies to emit explicit orders or target weights;
- keeps execution sizing, fill routing, friction accounting, and account mutation inside the engine.

MVP8 rebalancing:

- converts target weights into deterministic integer-share market orders;
- sells alphabetically before buying alphabetically;
- accounts for configured fee and slippage when checking affordability;
- never intentionally generates orders that would make simulated cash negative.

MVP9 factor rebalancing:

- optionally reads `factors.csv` using `timestamp,symbol,factor_name,factor_value`;
- forward-fills factor values onto synchronized market timestamps;
- passes strategies `factor_data` as `{symbol: {factor_name: value}}`;
- adds `PeriodicFactorWeight`, which rebalances every configured number of ticks into the top-K symbols by factor value;
- keeps the `PortfolioRebalancer` factor-agnostic.

MVP2 export:

- writes deterministic artifacts: `run_manifest.json`, `equity_curve.csv`, `trades.csv`, `orders.csv`, `fills.csv`, `account_summary.json`, and `audit_summary.json`;
- does not fabricate benchmark or factor files;
- records optional audit gaps as gaps, not failures.
- records optional `benchmark_price` and `benchmark_equity` columns in `equity_curve.csv` when `benchmark_prices.csv` is available.
- records portfolio `last_prices` and `position_quantities` in `equity_curve.csv` for multi-symbol replay validation.

MVP3 validate_run:

- reads an exported run without rerunning simulation;
- validates required artifacts, manifest/account consistency, order/fill consistency, friction-aware accounting replay, final equity, current trades/fills mapping, and audit summary status.

MVP5/MVP6 metrics:

- reads exported `equity_curve.csv` and `trades.csv`;
- writes deterministic `metrics.json`;
- calculates total return, CAGR using equity row intervals with 252 trading days/year, max drawdown, realized-PnL win rate, profit factor, and trade counts;
- calculates alpha, beta, Sharpe ratio, tracking error, and information ratio when benchmark equity is available;
- reports `UNAVAILABLE` and gaps rather than inferring missing realized PnL.

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

MVP1+ simulation accepts either normalized `market_prices.csv` or multiple sorted `*_prices.csv` files, each using `timestamp,symbol,price`. MVP6+ also accepts optional `benchmark_prices.csv`; the simulator forward-fills benchmark prices onto market timestamps and normalizes benchmark equity from the same initial cash. MVP9 also accepts optional `factors.csv` using `timestamp,symbol,factor_name,factor_value`; missing factors do not block non-factor strategies.

CSV is parsed as `utf-8-sig` so UTF-8 BOM headers from spreadsheet exports are accepted. Header whitespace is trimmed, header names remain case-sensitive, unknown extra columns are allowed, and blank required cells are treated as missing rather than zero.

## Non-Goals

- No live trading.
- No broker or exchange API integration.
- No real order placement or automation.
- No leverage automation.
- No optimization.
- No opaque ML prediction.
- No production, hosted, or broker-connected dashboard.
- No factor attribution or factor-relative loss classification yet.
- No richer risk-adjusted metrics such as Sortino, capture, or turnover yet.
- No per-symbol metrics yet; metrics evaluate portfolio-level equity only.
- No claim that any strategy is profitable.

See `schemas/` for the exact contracts and `docs/mvp-foundation.md` for the staged roadmap and limitations.
