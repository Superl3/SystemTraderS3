# MVP Foundation

## Product Thesis

This project is not an alpha-discovery bot and must not promise profit. Strategies are presets running on common base rules. Losses may be acceptable when they come from intended market or factor exposure. Unexplained, excessive, structural, execution, system, data, or order-related losses are failure signals that later phases should classify explicitly.

The current baseline is deliberately small. It proves a local simulated workflow from input dataset audit, to paper-trading simulation with deterministic full or partial fills, to deterministic run artifact export, to artifact self-check, to replay validation, to basic post-run metrics and benchmark-relative metrics, to periodic factor-driven rebalancing, to a thin factor exposure report, to factor-return proxy attribution with PnL reconciliation, to a gated statistical factor risk model, to benchmark-relative loss classification. It does not attempt live risk classification or claims about strategy quality.

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

MVP0 does not enforce financial consistency between `trades.csv` and `equity_curve.csv`. It does not calculate returns, drawdown, beta, correlation, tracking error, capture, turnover, expectancy, PnL, or simulated-to-live reproducibility; those belong to later run-artifact analysis layers.

## Current CLI Layers

Input audit:

```powershell
rtk python -m system_trading_s3.input_audit datasets/us_tech_100_simulated
```

MVP0 output audit:

```powershell
rtk python -m system_trading_s3.audit tests/fixtures/valid_minimal
```

MVP1 simulate:

```powershell
rtk python -m system_trading_s3.simulate tests/fixtures/valid_complete
rtk python -m system_trading_s3.simulate tests/fixtures/valid_multisymbol --config tests/fixtures/sample_config.json
```

MVP2 export:

```powershell
rtk python -m system_trading_s3.simulate tests/fixtures/valid_multisymbol --config tests/fixtures/sample_config.json --export-dir <run_dir> --run-id docs-smoke
```

MVP3 validate:

```powershell
rtk python -m system_trading_s3.validate_run <run_dir>
```

MVP5/MVP6 metrics:

```powershell
rtk python -m system_trading_s3.metrics <run_dir>
```

MVP10 factor report:

```powershell
rtk python -m system_trading_s3.factor_report <run_dir>
```

MVP12 factor attribution:

```powershell
rtk python -m system_trading_s3.factor_attribution <run_dir>
```

MVP14 factor risk model:

```powershell
rtk python -m system_trading_s3.factor_risk_model <run_dir>
```

MVP11 loss classification:

```powershell
rtk python -m system_trading_s3.loss_classification <run_dir>
```

`market_prices.csv` or sorted `*_prices.csv` files are the true simulation inputs for MVP1+. `input_audit` checks those simulator inputs directly, including optional `benchmark_prices.csv`, optional `factors.csv`, and optional `dataset_manifest.json`. Optional `benchmark_prices.csv` is read by the simulation engine, forward-filled onto market timestamps, and exported as normalized benchmark equity. Optional `factors.csv` is read by the simulation engine, forward-filled onto market timestamps, and exposed to strategies as cross-sectional factor data. Generated `equity_curve.csv` and `trades.csv` are run outputs that MVP0 output audit can audit as a self-check.

Drop-in datasets can live under `datasets/<dataset_id>/`, and drop-in strategy configs can live under `configs/strategies/*.json`. The CLI accepts arbitrary dataset/config paths, and the dashboard lists both fixture and drop-in objects. `datasets/us_tech_100_simulated` is a committed 100-symbol synthetic historical-style US tech dataset for integration testing and demos.

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

Completed input audit: simulator input dataset audit separated from output artifact audit.

Completed config/manifest contract hardening: `schemas/simulation_config.schema.json` and `schemas/dataset_manifest.schema.json` document the accepted file contracts, and runtime config parsing rejects unknown top-level and friction/risk keys instead of silently ignoring them.

Completed order lifecycle artifact v1: exported runs include `order_events.csv`, and `validate_run` checks accepted, filled, partially filled, and cancelled lifecycle events against orders and fills.

Completed optional analysis-ready CSV exports v1: exported runs write `benchmark.csv` return rows when benchmark equity exists and `factor_exposure.csv` holding exposure rows when source factor data and held positions exist.

Completed deterministic partial-fill execution v0: optional `execution.max_fill_quantity` caps simulated fill quantity, records `partially_filled` and `cancelled` lifecycle events, can carry residual quantities across later market ticks with `execution.partial_fill_policy=carry_forward`, and is replay-validated from artifacts.

Completed risk rule engine v0: config can define `risk.max_position_weight`, `risk.min_cash_buffer`, `risk.max_order_notional`, `risk.cooldown_periods`, and `risk.max_drawdown_pct`; the engine applies those rules before simulated execution and exports adjustments/rejections to `risk_events.csv`.

