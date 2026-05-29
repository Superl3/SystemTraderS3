"""Validate exported simulation run artifacts without rerunning simulation."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from system_trading_s3 import audit
from system_trading_s3 import simulate


PASS = "PASS"
FAIL = "FAIL"
REQUIRED_ARTIFACTS = list(simulate.ARTIFACT_FILES)


class RunValidationError(Exception):
    """Raised when run artifacts cannot be loaded or validated."""


@dataclass(frozen=True)
class OrderArtifact:
    order_id: str
    timestamp: datetime
    symbol: str
    side: str
    quantity: Decimal
    requested_price: Decimal
    status: str


@dataclass(frozen=True)
class FillArtifact:
    fill_id: str
    order_id: str
    timestamp: datetime
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    fee: Decimal
    slippage: Decimal


@dataclass(frozen=True)
class EquityArtifact:
    timestamp: str
    equity: Decimal
    cash: Decimal
    position_value: Decimal
    symbol: str
    position_quantity: Decimal
    last_price: Decimal


@dataclass(frozen=True)
class RunValidationResult:
    status: str
    run_id: str | None
    strategy_name: str | None
    order_count: int
    fill_count: int
    replayed_final_cash: Decimal | None
    replayed_final_positions: dict[str, Decimal]
    replayed_final_equity: Decimal | None
    artifact_audit_status: str | None
    gaps_or_limitations: list[str]
    errors: list[str]


def validate_run_artifacts(run_artifact_dir: Path | str) -> RunValidationResult:
    run_dir = Path(run_artifact_dir)
    errors: list[str] = []
    gaps_or_limitations = [
        "trades/fills consistency is checked only at the current MVP2 fill-to-trade mapping level.",
    ]

    try:
        _require_artifacts(run_dir)
        manifest = _load_json(run_dir / "run_manifest.json")
        account_summary = _load_json(run_dir / "account_summary.json")
        audit_summary = _load_json(run_dir / "audit_summary.json")
        orders = _load_orders(run_dir / "orders.csv")
        fills = _load_fills(run_dir / "fills.csv")
        equity_curve = _load_equity_curve(run_dir / "equity_curve.csv")
        trades = _load_csv_dicts(run_dir / "trades.csv")
    except RunValidationError as exc:
        return RunValidationResult(
            status=FAIL,
            run_id=None,
            strategy_name=None,
            order_count=0,
            fill_count=0,
            replayed_final_cash=None,
            replayed_final_positions={},
            replayed_final_equity=None,
            artifact_audit_status=None,
            gaps_or_limitations=gaps_or_limitations,
            errors=[str(exc)],
        )

    run_id = _string_or_none(manifest.get("run_id"))
    strategy_name = _string_or_none(manifest.get("strategy_name"))
    artifact_audit_status = _string_or_none(audit_summary.get("audit_status"))

    _validate_manifest(manifest, account_summary, errors)
    _validate_order_fill_consistency(orders, fills, errors)
    replay_cash, replay_positions = _replay_account(account_summary, fills, errors)
    replay_equity = _validate_equity(equity_curve, account_summary, replay_cash, replay_positions, errors)
    _validate_trades_to_fills(trades, fills, errors)
    _validate_audit_summary(audit_summary, errors, gaps_or_limitations)
    _validate_counts(account_summary, orders, fills, trades, errors)

    return RunValidationResult(
        status=PASS if not errors else FAIL,
        run_id=run_id,
        strategy_name=strategy_name,
        order_count=len(orders),
        fill_count=len(fills),
        replayed_final_cash=replay_cash,
        replayed_final_positions=dict(sorted(replay_positions.items())),
        replayed_final_equity=replay_equity,
        artifact_audit_status=artifact_audit_status,
        gaps_or_limitations=gaps_or_limitations,
        errors=errors,
    )


def format_validation_result(result: RunValidationResult) -> str:
    lines = [
        f"VALIDATION STATUS: {result.status}",
        f"RUN ID: {result.run_id or 'UNAVAILABLE'}",
        f"STRATEGY: {result.strategy_name or 'UNAVAILABLE'}",
        f"ORDER COUNT: {result.order_count}",
        f"FILL COUNT: {result.fill_count}",
        f"REPLAYED FINAL CASH: {_format_optional_decimal(result.replayed_final_cash)}",
        f"REPLAYED FINAL POSITIONS: {simulate.format_positions(result.replayed_final_positions)}",
        f"REPLAYED FINAL EQUITY: {_format_optional_decimal(result.replayed_final_equity)}",
        f"ARTIFACT AUDIT STATUS: {result.artifact_audit_status or 'UNAVAILABLE'}",
        "GAPS OR LIMITATIONS:",
    ]
    if result.gaps_or_limitations:
        lines.extend(f"- {item}" for item in result.gaps_or_limitations)
    else:
        lines.append("- none")
    lines.append("ERRORS:")
    if result.errors:
        lines.extend(f"- {item}" for item in result.errors)
    else:
        lines.append("- none")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate exported simulation run artifacts.")
    parser.add_argument("run_artifact_dir", type=Path, help="Directory containing MVP2 run artifacts.")
    args = parser.parse_args(argv)

    if not args.run_artifact_dir.exists() or not args.run_artifact_dir.is_dir():
        print(f"run_artifact_dir must be an existing directory: {args.run_artifact_dir}", file=sys.stderr)
        return 2

    try:
        result = validate_run_artifacts(args.run_artifact_dir)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary.
        print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    print(format_validation_result(result))
    return 0 if result.status == PASS else 1


def _require_artifacts(run_dir: Path) -> None:
    missing = [file_name for file_name in REQUIRED_ARTIFACTS if not (run_dir / file_name).is_file()]
    if missing:
        raise RunValidationError(f"Missing required artifact files: {', '.join(missing)}.")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RunValidationError(f"{path.name} could not be read as JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunValidationError(f"{path.name} must contain a JSON object.")
    return payload


def _load_orders(path: Path) -> list[OrderArtifact]:
    rows = _load_csv_dicts(path)
    orders: list[OrderArtifact] = []
    seen: set[str] = set()
    required = ["order_id", "timestamp", "symbol", "side", "quantity", "requested_price", "status"]
    for index, row in enumerate(rows, start=2):
        _require_columns(path.name, row, required)
        order_id = _required_text(path.name, index, row, "order_id")
        if order_id in seen:
            raise RunValidationError(f"orders.csv row {index} duplicate order_id: {order_id}.")
        seen.add(order_id)
        orders.append(
            OrderArtifact(
                order_id=order_id,
                timestamp=_parse_datetime(path.name, index, row["timestamp"]),
                symbol=_required_text(path.name, index, row, "symbol"),
                side=_parse_side(path.name, index, row["side"]),
                quantity=_parse_decimal(path.name, index, "quantity", row["quantity"]),
                requested_price=_parse_decimal(path.name, index, "requested_price", row["requested_price"]),
                status=_required_text(path.name, index, row, "status"),
            )
        )
    return orders


def _load_fills(path: Path) -> list[FillArtifact]:
    rows = _load_csv_dicts(path)
    fills: list[FillArtifact] = []
    seen: set[str] = set()
    required = ["fill_id", "order_id", "timestamp", "symbol", "side", "quantity", "fill_price", "fee", "slippage"]
    for index, row in enumerate(rows, start=2):
        _require_columns(path.name, row, required)
        fill_id = _required_text(path.name, index, row, "fill_id")
        if fill_id in seen:
            raise RunValidationError(f"fills.csv row {index} duplicate fill_id: {fill_id}.")
        seen.add(fill_id)
        fills.append(
            FillArtifact(
                fill_id=fill_id,
                order_id=_required_text(path.name, index, row, "order_id"),
                timestamp=_parse_datetime(path.name, index, row["timestamp"]),
                symbol=_required_text(path.name, index, row, "symbol"),
                side=_parse_side(path.name, index, row["side"]),
                quantity=_parse_decimal(path.name, index, "quantity", row["quantity"]),
                fill_price=_parse_decimal(path.name, index, "fill_price", row["fill_price"]),
                fee=_parse_decimal(path.name, index, "fee", row["fee"]),
                slippage=_parse_decimal(path.name, index, "slippage", row["slippage"]),
            )
        )
    return fills


def _load_equity_curve(path: Path) -> list[EquityArtifact]:
    rows = _load_csv_dicts(path)
    equity_rows: list[EquityArtifact] = []
    required = ["timestamp", "equity", "cash", "position_value", "symbol", "position_quantity", "last_price"]
    for index, row in enumerate(rows, start=2):
        _require_columns(path.name, row, required)
        timestamp = _required_text(path.name, index, row, "timestamp")
        if audit._parse_timestamp(timestamp, "date") is None:
            raise RunValidationError(f"equity_curve.csv row {index} has invalid date timestamp.")
        equity_rows.append(
            EquityArtifact(
                timestamp=timestamp,
                equity=_parse_decimal(path.name, index, "equity", row["equity"]),
                cash=_parse_decimal(path.name, index, "cash", row["cash"]),
                position_value=_parse_decimal(path.name, index, "position_value", row["position_value"]),
                symbol=_required_text(path.name, index, row, "symbol"),
                position_quantity=_parse_decimal(path.name, index, "position_quantity", row["position_quantity"]),
                last_price=_parse_decimal(path.name, index, "last_price", row["last_price"]),
            )
        )
    if not equity_rows:
        raise RunValidationError("equity_curve.csv must contain at least one row.")
    return equity_rows


def _load_csv_dicts(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise RunValidationError(f"{path.name} is missing a header row.")
            return [{key.strip(): value for key, value in row.items()} for row in reader]
    except csv.Error as exc:
        raise RunValidationError(f"{path.name} could not be read as CSV: {exc}") from exc


def _validate_manifest(manifest: dict[str, Any], account_summary: dict[str, Any], errors: list[str]) -> None:
    run_id = _string_or_none(manifest.get("run_id"))
    strategy_name = _string_or_none(manifest.get("strategy_name"))
    initial_cash = _string_or_none(manifest.get("initial_cash"))
    summary_initial_cash = _string_or_none(account_summary.get("initial_cash"))
    assumptions = manifest.get("simulation_assumptions")

    if not run_id:
        errors.append("run_manifest.json missing run_id.")
    if not strategy_name:
        errors.append("run_manifest.json missing strategy_name.")
    if not initial_cash:
        errors.append("run_manifest.json missing initial_cash.")
    if initial_cash and summary_initial_cash and initial_cash != summary_initial_cash:
        errors.append("run_manifest.json initial_cash does not match account_summary.json initial_cash.")
    if not isinstance(assumptions, list) or not assumptions:
        errors.append("run_manifest.json missing simulation_assumptions.")


def _validate_order_fill_consistency(orders: list[OrderArtifact], fills: list[FillArtifact], errors: list[str]) -> None:
    orders_by_id = {order.order_id: order for order in orders}
    filled_quantities: dict[str, Decimal] = {order.order_id: Decimal("0") for order in orders}

    for fill in fills:
        order = orders_by_id.get(fill.order_id)
        if order is None:
            errors.append(f"fills.csv fill_id {fill.fill_id} references unknown order_id {fill.order_id}.")
            continue
        if fill.side != order.side or fill.symbol != order.symbol:
            errors.append(f"fills.csv fill_id {fill.fill_id} side/symbol does not match order {fill.order_id}.")
        filled_quantities[fill.order_id] += fill.quantity
        if filled_quantities[fill.order_id] > order.quantity:
            errors.append(f"fills.csv fill quantity exceeds order quantity for order_id {fill.order_id}.")
        if order.status != "filled":
            errors.append(f"fills.csv fill_id {fill.fill_id} references non-filled order {fill.order_id}.")

    for order in orders:
        if order.status not in {"filled"}:
            errors.append(f"orders.csv order_id {order.order_id} has unsupported MVP3 status {order.status}.")
        if order.status == "filled" and filled_quantities.get(order.order_id, Decimal("0")) == 0:
            errors.append(f"orders.csv order_id {order.order_id} is filled but has no fill.")


def _replay_account(
    account_summary: dict[str, Any],
    fills: list[FillArtifact],
    errors: list[str],
) -> tuple[Decimal | None, dict[str, Decimal]]:
    initial_cash = _parse_summary_decimal(account_summary, "initial_cash", errors)
    expected_final_cash = _parse_summary_decimal(account_summary, "final_cash", errors)
    expected_positions = _parse_positions(account_summary.get("final_positions"), errors)
    if initial_cash is None:
        return None, {}

    cash = initial_cash
    positions: dict[str, Decimal] = {}
    for fill in sorted(fills, key=lambda item: (item.timestamp, item.order_id, item.fill_id)):
        if fill.quantity < 0:
            errors.append(f"fills.csv fill_id {fill.fill_id} has negative quantity.")
            continue
        notional = fill.quantity * fill.fill_price
        if fill.side == "buy":
            cash -= notional + fill.fee + fill.slippage
            positions[fill.symbol] = positions.get(fill.symbol, Decimal("0")) + fill.quantity
        elif fill.side == "sell":
            held = positions.get(fill.symbol, Decimal("0"))
            if held < fill.quantity:
                errors.append(f"fills.csv fill_id {fill.fill_id} sells more than held.")
                continue
            cash += notional - fill.fee - fill.slippage
            remaining = held - fill.quantity
            if remaining == 0:
                positions.pop(fill.symbol, None)
            else:
                positions[fill.symbol] = remaining

    if expected_final_cash is not None and cash != expected_final_cash:
        errors.append("Replayed final cash does not match account_summary.json final_cash.")
    if positions != expected_positions:
        errors.append("Replayed final positions do not match account_summary.json final_positions.")
    return cash, positions


def _validate_equity(
    equity_curve: list[EquityArtifact],
    account_summary: dict[str, Any],
    replay_cash: Decimal | None,
    replay_positions: dict[str, Decimal],
    errors: list[str],
) -> Decimal | None:
    expected_final_equity = _parse_summary_decimal(account_summary, "final_equity", errors)
    final_row = equity_curve[-1]
    if expected_final_equity is not None and final_row.equity != expected_final_equity:
        errors.append("Final equity_curve.csv equity does not match account_summary.json final_equity.")

    row_equity = final_row.cash + final_row.position_value
    if final_row.equity != row_equity:
        errors.append("Final equity_curve.csv equity does not equal cash plus position_value.")

    if final_row.position_value != final_row.position_quantity * final_row.last_price:
        errors.append("Final equity_curve.csv position_value does not equal position_quantity times last_price.")

    replayed_equity: Decimal | None = None
    if replay_cash is not None:
        replayed_equity = replay_cash
        for symbol, quantity in sorted(replay_positions.items()):
            if symbol != final_row.symbol:
                errors.append(f"No final equity_curve.csv price for replayed position symbol {symbol}.")
                continue
            replayed_equity += quantity * final_row.last_price
        if expected_final_equity is not None and replayed_equity != expected_final_equity:
            errors.append("Replayed final equity does not match account_summary.json final_equity.")
    return replayed_equity


def _validate_trades_to_fills(trades: list[dict[str, str]], fills: list[FillArtifact], errors: list[str]) -> None:
    if len(trades) != len(fills):
        errors.append("trades.csv row count does not match fills.csv row count.")
        return

    fills_by_id = {fill.fill_id: fill for fill in fills}
    for index, trade in enumerate(trades, start=2):
        fill_id = trade.get("fill_id", "")
        fill = fills_by_id.get(fill_id)
        if fill is None:
            errors.append(f"trades.csv row {index} references unknown fill_id {fill_id}.")
            continue
        checks = {
            "timestamp": fill.timestamp.isoformat(),
            "symbol": fill.symbol,
            "side": fill.side,
            "quantity": simulate.format_decimal(fill.quantity),
            "price": simulate.format_decimal(fill.fill_price),
            "order_id": fill.order_id,
        }
        for column, expected in checks.items():
            if trade.get(column, "") != expected:
                errors.append(f"trades.csv row {index} column {column} does not match fill {fill.fill_id}.")


def _validate_audit_summary(
    audit_summary: dict[str, Any],
    errors: list[str],
    gaps_or_limitations: list[str],
) -> None:
    status = audit_summary.get("audit_status")
    if status not in {"PASS", "INCONCLUSIVE"}:
        errors.append("audit_summary.json audit_status must be PASS or INCONCLUSIVE.")
    issues = audit_summary.get("issues", [])
    if not isinstance(issues, list):
        errors.append("audit_summary.json issues must be a list.")
        return
    gap_files: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            errors.append("audit_summary.json issue entries must be objects.")
            continue
        severity = issue.get("severity")
        file_name = issue.get("file")
        if severity == audit.ERROR:
            errors.append("audit_summary.json contains an audit error.")
        elif severity == audit.GAP:
            if file_name not in {"benchmark.csv", "factor_exposure.csv"}:
                errors.append(f"audit_summary.json contains non-optional gap for {file_name}.")
            else:
                gap_files.append(str(file_name))
    if gap_files:
        gaps_or_limitations.append(f"optional audit gaps: {', '.join(sorted(set(gap_files)))}")


def _validate_counts(
    account_summary: dict[str, Any],
    orders: list[OrderArtifact],
    fills: list[FillArtifact],
    trades: list[dict[str, str]],
    errors: list[str],
) -> None:
    expected_order_count = _parse_summary_int(account_summary, "order_count", errors)
    expected_fill_count = _parse_summary_int(account_summary, "fill_count", errors)
    expected_trade_count = _parse_summary_int(account_summary, "trade_count", errors)
    if expected_order_count is not None and expected_order_count != len(orders):
        errors.append("account_summary.json order_count does not match orders.csv row count.")
    if expected_fill_count is not None and expected_fill_count != len(fills):
        errors.append("account_summary.json fill_count does not match fills.csv row count.")
    if expected_trade_count is not None and expected_trade_count != len(trades):
        errors.append("account_summary.json trade_count does not match trades.csv row count.")


def _require_columns(file_name: str, row: dict[str, str], required: list[str]) -> None:
    missing = [column for column in required if column not in row]
    if missing:
        raise RunValidationError(f"{file_name} missing required columns: {', '.join(missing)}.")


def _required_text(file_name: str, row_number: int, row: dict[str, str], column: str) -> str:
    value = row.get(column, "")
    if audit._is_blank(value):
        raise RunValidationError(f"{file_name} row {row_number} missing {column}.")
    return value.strip()


def _parse_datetime(file_name: str, row_number: int, value: str) -> datetime:
    parsed = audit._parse_timestamp(value, "datetime")
    if not isinstance(parsed, datetime):
        raise RunValidationError(f"{file_name} row {row_number} has invalid ISO datetime.")
    return parsed


def _parse_side(file_name: str, row_number: int, value: str) -> str:
    side = value.strip()
    if side not in {"buy", "sell"}:
        raise RunValidationError(f"{file_name} row {row_number} has invalid side {side}.")
    return side


def _parse_decimal(file_name: str, row_number: int, column: str, value: str) -> Decimal:
    parsed = audit._parse_decimal(value)
    if parsed is None:
        raise RunValidationError(f"{file_name} row {row_number} has invalid decimal {column}.")
    return parsed


def _parse_summary_decimal(account_summary: dict[str, Any], key: str, errors: list[str]) -> Decimal | None:
    value = account_summary.get(key)
    if not isinstance(value, str):
        errors.append(f"account_summary.json {key} must be a decimal string.")
        return None
    parsed = audit._parse_decimal(value)
    if parsed is None:
        errors.append(f"account_summary.json {key} is not a finite decimal.")
        return None
    return parsed


def _parse_summary_int(account_summary: dict[str, Any], key: str, errors: list[str]) -> int | None:
    value = account_summary.get(key)
    if not isinstance(value, int):
        errors.append(f"account_summary.json {key} must be an integer.")
        return None
    return value


def _parse_positions(value: Any, errors: list[str]) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        errors.append("account_summary.json final_positions must be an object.")
        return {}
    positions: dict[str, Decimal] = {}
    for symbol, raw_quantity in value.items():
        if not isinstance(symbol, str) or not isinstance(raw_quantity, str):
            errors.append("account_summary.json final_positions must map symbols to decimal strings.")
            continue
        quantity = audit._parse_decimal(raw_quantity)
        if quantity is None:
            errors.append(f"account_summary.json final_positions has invalid quantity for {symbol}.")
            continue
        if quantity != 0:
            positions[symbol] = quantity
    return dict(sorted(positions.items()))


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _format_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    return simulate.format_decimal(value)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
