"""Post-run deterministic metrics for exported simulation artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any

from system_trading_s3 import audit


PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"
METRICS_SCHEMA_VERSION = "mvp6.metrics.v1"
METRICS_FILE_NAME = "metrics.json"
TRADING_DAYS_PER_YEAR = Decimal("252")
METRIC_QUANT = Decimal("0.000001")


class MetricsError(Exception):
    """Raised when run artifacts cannot support metrics calculation."""


@dataclass(frozen=True)
class EquityMetricRow:
    timestamp: str
    equity: Decimal
    benchmark_equity: Decimal | None = None


@dataclass(frozen=True)
class MetricsResult:
    status: str
    payload: dict[str, Any]
    errors: list[str]


def calculate_metrics(run_artifact_dir: Path | str) -> MetricsResult:
    run_dir = Path(run_artifact_dir)
    errors: list[str] = []
    gaps: list[str] = []

    try:
        equity_rows = _load_equity_rows(run_dir / "equity_curve.csv")
        realized_pnls, total_trade_count, missing_realized_count = _load_realized_pnls(run_dir / "trades.csv")
        risk_free_rate = _load_risk_free_rate(run_dir / "run_manifest.json", gaps)
    except MetricsError as exc:
        return MetricsResult(status=FAIL, payload=_failed_payload([str(exc)]), errors=[str(exc)])

    total_return = _total_return_pct(equity_rows, gaps)
    cagr = _cagr_pct(equity_rows, gaps)
    max_drawdown = _max_drawdown_pct(equity_rows, gaps)
    win_rate, profit_factor = _trade_outcome_metrics(
        realized_pnls=realized_pnls,
        total_trade_count=total_trade_count,
        missing_realized_count=missing_realized_count,
        gaps=gaps,
    )
    benchmark_relative = _benchmark_relative_metrics(equity_rows, risk_free_rate, gaps)

    payload: dict[str, Any] = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "status": PASS,
        "source_files": ["equity_curve.csv", "trades.csv"],
        "trading_days_per_year": int(TRADING_DAYS_PER_YEAR),
        "total_return_pct": _format_optional_metric(total_return),
        "cagr_pct": _format_optional_metric(cagr),
        "max_drawdown_pct": _format_optional_metric(max_drawdown),
        "win_rate_pct": _format_optional_metric(win_rate),
        "profit_factor": _format_profit_factor(profit_factor),
        "total_number_of_trades": total_trade_count,
        "realized_trade_count": len(realized_pnls),
        "benchmark_relative": benchmark_relative,
        "gaps": gaps,
    }
    return MetricsResult(status=PASS, payload=payload, errors=[])


def write_metrics(run_artifact_dir: Path | str) -> MetricsResult:
    run_dir = Path(run_artifact_dir)
    result = calculate_metrics(run_dir)
    if result.status == PASS:
        _write_json(run_dir / METRICS_FILE_NAME, result.payload)
    return result


def format_metrics_result(result: MetricsResult, metrics_path: Path | None = None) -> str:
    lines = [f"METRICS STATUS: {result.status}"]
    if metrics_path is not None:
        lines.append(f"METRICS FILE: {metrics_path}")
    payload = result.payload
    for key, label in [
        ("total_return_pct", "TOTAL RETURN PCT"),
        ("cagr_pct", "CAGR PCT"),
        ("max_drawdown_pct", "MAX DRAWDOWN PCT"),
        ("win_rate_pct", "WIN RATE PCT"),
        ("profit_factor", "PROFIT FACTOR"),
        ("total_number_of_trades", "TOTAL NUMBER OF TRADES"),
    ]:
        if key in payload:
            lines.append(f"{label}: {payload[key]}")
    benchmark_relative = payload.get("benchmark_relative")
    if isinstance(benchmark_relative, dict):
        lines.append("BENCHMARK RELATIVE:")
        for key, label in [
            ("alpha_pct", "ALPHA PCT"),
            ("beta", "BETA"),
            ("sharpe_ratio", "SHARPE RATIO"),
            ("tracking_error_pct", "TRACKING ERROR PCT"),
            ("information_ratio", "INFORMATION RATIO"),
        ]:
            lines.append(f"- {label}: {benchmark_relative.get(key, UNAVAILABLE)}")
    lines.append("GAPS:")
    gaps = payload.get("gaps", [])
    if isinstance(gaps, list) and gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("- none")
    if result.errors:
        lines.append("ERRORS:")
        lines.extend(f"- {error}" for error in result.errors)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calculate deterministic post-run metrics for exported artifacts.")
    parser.add_argument("run_artifact_dir", type=Path, help="Directory containing exported simulation artifacts.")
    args = parser.parse_args(argv)

    if not args.run_artifact_dir.exists() or not args.run_artifact_dir.is_dir():
        print(f"run_artifact_dir must be an existing directory: {args.run_artifact_dir}", file=sys.stderr)
        return 2

    try:
        result = write_metrics(args.run_artifact_dir)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary.
        print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    print(format_metrics_result(result, args.run_artifact_dir / METRICS_FILE_NAME))
    return 0 if result.status == PASS else 1


def _load_equity_rows(path: Path) -> list[EquityMetricRow]:
    if not path.is_file():
        raise MetricsError("equity_curve.csv is missing.")
    rows = _load_csv_dicts(path)
    equity_rows: list[EquityMetricRow] = []
    for index, row in enumerate(rows, start=2):
        timestamp = row.get("timestamp", "")
        if audit._parse_timestamp(timestamp, "date") is None:
            raise MetricsError(f"equity_curve.csv row {index} has invalid date timestamp.")
        equity_rows.append(
            EquityMetricRow(
                timestamp=timestamp,
                equity=_parse_decimal("equity_curve.csv", index, "equity", row.get("equity", "")),
                benchmark_equity=_parse_optional_decimal("equity_curve.csv", index, "benchmark_equity", row.get("benchmark_equity")),
            )
        )
    if not equity_rows:
        raise MetricsError("equity_curve.csv must contain at least one row.")
    return equity_rows


def _load_realized_pnls(path: Path) -> tuple[list[Decimal], int, int]:
    if not path.is_file():
        raise MetricsError("trades.csv is missing.")
    rows = _load_csv_dicts(path)
    realized: list[Decimal] = []
    missing_realized_count = 0
    for index, row in enumerate(rows, start=2):
        value = row.get("realized_pnl", "")
        if audit._is_blank(value):
            missing_realized_count += 1
            continue
        realized.append(_parse_decimal("trades.csv", index, "realized_pnl", value))
    return realized, len(rows), missing_realized_count


def _load_csv_dicts(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise MetricsError(f"{path.name} is missing a header row.")
            return [{key.strip(): value for key, value in row.items()} for row in reader]
    except csv.Error as exc:
        raise MetricsError(f"{path.name} could not be read as CSV: {exc}") from exc


def _parse_decimal(file_name: str, row_number: int, column: str, value: str) -> Decimal:
    parsed = audit._parse_decimal(value)
    if parsed is None:
        raise MetricsError(f"{file_name} row {row_number} has invalid decimal {column}.")
    return parsed


def _parse_optional_decimal(file_name: str, row_number: int, column: str, value: str | None) -> Decimal | None:
    if value is None or audit._is_blank(value):
        return None
    return _parse_decimal(file_name, row_number, column, value)


def _load_risk_free_rate(path: Path, gaps: list[str]) -> Decimal:
    if not path.is_file():
        gaps.append("run_manifest.json missing; risk_free_rate defaulted to 0.")
        return Decimal("0")
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        gaps.append("run_manifest.json unreadable; risk_free_rate defaulted to 0.")
        return Decimal("0")
    if not isinstance(payload, dict):
        gaps.append("run_manifest.json invalid; risk_free_rate defaulted to 0.")
        return Decimal("0")
    value = payload.get("risk_free_rate", "0")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        gaps.append("run_manifest.json risk_free_rate invalid; defaulted to 0.")
        return Decimal("0")
    parsed = audit._parse_decimal(value)
    if parsed is None:
        gaps.append("run_manifest.json risk_free_rate invalid; defaulted to 0.")
        return Decimal("0")
    return parsed


def _total_return_pct(equity_rows: list[EquityMetricRow], gaps: list[str]) -> Decimal | None:
    start = equity_rows[0].equity
    end = equity_rows[-1].equity
    if start <= 0:
        gaps.append("total_return_pct unavailable because starting equity is nonpositive.")
        return None
    return (end - start) / start * Decimal("100")


def _cagr_pct(equity_rows: list[EquityMetricRow], gaps: list[str]) -> Decimal | None:
    periods = len(equity_rows) - 1
    start = equity_rows[0].equity
    end = equity_rows[-1].equity
    if periods <= 0:
        gaps.append("cagr_pct unavailable because equity_curve has fewer than two rows.")
        return None
    if start <= 0 or end <= 0:
        gaps.append("cagr_pct unavailable because starting or ending equity is nonpositive.")
        return None
    with localcontext() as context:
        context.prec = 50
        exponent = TRADING_DAYS_PER_YEAR / Decimal(periods)
        return (((end / start).ln() * exponent).exp() - Decimal("1")) * Decimal("100")


def _max_drawdown_pct(equity_rows: list[EquityMetricRow], gaps: list[str]) -> Decimal | None:
    peak = equity_rows[0].equity
    if peak <= 0:
        gaps.append("max_drawdown_pct unavailable because initial equity is nonpositive.")
        return None
    max_drawdown = Decimal("0")
    for row in equity_rows:
        if row.equity > peak:
            peak = row.equity
        if peak <= 0:
            gaps.append("max_drawdown_pct unavailable because equity peak is nonpositive.")
            return None
        drawdown = (peak - row.equity) / peak * Decimal("100")
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def _trade_outcome_metrics(
    realized_pnls: list[Decimal],
    total_trade_count: int,
    missing_realized_count: int,
    gaps: list[str],
) -> tuple[Decimal | None, Decimal | str | None]:
    if missing_realized_count:
        gaps.append(f"realized_pnl missing for {missing_realized_count} of {total_trade_count} trade rows.")
    if not realized_pnls:
        gaps.append("win_rate_pct and profit_factor unavailable because no realized_pnl rows are available.")
        return None, None

    winning_trades = sum(1 for value in realized_pnls if value > 0)
    win_rate = Decimal(winning_trades) / Decimal(len(realized_pnls)) * Decimal("100")
    gross_profit = sum((value for value in realized_pnls if value > 0), Decimal("0"))
    gross_loss = sum((-value for value in realized_pnls if value < 0), Decimal("0"))
    if gross_loss == 0:
        if gross_profit > 0:
            return win_rate, "INF"
        gaps.append("profit_factor unavailable because gross profit and gross loss are both zero.")
        return win_rate, None
    return win_rate, gross_profit / gross_loss


def _benchmark_relative_metrics(
    equity_rows: list[EquityMetricRow],
    risk_free_rate: Decimal,
    top_level_gaps: list[str],
) -> dict[str, str | bool]:
    gaps: list[str] = []
    unavailable = _unavailable_benchmark_relative(risk_free_rate, benchmark_available=False, gaps=gaps)
    if any(row.benchmark_equity is None for row in equity_rows):
        gaps.append("benchmark_relative unavailable because benchmark_equity is missing.")
        top_level_gaps.extend(gaps)
        return unavailable | {"gaps": "; ".join(gaps)}

    strategy_returns = _period_returns([row.equity for row in equity_rows], "strategy equity", gaps)
    benchmark_returns = _period_returns(
        [row.benchmark_equity for row in equity_rows if row.benchmark_equity is not None],
        "benchmark equity",
        gaps,
    )
    if strategy_returns is None or benchmark_returns is None or len(strategy_returns) != len(benchmark_returns):
        top_level_gaps.extend(gaps)
        return unavailable | {"gaps": "; ".join(gaps)}

    benchmark_available = True
    active_returns = [strategy - benchmark for strategy, benchmark in zip(strategy_returns, benchmark_returns)]
    alpha = _mean(active_returns) * TRADING_DAYS_PER_YEAR * Decimal("100")
    benchmark_variance = _variance(benchmark_returns)
    beta: Decimal | None = None
    if benchmark_variance is None or benchmark_variance == 0:
        gaps.append("beta unavailable because benchmark return variance is zero or insufficient.")
    else:
        covariance = _covariance(strategy_returns, benchmark_returns)
        beta = None if covariance is None else covariance / benchmark_variance

    sharpe = _sharpe_ratio(strategy_returns, risk_free_rate, gaps)
    tracking_error = _annualized_std(active_returns)
    information_ratio: Decimal | None = None
    if tracking_error is None:
        gaps.append("tracking_error_pct unavailable because active returns are insufficient.")
    elif tracking_error == 0:
        gaps.append("information_ratio unavailable because tracking error is zero.")
    else:
        information_ratio = (_mean(active_returns) * TRADING_DAYS_PER_YEAR) / tracking_error

    top_level_gaps.extend(gaps)
    return {
        "benchmark_available": benchmark_available,
        "risk_free_rate": _format_decimal(risk_free_rate),
        "alpha_pct": _format_optional_metric(alpha),
        "beta": _format_optional_metric(beta),
        "sharpe_ratio": _format_optional_metric(sharpe),
        "tracking_error_pct": _format_optional_metric(None if tracking_error is None else tracking_error * Decimal("100")),
        "information_ratio": _format_optional_metric(information_ratio),
        "gaps": "; ".join(gaps),
    }


def _unavailable_benchmark_relative(
    risk_free_rate: Decimal,
    benchmark_available: bool,
    gaps: list[str],
) -> dict[str, str | bool]:
    return {
        "benchmark_available": benchmark_available,
        "risk_free_rate": _format_decimal(risk_free_rate),
        "alpha_pct": UNAVAILABLE,
        "beta": UNAVAILABLE,
        "sharpe_ratio": UNAVAILABLE,
        "tracking_error_pct": UNAVAILABLE,
        "information_ratio": UNAVAILABLE,
        "gaps": "; ".join(gaps),
    }


def _period_returns(values: list[Decimal], label: str, gaps: list[str]) -> list[Decimal] | None:
    if len(values) < 2:
        gaps.append(f"{label} returns unavailable because fewer than two observations are present.")
        return None
    returns: list[Decimal] = []
    for index in range(1, len(values)):
        previous = values[index - 1]
        current = values[index]
        if previous <= 0:
            gaps.append(f"{label} returns unavailable because a previous value is nonpositive.")
            return None
        returns.append((current - previous) / previous)
    return returns


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _variance(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = _mean(values)
    return sum(((value - mean) ** 2 for value in values), Decimal("0")) / Decimal(len(values))


def _covariance(first: list[Decimal], second: list[Decimal]) -> Decimal | None:
    if len(first) != len(second) or len(first) < 2:
        return None
    first_mean = _mean(first)
    second_mean = _mean(second)
    total = sum(((left - first_mean) * (right - second_mean) for left, right in zip(first, second)), Decimal("0"))
    return total / Decimal(len(first))


def _annualized_std(values: list[Decimal]) -> Decimal | None:
    variance = _variance(values)
    if variance is None:
        return None
    with localcontext() as context:
        context.prec = 50
        return (variance * TRADING_DAYS_PER_YEAR).sqrt()


def _sharpe_ratio(strategy_returns: list[Decimal], risk_free_rate: Decimal, gaps: list[str]) -> Decimal | None:
    annualized_std = _annualized_std(strategy_returns)
    if annualized_std is None:
        gaps.append("sharpe_ratio unavailable because strategy returns are insufficient.")
        return None
    if annualized_std == 0:
        gaps.append("sharpe_ratio unavailable because annualized strategy volatility is zero.")
        return None
    annualized_return = _mean(strategy_returns) * TRADING_DAYS_PER_YEAR
    return (annualized_return - risk_free_rate) / annualized_std


def _format_optional_metric(value: Decimal | None) -> str:
    if value is None:
        return UNAVAILABLE
    return _format_decimal(value)


def _format_profit_factor(value: Decimal | str | None) -> str:
    if value is None:
        return UNAVAILABLE
    if isinstance(value, str):
        return value
    return _format_decimal(value)


def _format_decimal(value: Decimal) -> str:
    if value == 0:
        value = Decimal("0")
    return format(value.quantize(METRIC_QUANT, rounding=ROUND_HALF_UP), "f")


def _failed_payload(errors: list[str]) -> dict[str, Any]:
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "status": FAIL,
        "source_files": ["equity_curve.csv", "trades.csv"],
        "gaps": [],
        "errors": errors,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
