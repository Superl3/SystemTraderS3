"""Deterministic benchmark-relative loss classification for exported runs."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from system_trading_s3 import audit


PASS = "PASS"
INCONCLUSIVE = "INCONCLUSIVE"
FAIL = "FAIL"
REPORT_SCHEMA_VERSION = "mvp11.loss_classification.v1"
REPORT_FILE_NAME = "loss_classification.json"
RETURN_QUANT = Decimal("0.000001")
DEFAULT_ACTIVE_RETURN_TOLERANCE = Decimal("0.0001")


class LossClassificationError(Exception):
    """Raised when exported artifacts cannot support loss classification."""


@dataclass(frozen=True)
class EquityRow:
    timestamp: str
    equity: Decimal
    benchmark_equity: Decimal | None


@dataclass(frozen=True)
class PeriodClassification:
    start_timestamp: str
    end_timestamp: str
    strategy_return: Decimal
    benchmark_return: Decimal | None
    active_return: Decimal | None
    classification: str
    explanation: str


@dataclass(frozen=True)
class LossClassificationResult:
    status: str
    payload: dict[str, Any]
    errors: list[str]


def classify_losses(
    run_artifact_dir: Path | str,
    active_return_tolerance: Decimal = DEFAULT_ACTIVE_RETURN_TOLERANCE,
) -> LossClassificationResult:
    run_dir = Path(run_artifact_dir)
    gaps: list[str] = []
    try:
        equity_rows = _load_equity_rows(run_dir / "equity_curve.csv")
    except LossClassificationError as exc:
        return LossClassificationResult(status=FAIL, payload=_failed_payload([str(exc)]), errors=[str(exc)])

    if len(equity_rows) < 2:
        gaps.append("loss classification unavailable because equity_curve has fewer than two rows.")
        return LossClassificationResult(status=INCONCLUSIVE, payload=_inconclusive_payload(gaps), errors=[])

    periods = _classify_periods(equity_rows, active_return_tolerance, gaps)
    factor_context = _factor_context(run_dir, gaps)
    summary = _summary(periods)
    status = INCONCLUSIVE if gaps else PASS
    payload: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "source_files": ["equity_curve.csv"],
        "active_return_tolerance": _format_decimal(active_return_tolerance),
        "summary": summary,
        "periods": [_period_payload(period) for period in periods],
        "factor_context": factor_context,
        "gaps": gaps,
        "interpretation": "Loss classification is deterministic benchmark-relative triage; it is not a profitability claim.",
    }
    return LossClassificationResult(status=status, payload=payload, errors=[])


def write_loss_classification(
    run_artifact_dir: Path | str,
    active_return_tolerance: Decimal = DEFAULT_ACTIVE_RETURN_TOLERANCE,
) -> LossClassificationResult:
    run_dir = Path(run_artifact_dir)
    result = classify_losses(run_dir, active_return_tolerance)
    if result.status in {PASS, INCONCLUSIVE}:
        _write_json(run_dir / REPORT_FILE_NAME, result.payload)
    return result


def format_loss_classification_result(result: LossClassificationResult, report_path: Path | None = None) -> str:
    lines = [f"LOSS CLASSIFICATION STATUS: {result.status}"]
    if report_path is not None:
        lines.append(f"LOSS CLASSIFICATION FILE: {report_path}")
    summary = result.payload.get("summary")
    if isinstance(summary, dict):
        lines.append(f"PERIOD COUNT: {summary.get('period_count', 0)}")
        lines.append(f"LOSS PERIOD COUNT: {summary.get('loss_period_count', 0)}")
        counts = summary.get("classification_counts", {})
        if isinstance(counts, dict):
            lines.append("CLASSIFICATIONS:")
            for key in sorted(counts):
                lines.append(f"- {key}: {counts[key]}")
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
    parser = argparse.ArgumentParser(description="Classify benchmark-relative losses for exported simulation artifacts.")
    parser.add_argument("run_artifact_dir", type=Path, help="Directory containing exported run artifacts.")
    parser.add_argument(
        "--active-return-tolerance",
        default=str(DEFAULT_ACTIVE_RETURN_TOLERANCE),
        help="Decimal active-return tolerance before marking excess relative loss. Defaults to 0.0001.",
    )
    args = parser.parse_args(argv)

    if not args.run_artifact_dir.exists() or not args.run_artifact_dir.is_dir():
        print(f"run_artifact_dir must be an existing directory: {args.run_artifact_dir}", file=sys.stderr)
        return 2
    parsed_tolerance = audit._parse_decimal(args.active_return_tolerance)
    if parsed_tolerance is None or parsed_tolerance < 0:
        print("active-return-tolerance must be a finite nonnegative decimal.", file=sys.stderr)
        return 2

    try:
        result = write_loss_classification(args.run_artifact_dir, parsed_tolerance)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary.
        print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    print(format_loss_classification_result(result, args.run_artifact_dir / REPORT_FILE_NAME))
    return 0 if result.status in {PASS, INCONCLUSIVE} else 1


def _classify_periods(
    equity_rows: list[EquityRow],
    active_return_tolerance: Decimal,
    gaps: list[str],
) -> list[PeriodClassification]:
    periods: list[PeriodClassification] = []
    missing_benchmark_count = 0
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
        classification, explanation = _classification_for(strategy_return, benchmark_return, active_return, active_return_tolerance)
        periods.append(
            PeriodClassification(
                start_timestamp=previous.timestamp,
                end_timestamp=current.timestamp,
                strategy_return=strategy_return,
                benchmark_return=benchmark_return,
                active_return=active_return,
                classification=classification,
                explanation=explanation,
            )
        )
    if missing_benchmark_count:
        gaps.append(f"benchmark_equity missing for {missing_benchmark_count} period(s); benchmark-relative loss classification is limited.")
    return periods


def _classification_for(
    strategy_return: Decimal,
    benchmark_return: Decimal | None,
    active_return: Decimal | None,
    active_return_tolerance: Decimal,
) -> tuple[str, str]:
    if strategy_return >= 0:
        return "NO_LOSS", "Strategy equity did not decline during this period."
    if benchmark_return is None or active_return is None:
        return "UNEXPLAINED_LOSS_DATA_GAP", "Strategy loss cannot be compared to benchmark because benchmark equity is missing."
    if benchmark_return < 0 and active_return >= -active_return_tolerance:
        return "BENCHMARK_EXPLAINED_LOSS", "Strategy loss is within tolerance of a negative benchmark period."
    if active_return < -active_return_tolerance:
        return "EXCESS_RELATIVE_LOSS", "Strategy loss exceeded benchmark-relative tolerance."
    return "STRATEGY_SPECIFIC_LOSS", "Strategy lost money while benchmark did not explain the move."


def _summary(periods: list[PeriodClassification]) -> dict[str, object]:
    counts: dict[str, int] = {}
    loss_periods = [period for period in periods if period.strategy_return < 0]
    for period in periods:
        counts[period.classification] = counts.get(period.classification, 0) + 1
    worst = min(periods, key=lambda period: period.strategy_return) if periods else None
    return {
        "period_count": len(periods),
        "loss_period_count": len(loss_periods),
        "classification_counts": dict(sorted(counts.items())),
        "worst_period": None if worst is None else _period_payload(worst),
    }


def _factor_context(run_dir: Path, gaps: list[str]) -> dict[str, object]:
    path = run_dir / "factor_report.json"
    attribution_path = run_dir / "factor_attribution.json"
    attribution_summary = _factor_attribution_context(attribution_path)
    if not path.is_file():
        gaps.append("factor_report.json missing; factor context unavailable for loss classification.")
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "holding_factor_exposure": {},
            "factor_attribution": attribution_summary,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        gaps.append("factor_report.json unreadable; factor context unavailable for loss classification.")
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "holding_factor_exposure": {},
            "factor_attribution": attribution_summary,
        }
    if not isinstance(payload, dict):
        gaps.append("factor_report.json invalid; factor context unavailable for loss classification.")
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "holding_factor_exposure": {},
            "factor_attribution": attribution_summary,
        }
    holding = payload.get("holding_factor_exposure", {})
    if not isinstance(holding, dict):
        holding = {}
    if not holding:
        gaps.append("holding_factor_exposure missing; factor-relative loss classification is unavailable.")
    return {
        "available": bool(holding),
        "status": payload.get("status", "UNAVAILABLE"),
        "holding_factor_exposure": holding,
        "factor_attribution": attribution_summary,
    }


def _factor_attribution_context(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"available": False, "status": "UNAVAILABLE", "factor_summary": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "status": "UNAVAILABLE", "factor_summary": {}}
    if not isinstance(payload, dict):
        return {"available": False, "status": "UNAVAILABLE", "factor_summary": {}}
    summary = payload.get("summary", {})
    factor_summary: object = {}
    if isinstance(summary, dict) and isinstance(summary.get("factor_summary"), dict):
        factor_summary = summary["factor_summary"]
    return {
        "available": bool(factor_summary),
        "status": payload.get("status", "UNAVAILABLE"),
        "factor_summary": factor_summary,
    }


def _load_equity_rows(path: Path) -> list[EquityRow]:
    if not path.is_file():
        raise LossClassificationError("equity_curve.csv is missing.")
    rows = _load_csv_dicts(path)
    equity_rows: list[EquityRow] = []
    for index, row in enumerate(rows, start=2):
        timestamp = row.get("timestamp", "")
        if audit._parse_timestamp(timestamp, "date") is None:
            raise LossClassificationError(f"equity_curve.csv row {index} has invalid date timestamp.")
        equity_rows.append(
            EquityRow(
                timestamp=timestamp,
                equity=_parse_decimal("equity_curve.csv", index, "equity", row.get("equity", "")),
                benchmark_equity=_parse_optional_decimal("equity_curve.csv", index, "benchmark_equity", row.get("benchmark_equity")),
            )
        )
    if not equity_rows:
        raise LossClassificationError("equity_curve.csv must contain at least one row.")
    return equity_rows


def _load_csv_dicts(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise LossClassificationError(f"{path.name} is missing a header row.")
            return [{key.strip(): value for key, value in row.items()} for row in reader]
    except csv.Error as exc:
        raise LossClassificationError(f"{path.name} could not be read as CSV: {exc}") from exc


def _period_return(previous: Decimal, current: Decimal, label: str) -> Decimal:
    if previous <= 0:
        raise LossClassificationError(f"{label} return unavailable because previous value is nonpositive.")
    return (current - previous) / previous


def _parse_decimal(file_name: str, row_number: int, column: str, value: str) -> Decimal:
    parsed = audit._parse_decimal(value)
    if parsed is None:
        raise LossClassificationError(f"{file_name} row {row_number} has invalid decimal {column}.")
    return parsed


def _parse_optional_decimal(file_name: str, row_number: int, column: str, value: str | None) -> Decimal | None:
    if value is None or audit._is_blank(value):
        return None
    return _parse_decimal(file_name, row_number, column, value)


def _period_payload(period: PeriodClassification) -> dict[str, str]:
    return {
        "start_timestamp": period.start_timestamp,
        "end_timestamp": period.end_timestamp,
        "strategy_return": _format_decimal(period.strategy_return),
        "benchmark_return": "UNAVAILABLE" if period.benchmark_return is None else _format_decimal(period.benchmark_return),
        "active_return": "UNAVAILABLE" if period.active_return is None else _format_decimal(period.active_return),
        "classification": period.classification,
        "explanation": period.explanation,
    }


def _inconclusive_payload(gaps: list[str]) -> dict[str, object]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": INCONCLUSIVE,
        "summary": {"period_count": 0, "loss_period_count": 0, "classification_counts": {}, "worst_period": None},
        "periods": [],
        "factor_context": {
            "available": False,
            "status": "UNAVAILABLE",
            "holding_factor_exposure": {},
            "factor_attribution": {"available": False, "status": "UNAVAILABLE", "factor_summary": {}},
        },
        "gaps": gaps,
    }


def _failed_payload(errors: list[str]) -> dict[str, object]:
    return {"schema_version": REPORT_SCHEMA_VERSION, "status": FAIL, "errors": errors, "gaps": []}


def _format_decimal(value: Decimal) -> str:
    if value == 0:
        value = Decimal("0")
    return format(value.quantize(RETURN_QUANT, rounding=ROUND_HALF_UP), "f")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
