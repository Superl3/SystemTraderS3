"""Factor-aware reporting for exported simulation artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from system_trading_s3 import audit


PASS = "PASS"
INCONCLUSIVE = "INCONCLUSIVE"
FAIL = "FAIL"
REPORT_SCHEMA_VERSION = "mvp10.factor_report.v1"
REPORT_FILE_NAME = "factor_report.json"


class FactorReportError(Exception):
    """Raised when required run artifact inputs are invalid."""


@dataclass(frozen=True)
class FillRow:
    timestamp: datetime
    symbol: str
    side: str
    quantity: Decimal


@dataclass(frozen=True)
class FactorRow:
    timestamp: datetime
    symbol: str
    factor_name: str
    factor_value: Decimal


@dataclass(frozen=True)
class EquityPositionRow:
    timestamp: datetime
    positions: dict[str, Decimal]


@dataclass(frozen=True)
class FactorReportResult:
    status: str
    payload: dict[str, Any]
    errors: list[str]


def calculate_factor_report(run_artifact_dir: Path | str, dataset_dir: Path | str | None = None) -> FactorReportResult:
    run_dir = Path(run_artifact_dir)
    errors: list[str] = []
    gaps: list[str] = []

    try:
        manifest = _load_json(run_dir / "run_manifest.json")
        fills = _load_fills(run_dir / "fills.csv")
        equity_positions = _load_equity_positions(run_dir / "equity_curve.csv")
    except FactorReportError as exc:
        return FactorReportResult(status=FAIL, payload=_failed_payload([str(exc)]), errors=[str(exc)])

    resolved_dataset_dir = _resolve_dataset_dir(run_dir, manifest, dataset_dir)
    if resolved_dataset_dir is None:
        gaps.append("dataset_dir unavailable; factor exposure report cannot load factors.csv.")
        return FactorReportResult(status=INCONCLUSIVE, payload=_inconclusive_payload(manifest, gaps), errors=[])

    factor_path = resolved_dataset_dir / "factors.csv"
    if not factor_path.is_file():
        gaps.append("factors.csv missing; factor exposure report is unavailable.")
        return FactorReportResult(status=INCONCLUSIVE, payload=_inconclusive_payload(manifest, gaps, resolved_dataset_dir), errors=[])

    try:
        factor_rows = _load_factor_rows(factor_path)
    except FactorReportError as exc:
        return FactorReportResult(status=FAIL, payload=_failed_payload([str(exc)]), errors=[str(exc)])

    buy_fills = [fill for fill in fills if fill.side == "buy"]
    if not buy_fills:
        gaps.append("no buy fills are available for factor exposure reporting.")

    factor_names = sorted({row.factor_name for row in factor_rows})
    exposure = _factor_exposure_summary(buy_fills, factor_rows, factor_names, gaps)
    holding_exposure = _holding_factor_exposure_summary(equity_positions, factor_rows, factor_names, gaps)

    status = INCONCLUSIVE if gaps else PASS
    payload: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "run_id": manifest.get("run_id", ""),
        "strategy_name": manifest.get("strategy_name", ""),
        "dataset_dir": str(resolved_dataset_dir),
        "factor_file": str(factor_path),
        "fill_count": len(fills),
        "buy_fill_count": len(buy_fills),
        "factor_names": factor_names,
        "factor_exposure": exposure,
        "holding_factor_exposure": holding_exposure,
        "gaps": gaps,
        "interpretation": "Factor report checks intended factor exposure alignment and holding-period factor exposure only; it is not a profitability claim.",
    }
    return FactorReportResult(status=status, payload=payload, errors=[])


def write_factor_report(run_artifact_dir: Path | str, dataset_dir: Path | str | None = None) -> FactorReportResult:
    run_dir = Path(run_artifact_dir)
    result = calculate_factor_report(run_dir, dataset_dir)
    if result.status in {PASS, INCONCLUSIVE}:
        _write_json(run_dir / REPORT_FILE_NAME, result.payload)
    return result


def format_factor_report_result(result: FactorReportResult, report_path: Path | None = None) -> str:
    payload = result.payload
    lines = [f"FACTOR REPORT STATUS: {result.status}"]
    if report_path is not None:
        lines.append(f"FACTOR REPORT FILE: {report_path}")
    for key, label in [
        ("run_id", "RUN ID"),
        ("strategy_name", "STRATEGY"),
        ("buy_fill_count", "BUY FILL COUNT"),
    ]:
        if key in payload:
            lines.append(f"{label}: {payload[key]}")
    exposure = payload.get("factor_exposure")
    if isinstance(exposure, dict):
        lines.append("BUY-SIDE FACTOR EXPOSURE:")
        for factor_name in sorted(exposure):
            item = exposure[factor_name]
            lines.append(
                "- "
                f"{factor_name}: average_buy_factor_value={item.get('average_buy_factor_value')}, "
                f"average_buy_factor_rank={item.get('average_buy_factor_rank')}, "
                f"top_rank_buy_count={item.get('top_rank_buy_count')}, "
                f"buy_fills_with_factor={item.get('buy_fills_with_factor')}"
            )
    holding_exposure = payload.get("holding_factor_exposure")
    if isinstance(holding_exposure, dict):
        lines.append("HOLDING FACTOR EXPOSURE:")
        for factor_name in sorted(holding_exposure):
            item = holding_exposure[factor_name]
            lines.append(
                "- "
                f"{factor_name}: average_held_factor_value={item.get('average_held_factor_value')}, "
                f"held_observation_count={item.get('held_observation_count')}, "
                f"missing_held_factor_count={item.get('missing_held_factor_count')}"
            )
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
    parser = argparse.ArgumentParser(description="Create a factor-aware report for exported simulation artifacts.")
    parser.add_argument("run_artifact_dir", type=Path, help="Directory containing exported simulation artifacts.")
    parser.add_argument("--dataset-dir", type=Path, help="Override dataset directory containing factors.csv.")
    args = parser.parse_args(argv)

    if not args.run_artifact_dir.exists() or not args.run_artifact_dir.is_dir():
        print(f"run_artifact_dir must be an existing directory: {args.run_artifact_dir}", file=sys.stderr)
        return 2

    try:
        result = write_factor_report(args.run_artifact_dir, args.dataset_dir)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary.
        print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    print(format_factor_report_result(result, args.run_artifact_dir / REPORT_FILE_NAME))
    return 0 if result.status in {PASS, INCONCLUSIVE} else 1


def _factor_exposure_summary(
    buy_fills: list[FillRow],
    factor_rows: list[FactorRow],
    factor_names: list[str],
    gaps: list[str],
) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for factor_name in factor_names:
        values: list[Decimal] = []
        ranks: list[int] = []
        top_rank_buy_count = 0
        missing_factor_count = 0
        for fill in buy_fills:
            snapshot = _factor_snapshot_at(factor_rows, fill.timestamp, factor_name)
            factor_value = snapshot.get(fill.symbol)
            if factor_value is None:
                missing_factor_count += 1
                continue
            values.append(factor_value)
            ranked = sorted(snapshot.items(), key=lambda item: (-item[1], item[0]))
            rank = _rank_for_symbol(ranked, fill.symbol)
            if rank is not None:
                ranks.append(rank)
            if rank == 1:
                top_rank_buy_count += 1
        if missing_factor_count:
            gaps.append(f"{factor_name} missing for {missing_factor_count} buy fill(s).")
        summary[factor_name] = {
            "buy_fills_with_factor": len(values),
            "average_buy_factor_value": _format_optional_decimal(_average(values)),
            "average_buy_factor_rank": _format_optional_decimal(_average([Decimal(rank) for rank in ranks])),
            "best_buy_factor_rank": min(ranks) if ranks else "UNAVAILABLE",
            "worst_buy_factor_rank": max(ranks) if ranks else "UNAVAILABLE",
            "top_rank_buy_count": top_rank_buy_count,
            "missing_factor_count": missing_factor_count,
        }
    return summary


def _holding_factor_exposure_summary(
    equity_positions: list[EquityPositionRow],
    factor_rows: list[FactorRow],
    factor_names: list[str],
    gaps: list[str],
) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for factor_name in factor_names:
        held_values: list[Decimal] = []
        missing_held_factor_count = 0
        held_row_count = 0
        for row in equity_positions:
            positions = {symbol: quantity for symbol, quantity in row.positions.items() if quantity > 0}
            if not positions:
                continue
            held_row_count += 1
            snapshot = _factor_snapshot_at(factor_rows, row.timestamp, factor_name)
            weighted_total = Decimal("0")
            quantity_total = Decimal("0")
            for symbol, quantity in positions.items():
                factor_value = snapshot.get(symbol)
                if factor_value is None:
                    missing_held_factor_count += 1
                    continue
                weighted_total += factor_value * quantity
                quantity_total += quantity
            if quantity_total > 0:
                held_values.append(weighted_total / quantity_total)
        if held_row_count == 0:
            gaps.append(f"{factor_name} holding exposure unavailable because no held positions are present.")
        if missing_held_factor_count:
            gaps.append(f"{factor_name} missing for {missing_held_factor_count} held position observation(s).")
        summary[factor_name] = {
            "held_observation_count": len(held_values),
            "average_held_factor_value": _format_optional_decimal(_average(held_values)),
            "missing_held_factor_count": missing_held_factor_count,
        }
    return summary


def _factor_snapshot_at(factor_rows: list[FactorRow], timestamp: datetime, factor_name: str) -> dict[str, Decimal]:
    snapshot: dict[str, Decimal] = {}
    for row in factor_rows:
        if row.timestamp > timestamp:
            break
        if row.factor_name == factor_name:
            snapshot[row.symbol] = row.factor_value
    return snapshot


def _rank_for_symbol(ranked: list[tuple[str, Decimal]], symbol: str) -> int | None:
    for index, (candidate, _) in enumerate(ranked, start=1):
        if candidate == symbol:
            return index
    return None


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


def _load_fills(path: Path) -> list[FillRow]:
    if not path.is_file():
        raise FactorReportError("fills.csv is missing.")
    rows = _load_csv_dicts(path)
    fills: list[FillRow] = []
    for index, row in enumerate(rows, start=2):
        timestamp = _parse_datetime("fills.csv", index, "timestamp", row.get("timestamp", ""))
        symbol = _required_text("fills.csv", index, "symbol", row.get("symbol", ""))
        side = _required_text("fills.csv", index, "side", row.get("side", ""))
        if side not in {"buy", "sell"}:
            raise FactorReportError(f"fills.csv row {index} has invalid side.")
        quantity = _parse_decimal("fills.csv", index, "quantity", row.get("quantity", ""))
        fills.append(FillRow(timestamp=timestamp, symbol=symbol, side=side, quantity=quantity))
    return fills


def _load_equity_positions(path: Path) -> list[EquityPositionRow]:
    if not path.is_file():
        raise FactorReportError("equity_curve.csv is missing.")
    rows = _load_csv_dicts(path)
    positions: list[EquityPositionRow] = []
    for index, row in enumerate(rows, start=2):
        timestamp = _parse_equity_date("equity_curve.csv", index, "timestamp", row.get("timestamp", ""))
        positions.append(
            EquityPositionRow(
                timestamp=timestamp,
                positions=_parse_position_quantities("equity_curve.csv", index, row.get("position_quantities", "")),
            )
        )
    return positions


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
        raise FactorReportError("factors.csv must contain at least one row.")
    return sorted(factors, key=lambda row: (row.timestamp, row.symbol, row.factor_name))


def _load_csv_dicts(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except csv.Error as exc:
        raise FactorReportError(f"{path.name} is not readable CSV: {exc}") from exc
    except OSError as exc:
        raise FactorReportError(f"{path.name} cannot be read: {exc}") from exc


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FactorReportError(f"{path.name} is missing.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactorReportError(f"{path.name} cannot be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise FactorReportError(f"{path.name} must contain a JSON object.")
    return payload


def _parse_datetime(file_name: str, row_number: int, column: str, value: str) -> datetime:
    parsed = audit._parse_timestamp(value, "datetime")
    if not isinstance(parsed, datetime):
        raise FactorReportError(f"{file_name} row {row_number} has invalid {column}.")
    return parsed


def _parse_equity_date(file_name: str, row_number: int, column: str, value: str) -> datetime:
    parsed = audit._parse_timestamp(value, "date")
    if parsed is None or isinstance(parsed, datetime):
        raise FactorReportError(f"{file_name} row {row_number} has invalid {column}.")
    return datetime.combine(parsed, time.max)


def _parse_decimal(file_name: str, row_number: int, column: str, value: str) -> Decimal:
    parsed = audit._parse_decimal(value)
    if parsed is None:
        raise FactorReportError(f"{file_name} row {row_number} has invalid {column}.")
    return parsed


def _parse_position_quantities(file_name: str, row_number: int, value: str) -> dict[str, Decimal]:
    if audit._is_blank(value):
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise FactorReportError(f"{file_name} row {row_number} has invalid position_quantities JSON.") from exc
    if not isinstance(payload, dict):
        raise FactorReportError(f"{file_name} row {row_number} position_quantities must be a JSON object.")
    positions: dict[str, Decimal] = {}
    for symbol, raw_quantity in payload.items():
        if not isinstance(symbol, str) or not isinstance(raw_quantity, str):
            raise FactorReportError(f"{file_name} row {row_number} position_quantities must map symbols to decimal strings.")
        quantity = audit._parse_decimal(raw_quantity)
        if quantity is None:
            raise FactorReportError(f"{file_name} row {row_number} has invalid position quantity for {symbol}.")
        if quantity != 0:
            positions[symbol] = quantity
    return dict(sorted(positions.items()))


def _required_text(file_name: str, row_number: int, column: str, value: str) -> str:
    text = value.strip()
    if not text:
        raise FactorReportError(f"{file_name} row {row_number} has missing {column}.")
    return text


def _average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _format_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    return format(value.quantize(Decimal("0.000001")), "f")


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
        "factor_exposure": {},
        "gaps": gaps,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
