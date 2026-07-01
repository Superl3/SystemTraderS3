"""Factor-return proxy attribution for exported simulation artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from system_trading_s3 import audit


PASS = "PASS"
INCONCLUSIVE = "INCONCLUSIVE"
FAIL = "FAIL"
REPORT_SCHEMA_VERSION = "mvp12.factor_attribution.v3"
REPORT_FILE_NAME = "factor_attribution.json"
RETURN_QUANT = Decimal("0.000001")


class FactorAttributionError(Exception):
    """Raised when required run artifact inputs are invalid."""


@dataclass(frozen=True)
class EquityRow:
    timestamp: str
    comparable_timestamp: datetime
    equity: Decimal
    benchmark_equity: Decimal | None
    last_prices: dict[str, Decimal]
    positions: dict[str, Decimal]


@dataclass(frozen=True)
class FactorRow:
    timestamp: datetime
    symbol: str
    factor_name: str
    factor_value: Decimal


@dataclass(frozen=True)
class FillRow:
    timestamp: datetime
    symbol: str
    quantity: Decimal
    fee: Decimal
    slippage: Decimal


@dataclass(frozen=True)
class FactorPeriodAttribution:
    start_timestamp: str
    end_timestamp: str
    strategy_return: Decimal
    benchmark_return: Decimal | None
    active_return: Decimal | None
    factor_return_proxy: dict[str, dict[str, object]]
    pnl_attribution: dict[str, object]


@dataclass(frozen=True)
class FactorAttributionResult:
    status: str
    payload: dict[str, Any]
    errors: list[str]


def calculate_factor_attribution(run_artifact_dir: Path | str, dataset_dir: Path | str | None = None) -> FactorAttributionResult:
    run_dir = Path(run_artifact_dir)
    gaps: list[str] = []
    try:
        manifest = _load_json(run_dir / "run_manifest.json")
        equity_rows = _load_equity_rows(run_dir / "equity_curve.csv")
        fills = _load_fills(run_dir / "fills.csv")
    except FactorAttributionError as exc:
        return FactorAttributionResult(status=FAIL, payload=_failed_payload([str(exc)]), errors=[str(exc)])

    if len(equity_rows) < 2:
        gaps.append("factor attribution unavailable because equity_curve has fewer than two rows.")
        return FactorAttributionResult(status=INCONCLUSIVE, payload=_inconclusive_payload(manifest, gaps), errors=[])

    resolved_dataset_dir = _resolve_dataset_dir(run_dir, manifest, dataset_dir)
    if resolved_dataset_dir is None:
        gaps.append("dataset_dir unavailable; factor attribution cannot load factors.csv.")
        return FactorAttributionResult(status=INCONCLUSIVE, payload=_inconclusive_payload(manifest, gaps), errors=[])

    factor_path = resolved_dataset_dir / "factors.csv"
    if not factor_path.is_file():
        gaps.append("factors.csv missing; factor attribution is unavailable.")
        return FactorAttributionResult(
            status=INCONCLUSIVE,
            payload=_inconclusive_payload(manifest, gaps, resolved_dataset_dir),
            errors=[],
        )

    try:
        factor_rows = _load_factor_rows(factor_path)
    except FactorAttributionError as exc:
        return FactorAttributionResult(status=FAIL, payload=_failed_payload([str(exc)]), errors=[str(exc)])

    factor_names = sorted({row.factor_name for row in factor_rows})
    try:
        periods = _period_attributions(equity_rows, factor_rows, factor_names, fills, gaps)
    except FactorAttributionError as exc:
        return FactorAttributionResult(status=FAIL, payload=_failed_payload([str(exc)]), errors=[str(exc)])
    summary = _summary(periods, factor_names)
    status = INCONCLUSIVE if gaps else PASS
    payload: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "run_id": manifest.get("run_id", ""),
        "strategy_name": manifest.get("strategy_name", ""),
        "dataset_dir": str(resolved_dataset_dir),
        "factor_file": str(factor_path),
        "source_files": ["run_manifest.json", "equity_curve.csv", "fills.csv", "factors.csv"],
        "factor_names": factor_names,
        "summary": summary,
        "periods": [_period_payload(period) for period in periods],
        "gaps": gaps,
        "interpretation": "Factor attribution is a deterministic factor-return proxy plus PnL reconciliation from holdings, fills, prices, and factor ranks; it is not a multi-factor regression model or a profitability claim.",
    }
    return FactorAttributionResult(status=status, payload=payload, errors=[])


def write_factor_attribution(run_artifact_dir: Path | str, dataset_dir: Path | str | None = None) -> FactorAttributionResult:
    run_dir = Path(run_artifact_dir)
    result = calculate_factor_attribution(run_dir, dataset_dir)
    if result.status in {PASS, INCONCLUSIVE}:
        _write_json(run_dir / REPORT_FILE_NAME, result.payload)
    return result


def format_factor_attribution_result(result: FactorAttributionResult, report_path: Path | None = None) -> str:
    lines = [f"FACTOR ATTRIBUTION STATUS: {result.status}"]
    if report_path is not None:
        lines.append(f"FACTOR ATTRIBUTION FILE: {report_path}")
    summary = result.payload.get("summary")
    if isinstance(summary, dict):
        lines.append(f"PERIOD COUNT: {summary.get('period_count', 0)}")
        factor_summary = summary.get("factor_summary", {})
        if isinstance(factor_summary, dict):
            lines.append("FACTOR RETURN PROXY:")
            for factor_name in sorted(factor_summary):
                item = factor_summary[factor_name]
                lines.append(
                    "- "
                    f"{factor_name}: average_portfolio_exposure={item.get('average_portfolio_exposure')}, "
                    f"average_factor_spread_return={item.get('average_factor_spread_return')}, "
                    f"average_proxy_contribution={item.get('average_proxy_contribution')}, "
                    f"periods_with_proxy={item.get('periods_with_proxy')}"
                )
        decomposition = summary.get("return_decomposition", {})
        if isinstance(decomposition, dict):
            lines.append("RETURN DECOMPOSITION:")
            lines.append(
                "- "
                f"average_active_return={decomposition.get('average_active_return')}, "
                f"average_factor_proxy_total_contribution={decomposition.get('average_factor_proxy_total_contribution')}, "
                f"average_active_residual_return={decomposition.get('average_active_residual_return')}, "
                f"total_unexplained_pnl={decomposition.get('total_unexplained_pnl')}"
            )
    lines.append("GAPS:")
    gaps = result.payload.get("gaps", [])
    if isinstance(gaps, list) and gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("- none")
    if result.errors:
        lines.append("ERRORS:")
        lines.extend(f"- {error}" for error in result.errors)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create factor-return proxy attribution for exported simulation artifacts.")
    parser.add_argument("run_artifact_dir", type=Path, help="Directory containing exported simulation artifacts.")
    parser.add_argument("--dataset-dir", type=Path, help="Override dataset directory containing factors.csv.")
    args = parser.parse_args(argv)

    if not args.run_artifact_dir.exists() or not args.run_artifact_dir.is_dir():
        print(f"run_artifact_dir must be an existing directory: {args.run_artifact_dir}", file=sys.stderr)
        return 2

    try:
        result = write_factor_attribution(args.run_artifact_dir, args.dataset_dir)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary.
        print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    print(format_factor_attribution_result(result, args.run_artifact_dir / REPORT_FILE_NAME))
    return 0 if result.status in {PASS, INCONCLUSIVE} else 1


def _period_attributions(
    equity_rows: list[EquityRow],
    factor_rows: list[FactorRow],
    factor_names: list[str],
    fills: list[FillRow],
    gaps: list[str],
) -> list[FactorPeriodAttribution]:
    periods: list[FactorPeriodAttribution] = []
    missing_benchmark_count = 0
    missing_proxy_counts = {factor_name: 0 for factor_name in factor_names}
    for index in range(1, len(equity_rows)):
        previous = equity_rows[index - 1]
        current = equity_rows[index]
        strategy_return = _period_return(previous.equity, current.equity, "strategy equity")
        benchmark_return: Decimal | None = None
        active_return: Decimal | None = None
        if previous.benchmark_equity is None or current.benchmark_equity is None:
            missing_benchmark_count += 1
        else:
            benchmark_return = _period_return(previous.benchmark_equity, current.benchmark_equity, "benchmark equity")
            active_return = strategy_return - benchmark_return

        factor_return_proxy: dict[str, dict[str, object]] = {}
        for factor_name in factor_names:
            item = _factor_period_proxy(previous, current, factor_rows, factor_name)
            if item["proxy_contribution"] == "UNAVAILABLE":
                missing_proxy_counts[factor_name] += 1
            factor_return_proxy[factor_name] = item
        pnl_attribution = _pnl_attribution(previous, current, fills)
        periods.append(
            FactorPeriodAttribution(
                start_timestamp=previous.timestamp,
                end_timestamp=current.timestamp,
                strategy_return=strategy_return,
                benchmark_return=benchmark_return,
                active_return=active_return,
                factor_return_proxy=factor_return_proxy,
                pnl_attribution=pnl_attribution,
            )
        )
    if missing_benchmark_count:
        gaps.append(f"benchmark_equity missing for {missing_benchmark_count} period(s); active-return context is limited.")
    for factor_name, count in missing_proxy_counts.items():
        if count:
            gaps.append(f"{factor_name} factor-return proxy unavailable for {count} period(s).")
    return periods


def _factor_period_proxy(
    previous: EquityRow,
    current: EquityRow,
    factor_rows: list[FactorRow],
    factor_name: str,
) -> dict[str, object]:
    snapshot = _factor_snapshot_at(factor_rows, previous.comparable_timestamp, factor_name)
    priced_returns = _priced_returns(previous.last_prices, current.last_prices, snapshot)
    exposure = _portfolio_factor_exposure(previous.positions, previous.last_prices, snapshot)
    spread = _factor_spread_return(priced_returns)
    contribution = None if exposure is None or spread is None else exposure * spread
    return {
        "portfolio_exposure": _format_optional_decimal(exposure),
        "factor_spread_return": _format_optional_decimal(spread),
        "proxy_contribution": _format_optional_decimal(contribution),
        "priced_symbol_count": len(priced_returns),
        "held_symbol_count": len([quantity for quantity in previous.positions.values() if quantity > 0]),
    }


def _portfolio_factor_exposure(
    positions: dict[str, Decimal],
    prices: dict[str, Decimal],
    factor_snapshot: dict[str, Decimal],
) -> Decimal | None:
    weighted_total = Decimal("0")
    value_total = Decimal("0")
    for symbol, quantity in positions.items():
        if quantity <= 0:
            continue
        price = prices.get(symbol)
        factor_value = factor_snapshot.get(symbol)
        if price is None or price <= 0 or factor_value is None:
            continue
        market_value = quantity * price
        weighted_total += factor_value * market_value
        value_total += market_value
    if value_total <= 0:
        return None
    return weighted_total / value_total


def _priced_returns(
    previous_prices: dict[str, Decimal],
    current_prices: dict[str, Decimal],
    factor_snapshot: dict[str, Decimal],
) -> list[tuple[str, Decimal, Decimal]]:
    returns: list[tuple[str, Decimal, Decimal]] = []
    for symbol, factor_value in factor_snapshot.items():
        previous_price = previous_prices.get(symbol)
        current_price = current_prices.get(symbol)
        if previous_price is None or current_price is None or previous_price <= 0:
            continue
        returns.append((symbol, factor_value, (current_price - previous_price) / previous_price))
    return sorted(returns, key=lambda item: (-item[1], item[0]))


def _factor_spread_return(priced_returns: list[tuple[str, Decimal, Decimal]]) -> Decimal | None:
    if len(priced_returns) < 2:
        return None
    bucket_size = max(1, len(priced_returns) // 3)
    top_returns = [item[2] for item in priced_returns[:bucket_size]]
    bottom_returns = [item[2] for item in priced_returns[-bucket_size:]]
    return _average(top_returns) - _average(bottom_returns)


def _summary(periods: list[FactorPeriodAttribution], factor_names: list[str]) -> dict[str, object]:
    factor_summary: dict[str, object] = {}
    strategy_returns: list[Decimal] = []
    active_returns: list[Decimal] = []
    factor_proxy_totals: list[Decimal] = []
    active_residuals: list[Decimal] = []
    strategy_residuals: list[Decimal] = []
    equity_changes: list[Decimal] = []
    holding_price_pnls: list[Decimal] = []
    trade_cashflow_impacts: list[Decimal] = []
    trading_costs: list[Decimal] = []
    unexplained_pnls: list[Decimal] = []
    for period in periods:
        strategy_returns.append(period.strategy_return)
        if period.active_return is not None:
            active_returns.append(period.active_return)
        equity_changes.append(_decimal_from_payload(period.pnl_attribution.get("equity_change")) or Decimal("0"))
        holding_price_pnls.append(_decimal_from_payload(period.pnl_attribution.get("holding_price_pnl")) or Decimal("0"))
        trade_cashflow_impacts.append(_decimal_from_payload(period.pnl_attribution.get("trade_cashflow_impact")) or Decimal("0"))
        trading_costs.append(_decimal_from_payload(period.pnl_attribution.get("trading_costs")) or Decimal("0"))
        unexplained_pnls.append(_decimal_from_payload(period.pnl_attribution.get("unexplained_pnl")) or Decimal("0"))
    for factor_name in factor_names:
        exposures: list[Decimal] = []
        spreads: list[Decimal] = []
        contributions: list[Decimal] = []
        for period in periods:
            item = period.factor_return_proxy.get(factor_name, {})
            exposure = _decimal_from_payload(item.get("portfolio_exposure"))
            spread = _decimal_from_payload(item.get("factor_spread_return"))
            contribution = _decimal_from_payload(item.get("proxy_contribution"))
            if exposure is not None:
                exposures.append(exposure)
            if spread is not None:
                spreads.append(spread)
            if contribution is not None:
                contributions.append(contribution)
        factor_summary[factor_name] = {
            "periods_with_exposure": len(exposures),
            "periods_with_proxy": len(contributions),
            "average_portfolio_exposure": _format_optional_decimal(_average(exposures) if exposures else None),
            "average_factor_spread_return": _format_optional_decimal(_average(spreads) if spreads else None),
            "average_proxy_contribution": _format_optional_decimal(_average(contributions) if contributions else None),
            "total_proxy_contribution": _format_optional_decimal(sum(contributions, Decimal("0")) if contributions else None),
        }
    for period in periods:
        total = _factor_proxy_total(period)
        if total is None:
            continue
        factor_proxy_totals.append(total)
        strategy_residuals.append(period.strategy_return - total)
        if period.active_return is not None:
            active_residuals.append(period.active_return - total)
    return {
        "period_count": len(periods),
        "factor_summary": factor_summary,
        "return_decomposition": {
            "periods_with_active_return": len(active_returns),
            "periods_with_factor_proxy_total": len(factor_proxy_totals),
            "average_strategy_return": _format_optional_decimal(_average(strategy_returns) if strategy_returns else None),
            "average_active_return": _format_optional_decimal(_average(active_returns) if active_returns else None),
            "average_factor_proxy_total_contribution": _format_optional_decimal(_average(factor_proxy_totals) if factor_proxy_totals else None),
            "average_strategy_residual_return": _format_optional_decimal(_average(strategy_residuals) if strategy_residuals else None),
            "average_active_residual_return": _format_optional_decimal(_average(active_residuals) if active_residuals else None),
            "total_equity_change": _format_decimal(sum(equity_changes, Decimal("0"))),
            "total_holding_price_pnl": _format_decimal(sum(holding_price_pnls, Decimal("0"))),
            "total_trade_cashflow_impact": _format_decimal(sum(trade_cashflow_impacts, Decimal("0"))),
            "total_trading_costs": _format_decimal(sum(trading_costs, Decimal("0"))),
            "total_unexplained_pnl": _format_decimal(sum(unexplained_pnls, Decimal("0"))),
        },
    }


def _pnl_attribution(previous: EquityRow, current: EquityRow, fills: list[FillRow]) -> dict[str, object]:
    equity_change = current.equity - previous.equity
    holding_price_pnl = Decimal("0")
    missing_price_symbols: list[str] = []
    for symbol, quantity in previous.positions.items():
        previous_price = previous.last_prices.get(symbol)
        current_price = current.last_prices.get(symbol)
        if previous_price is None or current_price is None:
            missing_price_symbols.append(symbol)
            continue
        holding_price_pnl += quantity * (current_price - previous_price)

    period_fills = [
        fill
        for fill in fills
        if previous.comparable_timestamp < fill.timestamp <= current.comparable_timestamp
    ]
    trading_costs = sum((fill.fee + fill.slippage for fill in period_fills), Decimal("0"))
    trade_cashflow_impact = equity_change - holding_price_pnl
    unexplained_pnl = trade_cashflow_impact + trading_costs
    return {
        "equity_change": _format_decimal(equity_change),
        "holding_price_pnl": _format_decimal(holding_price_pnl),
        "trade_cashflow_impact": _format_decimal(trade_cashflow_impact),
        "trading_costs": _format_decimal(trading_costs),
        "unexplained_pnl": _format_decimal(unexplained_pnl),
        "period_fill_count": len(period_fills),
        "missing_price_symbols": missing_price_symbols,
    }


def _factor_snapshot_at(factor_rows: list[FactorRow], timestamp: datetime, factor_name: str) -> dict[str, Decimal]:
    snapshot: dict[str, Decimal] = {}
    for row in factor_rows:
        if row.timestamp > timestamp:
            break
        if row.factor_name == factor_name:
            snapshot[row.symbol] = row.factor_value
    return snapshot


def _resolve_dataset_dir(run_dir: Path, manifest: dict[str, Any], override: Path | str | None) -> Path | None:
    if override is not None:
        candidate = Path(override)
        return candidate.resolve() if candidate.exists() else candidate

    value = manifest.get("dataset_dir")
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value)
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates.extend([Path.cwd() / candidate, run_dir / candidate])
    for item in candidates:
        if item.is_dir():
            return item.resolve()
    return candidates[0]


def _load_equity_rows(path: Path) -> list[EquityRow]:
    if not path.is_file():
        raise FactorAttributionError("equity_curve.csv is missing.")
    rows = _load_csv_dicts(path)
    equity_rows: list[EquityRow] = []
    for index, row in enumerate(rows, start=2):
        timestamp = row.get("timestamp", "")
        parsed_timestamp = audit._parse_timestamp(timestamp, "date")
        if parsed_timestamp is None or isinstance(parsed_timestamp, datetime):
            raise FactorAttributionError(f"equity_curve.csv row {index} has invalid date timestamp.")
        equity_rows.append(
            EquityRow(
                timestamp=timestamp,
                comparable_timestamp=datetime.combine(parsed_timestamp, time.max),
                equity=_parse_decimal("equity_curve.csv", index, "equity", row.get("equity", "")),
                benchmark_equity=_parse_optional_decimal("equity_curve.csv", index, "benchmark_equity", row.get("benchmark_equity")),
                last_prices=_parse_decimal_mapping("equity_curve.csv", index, "last_prices", row.get("last_prices", "")),
                positions=_parse_decimal_mapping("equity_curve.csv", index, "position_quantities", row.get("position_quantities", "")),
            )
        )
    if not equity_rows:
        raise FactorAttributionError("equity_curve.csv must contain at least one row.")
    return equity_rows


def _load_factor_rows(path: Path) -> list[FactorRow]:
    rows = _load_csv_dicts(path)
    factors: list[FactorRow] = []
    for index, row in enumerate(rows, start=2):
        factors.append(
            FactorRow(
                timestamp=_parse_datetime("factors.csv", index, "timestamp", row.get("timestamp", "")),
                symbol=_required_text("factors.csv", index, "symbol", row.get("symbol", "")),
                factor_name=_required_text("factors.csv", index, "factor_name", row.get("factor_name", "")),
                factor_value=_parse_decimal("factors.csv", index, "factor_value", row.get("factor_value", "")),
            )
        )
    if not factors:
        raise FactorAttributionError("factors.csv must contain at least one row.")
    return sorted(factors, key=lambda row: (row.timestamp, row.symbol, row.factor_name))


def _load_fills(path: Path) -> list[FillRow]:
    if not path.is_file():
        raise FactorAttributionError("fills.csv is missing.")
    rows = _load_csv_dicts(path)
    fills: list[FillRow] = []
    for index, row in enumerate(rows, start=2):
        fills.append(
            FillRow(
                timestamp=_parse_datetime("fills.csv", index, "timestamp", row.get("timestamp", "")),
                symbol=_required_text("fills.csv", index, "symbol", row.get("symbol", "")),
                quantity=_parse_decimal("fills.csv", index, "quantity", row.get("quantity", "")),
                fee=_parse_decimal("fills.csv", index, "fee", row.get("fee", "")),
                slippage=_parse_decimal("fills.csv", index, "slippage", row.get("slippage", "")),
            )
        )
    return fills


def _load_csv_dicts(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise FactorAttributionError(f"{path.name} is missing a header row.")
            return [{key.strip(): value for key, value in row.items()} for row in reader]
    except csv.Error as exc:
        raise FactorAttributionError(f"{path.name} could not be read as CSV: {exc}") from exc
    except OSError as exc:
        raise FactorAttributionError(f"{path.name} cannot be read: {exc}") from exc


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FactorAttributionError(f"{path.name} is missing.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactorAttributionError(f"{path.name} cannot be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise FactorAttributionError(f"{path.name} must contain a JSON object.")
    return payload


def _parse_datetime(file_name: str, row_number: int, column: str, value: str) -> datetime:
    parsed = audit._parse_timestamp(value, "datetime")
    if not isinstance(parsed, datetime):
        raise FactorAttributionError(f"{file_name} row {row_number} has invalid {column}.")
    return parsed


def _parse_decimal(file_name: str, row_number: int, column: str, value: str) -> Decimal:
    parsed = audit._parse_decimal(value)
    if parsed is None:
        raise FactorAttributionError(f"{file_name} row {row_number} has invalid decimal {column}.")
    return parsed


def _parse_optional_decimal(file_name: str, row_number: int, column: str, value: str | None) -> Decimal | None:
    if value is None or audit._is_blank(value):
        return None
    return _parse_decimal(file_name, row_number, column, value)


def _parse_decimal_mapping(file_name: str, row_number: int, column: str, value: str) -> dict[str, Decimal]:
    if audit._is_blank(value):
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise FactorAttributionError(f"{file_name} row {row_number} has invalid {column} JSON.") from exc
    if not isinstance(payload, dict):
        raise FactorAttributionError(f"{file_name} row {row_number} {column} must be a JSON object.")
    parsed: dict[str, Decimal] = {}
    for symbol, raw_value in payload.items():
        if not isinstance(symbol, str) or not isinstance(raw_value, str):
            raise FactorAttributionError(f"{file_name} row {row_number} {column} must map symbols to decimal strings.")
        decimal_value = audit._parse_decimal(raw_value)
        if decimal_value is None:
            raise FactorAttributionError(f"{file_name} row {row_number} has invalid {column} value for {symbol}.")
        if decimal_value != 0:
            parsed[symbol] = decimal_value
    return dict(sorted(parsed.items()))


def _period_return(previous: Decimal, current: Decimal, label: str) -> Decimal:
    if previous <= 0:
        raise FactorAttributionError(f"{label} return unavailable because previous value is nonpositive.")
    return (current - previous) / previous


def _required_text(file_name: str, row_number: int, column: str, value: str) -> str:
    text = value.strip()
    if not text:
        raise FactorAttributionError(f"{file_name} row {row_number} has missing {column}.")
    return text


def _period_payload(period: FactorPeriodAttribution) -> dict[str, object]:
    proxy_total = _factor_proxy_total(period)
    strategy_residual = None if proxy_total is None else period.strategy_return - proxy_total
    active_residual = None if proxy_total is None or period.active_return is None else period.active_return - proxy_total
    return {
        "start_timestamp": period.start_timestamp,
        "end_timestamp": period.end_timestamp,
        "strategy_return": _format_decimal(period.strategy_return),
        "benchmark_return": "UNAVAILABLE" if period.benchmark_return is None else _format_decimal(period.benchmark_return),
        "active_return": "UNAVAILABLE" if period.active_return is None else _format_decimal(period.active_return),
        "factor_return_proxy": period.factor_return_proxy,
        "pnl_attribution": period.pnl_attribution,
        "return_decomposition": {
            "strategy_return": _format_decimal(period.strategy_return),
            "benchmark_return": "UNAVAILABLE" if period.benchmark_return is None else _format_decimal(period.benchmark_return),
            "active_return": "UNAVAILABLE" if period.active_return is None else _format_decimal(period.active_return),
            "factor_proxy_total_contribution": _format_optional_decimal(proxy_total),
            "strategy_residual_return": _format_optional_decimal(strategy_residual),
            "active_residual_return": _format_optional_decimal(active_residual),
        },
    }


def _decimal_from_payload(value: object) -> Decimal | None:
    if not isinstance(value, str) or value == "UNAVAILABLE":
        return None
    return audit._parse_decimal(value)


def _factor_proxy_total(period: FactorPeriodAttribution) -> Decimal | None:
    contributions = [
        contribution
        for item in period.factor_return_proxy.values()
        if (contribution := _decimal_from_payload(item.get("proxy_contribution"))) is not None
    ]
    if not contributions:
        return None
    return sum(contributions, Decimal("0"))


def _average(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _format_decimal(value: Decimal) -> str:
    return format(value.quantize(RETURN_QUANT, rounding=ROUND_HALF_UP), "f")


def _format_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    return _format_decimal(value)


def _failed_payload(errors: list[str]) -> dict[str, object]:
    return {"schema_version": REPORT_SCHEMA_VERSION, "status": FAIL, "errors": errors, "gaps": []}


def _inconclusive_payload(
    manifest: dict[str, Any],
    gaps: list[str],
    dataset_dir: Path | None = None,
) -> dict[str, object]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": INCONCLUSIVE,
        "run_id": manifest.get("run_id", ""),
        "strategy_name": manifest.get("strategy_name", ""),
        "dataset_dir": "" if dataset_dir is None else str(dataset_dir),
        "summary": {"period_count": 0, "factor_summary": {}},
        "periods": [],
        "gaps": gaps,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
