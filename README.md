# SystemTradingS3

MVP0 is a read-only audit foundation for simulated trading datasets. It checks whether input files are structurally ready for later analysis, but it does not calculate performance metrics, infer profit and loss, optimize strategy parameters, train models, connect to brokers, or automate orders.

The long-term product thesis is that strategies are presets running on common base rules. A future system should evaluate outcomes relative to target market or factor exposure, not only by absolute profit. MVP0 only verifies that simulated data is usable enough to support that work later.

## Quickstart

```powershell
rtk python -m unittest discover -s tests
rtk python -m system_trading_s3.audit tests/fixtures/valid_minimal
rtk python -m system_trading_s3.audit --strict tests/fixtures/valid_minimal
```

The audit command is read-only.

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

See `schemas/` for the exact contracts and `docs/mvp-foundation.md` for the staged roadmap and limitations.
