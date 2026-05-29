# MVP Foundation

## Product Thesis

This project is not an alpha-discovery bot and must not promise profit. Strategies are presets running on common base rules. Losses may be acceptable when they come from intended market or factor exposure. Unexplained, excessive, structural, execution, system, data, or order-related losses are failure signals that later phases should classify explicitly.

The current baseline is deliberately small. It proves a local simulated workflow from dataset audit, to paper-trading simulation, to deterministic run artifact export, to replay validation, to basic post-run metrics and benchmark-relative metrics. It does not yet attempt factor exposure modeling, risk classification, or claims about strategy quality.

## Non-Goals

- No live trading.
- No broker or exchange API integration.
- No real order placement or automation.
- No leverage automation.
- No parameter optimization for backtest return.
- No opaque machine-learning prediction.
- No claim that any strategy is profitable.
- No assumption that market or factor drawdown is an automatic strategy failure.

## Strategy Presets

CSV data uses canonical snake_case enum values:

- `market_follow` - Market Follow
- `trend_following` - Trend Following
- `mean_reversion` - Mean Reversion
- `volatility_breakout` - Volatility Breakout
- `defensive` - Defensive
- `cash_pause` - Cash/Pause

## Audit Semantics

Status aggregation is worst-wins: `FAIL > INCONCLUSIVE > PASS`.

- `PASS`: required files and required fields are valid, and no optional readiness gaps were found.
- `INCONCLUSIVE`: required data is valid, but optional benchmark, factor, or cost inputs are missing.
- `FAIL`: required file/header/parseability/order/key/date/numeric/enum/alternative-group validation fails.

MVP0 does not enforce financial consistency between `trades.csv` and `equity_curve.csv`. It does not calculate returns, drawdown, beta, correlation, tracking error, capture, turnover, expectancy, PnL, or simulated-to-live reproducibility.

## Current CLI Layers

MVP0 audit:

```powershell
rtk python -m system_trading_s3.audit tests/fixtures/valid_minimal
```

MVP1 simulate:

```powershell
rtk python -m system_trading_s3.simulate tests/fixtures/valid_complete
rtk python -m system_trading_s3.simulate tests/fixtures/valid_complete --config tests/fixtures/sample_config.json
```

MVP2 export:

```powershell
rtk python -m system_trading_s3.simulate tests/fixtures/valid_complete --export-dir <run_dir> --run-id docs-smoke
```

MVP3 validate:

```powershell
rtk python -m system_trading_s3.validate_run <run_dir>
```

MVP5/MVP6 metrics:

```powershell
rtk python -m system_trading_s3.metrics <run_dir>
```

`market_prices.csv` is the true simulation input for MVP1+. Optional `benchmark_prices.csv` is read by the simulation engine, forward-filled onto market timestamps, and exported as normalized benchmark equity. Generated `equity_curve.csv` and `trades.csv` are run outputs that MVP0 can audit as a self-check.

## CSV Policy

- Files are parsed with `utf-8-sig`.
- Header whitespace is trimmed.
- Header names are case-sensitive after trimming.
- Unknown extra columns are allowed.
- Blank required cells are missing, not zero.
- ISO 8601 is the only accepted date/time format.
- `equity_curve.csv` uses ISO dates in `YYYY-MM-DD` form.
- Trade, benchmark, and factor rows use ISO datetimes in `YYYY-MM-DDTHH:MM:SS[.ffffff][Z|+HH:MM]` form.
- Mixing timezone-aware and timezone-naive datetimes within one file is `FAIL`.

Field sign policy is field-specific:

- Cost fields such as `cost`, `fee`, `commission`, `slippage`, and `spread_cost` must be nonnegative when present.
- `quantity` may be signed or unsigned, but zero is invalid when used for trade readiness.
- `realized_pnl` and `strategy_return` may be positive, zero, or negative.
- `equity` and `portfolio_value` are parsed as finite numbers but are not forced nonnegative in MVP0.
- Price fields must be finite numbers. Instrument-specific negative price policy is future work.

## Staged Roadmap

Completed MVP0: simulated dataset audit.

Completed MVP1: thin paper-trading simulation loop.

Completed MVP2: deterministic run artifact export.

Completed MVP3: run artifact replay and baseline accounting validation.

Completed MVP4: config-driven execution with a static strategy registry.

Completed MVP5: deterministic transaction friction and post-run baseline metrics.

Completed MVP6: engine-integrated benchmark logging and benchmark-relative metrics.

Candidate MVP7: factor exposure inputs and factor-relative metrics using explicit target series.

Later phase: richer risk-adjusted metrics.

Later phase: loss classification.

Later phase: common base risk rule engine.

Later phase: strategy preset abstraction.

Later phase: paper/live shadow testing design.

Later phase: small-capital live validation criteria only.

## Current Limitations

- One symbol only.
- Default no-config behavior still uses the legacy `buy_and_hold_one_unit` strategy.
- Config-driven runs support only `BuyAndHold` and `MovingAverageCross`.
- Immediate fills only.
- Friction is deterministic only: percentage fee plus fixed slippage per fill.
- Benchmark synchronization is deterministic forward-fill only.
- No partial fills.
- No richer order lifecycle.
- No risk budget, sizing model, exposure limits, cooldown, or kill switch.
- No generated factor exposure data.
- Performance metrics are still narrow: total return, row-interval CAGR, max drawdown, realized-PnL win rate, profit factor, trade counts, alpha, beta, Sharpe ratio, tracking error, and information ratio.
- No factor-relative analysis yet.
- No live trading, broker integration, dashboard, optimization, or ML.

## Future Data Requirements

Later phases need factor exposure series, complete cost/slippage fields, stable run identifiers, data source provenance, simulated order lifecycle events, live shadow observations, and explicit target market/factor definitions. Missing data should remain a reported gap rather than invented precision.
