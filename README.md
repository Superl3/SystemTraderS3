# SystemTradingS3

SystemTradingS3 is a simulated trading systems skeleton. It is not an alpha-discovery bot and must not promise profit.

The current baseline has eight CLI layers:

1. `input_audit`: read-only structural audit for simulator input datasets.
2. `audit`: read-only structural audit for output-shaped run artifacts.
3. `simulate`: config-driven paper-trading loop using simulated market prices and deterministic execution rules.
4. `validate_run`: replay validation for exported simulation run artifacts.
5. `metrics`: deterministic post-run baseline and benchmark-relative metrics from exported artifacts.
6. `factor_report`: deterministic factor exposure report from exported fills and dataset `factors.csv`.
7. `factor_attribution`: deterministic factor-return proxy attribution and PnL reconciliation from exported holdings, fills, prices, and dataset `factors.csv`.
8. `factor_risk_model`: gated deterministic OLS factor risk model from `factor_attribution.json`.
9. `loss_classification`: deterministic benchmark-relative loss triage from exported equity artifacts.

The long-term thesis is that strategies are presets running on common base rules. The system should evaluate outcomes relative to target market or factor exposure, not only by absolute profit. The current code gates statistical multi-factor risk modeling behind explicit data sufficiency checks and intentionally stops before richer strategy modeling or live integration.

## Quickstart

```powershell
rtk python -m unittest discover -s tests

# MVP0: audit an output-shaped dataset
rtk python -m system_trading_s3.audit tests/fixtures/valid_minimal
rtk python -m system_trading_s3.audit --strict tests/fixtures/valid_minimal

# Input audit: audit simulator inputs before running a backtest
rtk python -m system_trading_s3.input_audit datasets/us_tech_100_simulated

# MVP1: run the thin paper-trading loop without writing artifacts
rtk python -m system_trading_s3.simulate tests/fixtures/valid_complete

# MVP7-MVP9: run a multi-symbol periodic factor rebalance fixture
rtk python -m system_trading_s3.simulate tests/fixtures/valid_multisymbol --config tests/fixtures/sample_config.json

# Integrated simulator: run a drop-in simulated US tech 100 historical dataset
rtk python -m system_trading_s3.simulate datasets/us_tech_100_simulated --config configs/strategies/periodic_momentum_top10.json

# MVP2: export deterministic run artifacts
$run = "$env:TEMP\systemtraders3-docs-smoke"
rtk python -m system_trading_s3.simulate tests/fixtures/valid_multisymbol --config tests/fixtures/sample_config.json --export-dir $run --run-id docs-smoke --overwrite

# MVP2 self-check and MVP3 replay validation
rtk python -m system_trading_s3.audit $run
rtk python -m system_trading_s3.validate_run $run

# MVP5/MVP6: write deterministic baseline, turnover, and benchmark-relative metrics
rtk python -m system_trading_s3.metrics $run

# MVP10: write factor-aware exposure report
rtk python -m system_trading_s3.factor_report $run

# MVP12: write factor-return proxy attribution
rtk python -m system_trading_s3.factor_attribution $run

# MVP14: write gated statistical factor risk model
rtk python -m system_trading_s3.factor_risk_model $run

# MVP11: classify benchmark-relative loss periods
rtk python -m system_trading_s3.loss_classification $run

# Downloader contract smoke without network or optional dependencies
rtk python scripts/download_data.py --offline-smoke --output-dir "$env:TEMP\systemtraders3-offline-smoke"
```

## Interactive Dashboard

The repo includes an experimental local dashboard for visualizing simulation runs and launching new local backtests. The Python server uses stdlib only; the browser UI currently loads Chart.js, Lucide, and fonts from public CDNs.

```powershell
# Start the local dashboard server (from the project root directory)
rtk python dashboard/server.py
```
Once running, open your browser and navigate to **http://localhost:8000** to view the interactive UI.

All commands are local and simulated. `audit` and `validate_run` are read-only. `simulate --export-dir` writes deterministic artifacts only to the requested output directory.

For a Korean standalone guide to usage, artifact interpretation, and limitations, open `docs/interactive-usage-guide.html`.

## Current Status

- MVP0 completed: dataset audit for `equity_curve.csv` and `trades.csv`.
- Input audit completed: simulator input audit for `market_prices.csv` or sorted `*_prices.csv`, optional `benchmark_prices.csv`, optional `factors.csv`, and optional `dataset_manifest.json`.
- MVP1 completed: one-symbol simulated paper-trading loop with a trivial buy/hold/sell strategy.
- MVP2 completed: deterministic run artifact export.
- MVP3 completed: run artifact replay and baseline accounting validation.
- MVP4 completed: config-driven strategy preset selection from a source-of-truth preset catalog.
- MVP5 completed: deterministic transaction friction and post-run baseline metrics.
- MVP6 completed: engine-integrated benchmark logging and benchmark-relative metrics.
- MVP7 completed: multi-symbol market ingestion and portfolio state accounting.
- MVP8 completed: target-weight strategy intents and integer-share portfolio rebalancing.
- MVP9 completed: optional factor data ingestion and periodic factor-weight rebalancing.
- Execution hardening completed: deterministic full-fill default plus optional capped partial fills with lifecycle validation.
- MVP11 completed: deterministic benchmark-relative loss classification with factor context when `factor_report.json` and `factor_attribution.json` are available.
- MVP12 completed: deterministic factor-return proxy attribution and PnL reconciliation from holdings, fills, prices, and factor ranks.
- MVP14 completed: gated statistical factor risk model from factor attribution; it reports `INCONCLUSIVE` unless complete observations and at least two factors are sufficient for OLS.

