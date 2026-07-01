# Integrated Simulator Contract

This document describes the current drop-in shape for historical-style simulated datasets, strategy configs, runs, and dashboard execution.

## Goal

The simulator should let a user take a historical-style multi-symbol dataset, choose or edit a strategy config, run a deterministic paper simulation, export artifacts, validate them, and calculate metrics without changing engine code.

This remains simulated-only:

- no live trading;
- no broker or exchange API;
- no real order placement;
- no profitability claims.

## Drop-In Dataset

Place a dataset directory under `datasets/<dataset_id>/`, or pass any dataset directory directly to the CLI.

Minimum required simulation input:

```text
market_prices.csv
```

or:

```text
<SYMBOL>_prices.csv
```

Optional files:

```text
benchmark_prices.csv
factors.csv
dataset_manifest.json
```

`market_prices.csv` format:

```csv
timestamp,symbol,price
2024-01-02T16:00:00,USTECH001,26.14
```

`factors.csv` format:

```csv
timestamp,symbol,factor_name,factor_value
2024-01-02T16:00:00,USTECH001,momentum,-0.0875
```

The committed `datasets/us_tech_100_simulated` dataset contains 100 synthetic US tech-style symbols across a historical date range. It is deterministic fixture data, not real market data.

Regenerate it with:

```powershell
rtk python scripts/generate_simulated_universe.py --output-dir datasets/us_tech_100_simulated
```

Validate the downloader output contract without network or optional dependencies:

```powershell
rtk python scripts/download_data.py --offline-smoke --output-dir "$env:TEMP\systemtraders3-offline-smoke"
rtk python -m system_trading_s3.input_audit "$env:TEMP\systemtraders3-offline-smoke"
rtk python -m system_trading_s3.simulate "$env:TEMP\systemtraders3-offline-smoke" --config tests/fixtures/sample_config.json
```

## Drop-In Strategy Config

Place strategy configs under:

```text
configs/strategies/*.json
```

Example:

```json
{
  "initial_cash": "100000",
  "strategy_name": "PeriodicFactorWeight",
  "risk_free_rate": "0.02",
  "strategy_params": {
    "factor_name": "momentum",
    "rebalance_interval": 5,
    "top_k": 10
  },
  "friction": {
    "fee_rate": "0.0005",
    "slippage_per_trade": "0.01"
  },
  "execution": {
    "max_fill_quantity": "1000",
    "partial_fill_policy": "carry_forward"
  },
  "risk": {
    "max_position_weight": "0.25",
    "min_cash_buffer": "1000",
    "max_order_notional": "25000",
    "cooldown_periods": 1,
    "max_drawdown_pct": "12"
  }
}
```

Supported strategy presets come from the simulator preset catalog, which is also used by the dashboard strategy form:

- `BuyAndHold`
- `MovingAverageCross`
- `EqualWeightRebalance`
- `PeriodicFactorWeight`
- `RoundTripBuyAndHold`

## CLI Flow

Run without artifacts:

```powershell
rtk python -m system_trading_s3.simulate datasets/us_tech_100_simulated --config configs/strategies/periodic_momentum_top10.json
```

Export artifacts:

```powershell
$run = "$env:TEMP\systemtraders3-us-tech-demo"
rtk python -m system_trading_s3.input_audit datasets/us_tech_100_simulated
rtk python -m system_trading_s3.simulate datasets/us_tech_100_simulated --config configs/strategies/periodic_momentum_top10.json --export-dir $run --run-id us-tech-demo --overwrite
rtk python -m system_trading_s3.validate_run $run
rtk python -m system_trading_s3.metrics $run
rtk python -m system_trading_s3.factor_report $run
rtk python -m system_trading_s3.factor_attribution $run
rtk python -m system_trading_s3.factor_risk_model $run
rtk python -m system_trading_s3.loss_classification $run
```

## Dashboard Flow

Start:

```powershell
rtk python dashboard/server.py
```

The dashboard lists:

- fixture datasets from `tests/fixtures`;
- drop-in datasets from `datasets`;
- fixture configs from `tests/fixtures/*.json`;
- strategy configs from `configs/strategies/*.json`.
- registered strategies and their editable parameters from `/api/strategies`.

Before launching a run, the dashboard can copy a selected config into an editable JSON textarea or generate the config JSON from a strategy form. Editing that JSON changes the strategy for that run without modifying files on disk. Existing run artifact directories are not replaced unless overwrite is explicitly enabled for the request.

## Object Boundaries

The current portable object boundaries are:

- Dataset directory: market/factor/benchmark input object.
- Strategy config JSON: strategy selection and parameters.
- Run artifact directory: exported immutable simulation result.
- Dashboard run request: dataset path plus either config path or inline config JSON.

These boundaries are intentionally file-based so objects can be copied, replaced, or dropped into another checkout without a database.

The accepted strategy config shape is documented in `schemas/simulation_config.schema.json`. The recommended dataset provenance shape is documented in `schemas/dataset_manifest.schema.json`. Runtime config parsing rejects unknown top-level config keys and unknown friction/risk keys so misspelled or imaginary controls do not appear to work.

Run artifacts include order intent rows (`orders.csv`), lifecycle rows (`order_events.csv`), base risk rule rows (`risk_events.csv`), optional benchmark return rows (`benchmark.csv`), optional holding factor exposure rows (`factor_exposure.csv`), and optional analysis reports such as `metrics.json`, `factor_report.json`, `factor_attribution.json`, `factor_risk_model.json`, and `loss_classification.json`. The simulator fills fully by default, can cancel deterministic partial-fill remainders, or can carry residual order quantity across later market ticks with `execution.partial_fill_policy=carry_forward`; the artifact contract has lifecycle and risk surfaces that later richer execution or risk models can extend.

## Current Limits

- Historical US tech 100 data is synthetic fixture data.
- The Yahoo downloader remains optional and depends on `yfinance` and `pandas`.
- Dashboard UI uses CDN assets.
- Metrics include portfolio-level equity metrics, Sortino/capture ratios, fill-notional turnover, and per-symbol trade-realization summaries.
- Metrics report a gap when annualized values are based on fewer than 20 equity rows.
- Execution supports deterministic full fills by default, optional capped partial fills, and optional carry-forward residual orders across later market ticks.
- Factor reporting covers buy-fill exposure and quantity-weighted holding exposure.
- Factor attribution is a holdings/fills/prices/factor-rank proxy with top-minus-bottom factor spread returns, active/residual return decomposition, and period PnL reconciliation.
- Statistical factor risk modeling is gated: `factor_risk_model.json` reports `INCONCLUSIVE` unless there are enough complete observations, at least two factors, and an identifiable OLS design matrix.
- Loss classification is benchmark-relative with factor context, not a forecast or profitability judgment.
- Risk rules cover basic pre-trade sizing/exposure/cash constraints, buy cooldown, and drawdown kill-switch rejection.