Completed per-symbol trade metrics v0: `metrics.json` includes per-symbol trade counts, realized PnL totals, win rate, and profit factor where trade symbols are available.

Completed factor-return proxy attribution v0: `factor_attribution.json` links exported holdings, fills, prices, and factor ranks to report value-weighted exposure, top-minus-bottom factor spread returns, exposure-times-spread proxy contribution, residual return versus the factor proxy, and period PnL reconciliation. This is not a statistical multi-factor regression model.

Completed gated statistical factor risk model v0: `factor_risk_model.json` reads `factor_attribution.json`, requires at least two factors and enough complete observations, fits deterministic OLS only when the design matrix is identifiable, and otherwise reports `INCONCLUSIVE`.

Completed richer risk-adjusted metrics v0: `metrics.json` includes Sortino ratio and benchmark-relative upside/downside capture ratios when the required equity return series exists.

Completed turnover metrics v0: `metrics.json` includes total, buy-side, and sell-side fill-notional turnover relative to average exported equity when `fills.csv` is present.

Completed MVP1: thin paper-trading simulation loop.

Completed MVP2: deterministic run artifact export.

Completed MVP3: run artifact replay and baseline accounting validation.

Completed MVP4: config-driven execution with a source-of-truth strategy preset catalog used by config parsing, dashboard forms, schema checks, and run manifests.

Completed MVP5: deterministic transaction friction and post-run baseline metrics.

Completed MVP6: engine-integrated benchmark logging and benchmark-relative metrics.

Completed MVP7: multi-symbol market ingestion and portfolio state accounting.

Completed MVP8: target-weight strategy intents and integer-share portfolio rebalancing.

Completed MVP9: optional factor data ingestion and periodic factor-weight rebalancing.

Completed MVP10: factor-aware buy-side exposure reporting from exported fills and holding-period exposure reporting from exported position quantities and source `factors.csv`.

Completed MVP11: benchmark-relative loss classification using exported equity and optional factor report/attribution context.

Completed MVP12: factor-return proxy attribution, residual decomposition, and PnL reconciliation using exported holdings, fills, prices, and source factor ranks.

Completed MVP14: gated statistical factor risk modeling from factor attribution, with explicit insufficiency gaps instead of invented coefficients.

Later phase: paper/live shadow testing design.

Later phase: small-capital live validation criteria only.

## Current Limitations

- Default no-config behavior uses the registered `RoundTripBuyAndHold` preset; the old `buy_and_hold_one_unit` name is accepted as a compatibility alias.
- Config-driven runs support only the currently registered preset catalog: `RoundTripBuyAndHold`, `BuyAndHold`, `MovingAverageCross`, `EqualWeightRebalance`, and `PeriodicFactorWeight`.
- Strategy behavior is portfolio-capable but still simple: current built-ins either act across available symbols, use an optional `target_symbol`, emit one-shot equal weights, or periodically rebalance to top-K symbols by one factor.
- Rebalancing uses integer shares, deterministic symbol ordering, and no optimizer.
- Factor data is optional, forward-filled, and used only by strategies that explicitly request it.
- The committed US tech 100 dataset is synthetic deterministic fixture data, not real market data.
- Immediate full fills by default, with optional deterministic partial fills capped by `execution.max_fill_quantity`; residuals are either cancelled or carried across later market ticks based on `execution.partial_fill_policy`.
- Friction is deterministic only: percentage fee plus fixed slippage per fill.
- Benchmark synchronization is deterministic forward-fill only.
- Order lifecycle covers accepted, filled, partially filled, and cancelled states, including deterministic multi-tick residual orders when enabled; it still does not model exchange queues or time priority.
- Risk rule engine v0 supports max position weight, minimum cash buffer, max order notional, buy cooldown, and a drawdown kill switch.
- Standalone factor exposure series is generated only when source factor data and held positions are available; otherwise the missing file remains an explicit audit gap.
- Performance metrics include total return, row-interval CAGR, max drawdown, Sortino ratio, realized-PnL win rate, profit factor, trade counts, fill-notional turnover, alpha, beta, Sharpe ratio, upside/downside capture ratio, tracking error, and information ratio.
- Metrics include per-symbol trade-realization summaries, but portfolio equity attribution remains portfolio-level.
- Statistical multi-factor risk modeling exists only behind sufficiency gates; thin, missing, or singular data remains `INCONCLUSIVE`.
- No live trading, broker integration, production dashboard, optimization, or ML.

## Future Data Requirements

Later phases need complete cost/slippage fields, stable run identifiers, data source provenance, richer simulated order lifecycle events, live shadow observations, and explicit target market/factor definitions. Missing data should remain a reported gap rather than invented precision.