## MVP Responsibilities

MVP0 audit:

- validates required generated/provided files: `equity_curve.csv`, `trades.csv`;
- treats missing `benchmark.csv` and `factor_exposure.csv` as optional readiness gaps;
- reports `PASS`, `INCONCLUSIVE`, or `FAIL`.

Input audit:

- validates required simulator inputs: `market_prices.csv`, or sorted `*_prices.csv` files;
- validates optional `benchmark_prices.csv`, `factors.csv`, and `dataset_manifest.json` when present;
- treats missing optional benchmark, factor, and manifest inputs as gaps instead of failures;
- keeps input readiness separate from output artifact self-checks.

Config and manifest contracts:

- `schemas/simulation_config.schema.json` documents the accepted run config keys and registered strategy names;
- `schemas/dataset_manifest.schema.json` documents recommended dataset provenance fields;
- unknown top-level simulation config keys and unknown friction/risk keys are rejected instead of silently ignored.

MVP1 simulate:

- reads `market_prices.csv`, or sorted `*_prices.csv` files, as the true simulation input;
- forward-fills prices onto synchronized market timestamps and passes strategies a `{symbol: price}` market state;
- uses the `RoundTripBuyAndHold` preset by default when no config is provided, preserving the old buy-one-then-liquidate behavior;
- uses `decimal.Decimal` accounting;
- prints final account state without exporting files unless requested.

MVP4 config-driven execution:

- accepts `--config <json>`;
- reads `initial_cash`, `strategy_name`, and `strategy_params`;
- optionally reads `friction.fee_rate` and `friction.slippage_per_trade`;
- optionally reads `execution.max_fill_quantity` and `execution.partial_fill_policy` for deterministic partial fills, either cancelling remainders or carrying residual orders across later market ticks;
- optionally reads `risk.max_position_weight`, `risk.min_cash_buffer`, `risk.max_order_notional`, `risk.cooldown_periods`, and `risk.max_drawdown_pct`;
- optionally reads `risk_free_rate` for Sharpe ratio calculations;
- supports a source-of-truth preset catalog with `RoundTripBuyAndHold`, `BuyAndHold`, `MovingAverageCross`, `EqualWeightRebalance`, and `PeriodicFactorWeight`;
- exposes that preset catalog to the dashboard strategy form and exported run manifest;
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

Drop-in simulator objects:

- datasets can be dropped under `datasets/<dataset_id>/` when they contain `market_prices.csv` or `*_prices.csv`;
- strategy configs can be dropped under `configs/strategies/*.json`;
- the simulator CLI accepts arbitrary dataset and config paths;
- the dashboard lists both fixture and drop-in datasets/configs, exposes the simulator's source-of-truth strategy preset catalog, and can generate editable strategy JSON from a form before a run.
- the dashboard blocks accidental run artifact replacement unless `overwrite` is explicitly enabled for that request.

MVP2 export:

- writes deterministic artifacts: `run_manifest.json`, `equity_curve.csv`, `trades.csv`, `orders.csv`, `order_events.csv`, `fills.csv`, `account_summary.json`, and `audit_summary.json`;
- writes optional `benchmark.csv` return rows when benchmark equity is available and optional `factor_exposure.csv` rows when held positions and factor data are available;
- does not fabricate benchmark or factor files when source data is unavailable;
- records optional audit gaps as gaps, not failures.
- records the simulator input audit status in `run_manifest.json` as `input_audit_status`.
- records optional `benchmark_price` and `benchmark_equity` columns in `equity_curve.csv` when `benchmark_prices.csv` is available.
- records portfolio `last_prices` and `position_quantities` in `equity_curve.csv` for multi-symbol replay validation.
- records deterministic order lifecycle events as `accepted`, `filled`, `partially_filled`, or `cancelled` in `order_events.csv`.
- records base risk rule adjustments, cooldown rejections, and drawdown kill-switch rejections in `risk_events.csv`.

MVP3 validate_run:

- reads an exported run without rerunning simulation;
- validates required artifacts, manifest/account consistency, full and partial order/fill/lifecycle/risk-event consistency, friction-aware accounting replay, final equity, current trades/fills mapping, and audit summary status.

MVP5/MVP6 metrics:

- reads exported `equity_curve.csv` and `trades.csv`;
- writes deterministic `metrics.json`;
- calculates total return, CAGR using equity row intervals with 252 trading days/year, max drawdown, Sortino ratio, realized-PnL win rate, profit factor, trade counts, and fill-notional turnover;
- calculates alpha, beta, Sharpe ratio, upside/downside capture ratio, tracking error, and information ratio when benchmark equity is available;
- reports per-symbol trade counts, realized PnL totals, win rate, and profit factor when trade symbols are available;
- reports `UNAVAILABLE` and gaps rather than inferring missing realized PnL;
- reports a sample-size gap when annualized metrics are calculated from fewer than 20 equity rows.

MVP10 factor report:

- reads exported `fills.csv` and the source dataset `factors.csv`;
- forward-fills factor observations up to each buy fill timestamp;
- reports average buy-side factor value, factor rank, and top-rank buy counts by factor name;
- reads exported `equity_curve.csv` position quantities and reports quantity-weighted holding-period factor exposure;
- writes deterministic `factor_report.json`;
- reports gaps instead of fabricating factor exposure;
- is not a profitability report.

MVP12 factor attribution:

- reads exported `equity_curve.csv` holdings, prices, strategy equity, and optional benchmark equity;
- reads source dataset `factors.csv`;
- forward-fills factor observations to each period start;
- calculates value-weighted portfolio factor exposure, cross-sectional top-minus-bottom factor spread return, exposure-times-spread proxy contribution, residual return versus the factor proxy, and a period PnL reconciliation from holding price movement, trading costs, and unexplained residual;
- writes deterministic `factor_attribution.json`;
- reports gaps instead of fabricating factor returns;
- is deterministic factor proxy attribution plus PnL reconciliation, not a statistical multi-factor regression model.

MVP14 factor risk model:

- reads `factor_attribution.json`;
- uses `active_return` when every candidate period has active return, otherwise uses `strategy_return`;
- requires at least two factors and enough complete observations before fitting deterministic OLS with an intercept;
- writes deterministic `factor_risk_model.json`;
- reports `INCONCLUSIVE` instead of fabricating regression coefficients from thin, missing, or singular data;
- is a risk attribution model, not a forecast, trading signal, or profitability claim.

MVP11 loss classification:

- reads exported `equity_curve.csv`;
- classifies each equity period as `NO_LOSS`, `BENCHMARK_EXPLAINED_LOSS`, `EXCESS_RELATIVE_LOSS`, `STRATEGY_SPECIFIC_LOSS`, or `UNEXPLAINED_LOSS_DATA_GAP`;
- reads `factor_report.json` and `factor_attribution.json` as context when available;
- writes deterministic `loss_classification.json`;
- reports benchmark/factor gaps rather than fabricating explanations.

## Audit Status

- `PASS`: required files and required fields are valid, and no optional readiness gaps were found.
- `INCONCLUSIVE`: required data is valid, but optional benchmark, factor, or cost inputs are missing, so future analyses are unavailable.
- `FAIL`: required files, headers, CSV parseability, timestamp order, duplicate keys, required date/numeric fields, enum values, or trade readiness groups are invalid.

Exit codes:

- `0`: `PASS`, or `INCONCLUSIVE` without `--strict`.
- `1`: `FAIL`, or `INCONCLUSIVE` with `--strict`.
- `2`: usage errors or unexpected internal errors.

## MVP0 Output Audit Inputs

Required:

- `equity_curve.csv`
- `trades.csv`

Optional:

- `benchmark.csv`
- `factor_exposure.csv`

MVP1+ simulation accepts either normalized `market_prices.csv` or multiple sorted `*_prices.csv` files, each using `timestamp,symbol,price`. MVP6+ also accepts optional `benchmark_prices.csv`; the simulator forward-fills benchmark prices onto market timestamps and normalizes benchmark equity from the same initial cash. MVP9 also accepts optional `factors.csv` using `timestamp,symbol,factor_name,factor_value`; missing factors do not block non-factor strategies. The committed `datasets/us_tech_100_simulated` dataset is synthetic historical-style data for integration testing and demos, not real market data.

CSV is parsed as `utf-8-sig` so UTF-8 BOM headers from spreadsheet exports are accepted. Header whitespace is trimmed, header names remain case-sensitive, unknown extra columns are allowed, and blank required cells are treated as missing rather than zero.

## Non-Goals

- No live trading.
- No broker or exchange API integration.
- No real order placement or automation.
- No leverage automation.
- No optimization.
- No opaque ML prediction.
- No production, hosted, or broker-connected dashboard.
- Statistical factor risk modeling is gated and reports `INCONCLUSIVE` unless there are enough complete observations and at least two factors; current demo data is intentionally too small for a meaningful OLS report.
- Per-symbol metrics are trade-realization summaries only; portfolio equity attribution remains portfolio-level.
- No claim that any strategy is profitable.

See `schemas/` for the exact contracts and `docs/mvp-foundation.md` for the staged roadmap and limitations.
