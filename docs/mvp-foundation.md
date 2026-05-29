# MVP Foundation

## Product Thesis

This project is not an alpha-discovery bot and must not promise profit. Strategies are presets running on common base rules. Losses may be acceptable when they come from intended market or factor exposure. Unexplained, excessive, structural, execution, system, data, or order-related losses are failure signals that later phases should classify explicitly.

MVP0 is deliberately limited to simulated dataset audit. It answers one question: is the provided simulated data structurally ready for future analysis?

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

Phase 0: simulated data audit.

Phase 1: baseline performance and cost metrics.

Phase 2: benchmark and factor-relative metrics.

Phase 3: loss classification.

Phase 4: common base risk rule engine.

Phase 5: strategy preset abstraction.

Phase 6: paper/live shadow testing design.

Phase 7: small-capital live validation criteria only.

## Future Data Requirements

Later phases need benchmark series, factor exposure series, complete cost/slippage fields, stable run identifiers, data source provenance, simulated order lifecycle events, live shadow observations, and explicit target market/factor definitions. Missing data should remain a reported gap rather than invented precision.
