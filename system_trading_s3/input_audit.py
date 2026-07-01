"""Read-only audit for simulator input datasets."""

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


PASS = audit.PASS
INCONCLUSIVE = audit.INCONCLUSIVE
FAIL = audit.FAIL

ERROR = audit.ERROR
GAP = audit.GAP


@dataclass(frozen=True)
class InputAuditResult:
    status: str
    dataset: str
    files: list[audit.FileSummary]
    issues: list[audit.Issue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dataset": self.dataset,
            "files": [summary.to_dict() for summary in self.files],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class PriceRow:
    file_name: str
    row_number: int
    timestamp: datetime
    symbol: str
    price: Decimal


@dataclass(frozen=True)
class FactorRow:
    row_number: int
    timestamp: datetime
    symbol: str
    factor_name: str
    factor_value: Decimal


def audit_input_dataset(dataset_dir: Path | str) -> InputAuditResult:
    dataset_path = Path(dataset_dir)
    issues: list[audit.Issue] = []
    summaries: list[audit.FileSummary] = []

    market_rows = _audit_market_inputs(dataset_path, summaries, issues)
    _audit_benchmark_input(dataset_path, summaries, issues)
    _audit_factor_input(dataset_path, summaries, issues)
    _audit_manifest_input(dataset_path, summaries, issues)

    if market_rows and len({row.timestamp for row in market_rows}) < 2:
        issues.append(
            _issue(
                "market_inputs",
                None,
                "timestamp",
                "market_input_too_short",
                ERROR,
                "Market input must contain at least two distinct timestamps.",
            )
        )

    return InputAuditResult(
        status=audit._compose_status(issues),
        dataset=str(dataset_path),
        files=summaries,
        issues=issues,
    )


def format_human(result: InputAuditResult) -> str:
    lines = [f"STATUS: {result.status}", f"DATASET: {result.dataset}", "FILES:"]
    for summary in result.files:
        presence = "present" if summary.present else "missing"
        lines.append(f"- {summary.file}: {presence} rows={summary.rows}")

    lines.append("ISSUES:")
    if not result.issues:
        lines.append("- none")
    else:
        for issue in result.issues:
            row = "-" if issue.row is None else str(issue.row)
            column = "-" if issue.column is None else issue.column
            value = "" if issue.value is None else f" value={issue.value!r}"
            lines.append(
                f"- [{issue.severity.upper()}] {issue.file} row={row} column={column} "
                f"check={issue.check}:{value} {issue.message}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a simulator input dataset.")
    parser.add_argument("dataset_dir", type=Path, help="Directory containing market_prices.csv or *_prices.csv inputs.")
    parser.add_argument("--strict", action="store_true", help="Treat INCONCLUSIVE as exit code 1.")
    parser.add_argument("--json", action="store_true", help="Emit deterministic JSON output.")
    args = parser.parse_args(argv)

    if not args.dataset_dir.exists() or not args.dataset_dir.is_dir():
        print(f"dataset_dir must be an existing directory: {args.dataset_dir}", file=sys.stderr)
        return 2

    try:
        result = audit_input_dataset(args.dataset_dir)
    except Exception as exc:  # pragma: no cover - defensive boundary.
        if args.json:
            print(json.dumps(_failed_payload(args.dataset_dir, str(exc)), indent=2))
        else:
            print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_human(result))
    return audit.exit_code_for_status(result.status, args.strict)


def _audit_market_inputs(
    root: Path,
    summaries: list[audit.FileSummary],
    issues: list[audit.Issue],
) -> list[PriceRow]:
    market_path = root / "market_prices.csv"
    if market_path.exists():
        if not market_path.is_file():
            summaries.append(audit.FileSummary("market_prices.csv", True, 0))
            issues.append(_issue("market_prices.csv", None, None, "market_input_not_file", ERROR, "market_prices.csv must be a file."))
            return []
        rows = _audit_price_file(market_path, "market_prices.csv", issues, require_one_symbol=False)
        summaries.append(audit.FileSummary("market_prices.csv", True, len(rows)))
        _audit_price_duplicates(rows, issues)
        _audit_timestamp_comparability(rows, issues, "market_inputs")
        return rows

    price_files = sorted(
        path
        for path in root.glob("*_prices.csv")
        if path.name not in {"benchmark_prices.csv", "market_prices.csv"} and path.is_file()
    )
    if not price_files:
        summaries.append(audit.FileSummary("market_prices.csv or *_prices.csv", False, 0))
        issues.append(
            _issue(
                "market_inputs",
                None,
                None,
                "required_market_input_missing",
                ERROR,
                "Market input requires market_prices.csv or at least one sorted *_prices.csv file.",
            )
        )
        return []

    rows: list[PriceRow] = []
    for price_file in price_files:
        file_rows = _audit_price_file(price_file, price_file.name, issues, require_one_symbol=True)
        summaries.append(audit.FileSummary(price_file.name, True, len(file_rows)))
        rows.extend(file_rows)
    _audit_price_duplicates(rows, issues)
    _audit_timestamp_comparability(rows, issues, "market_inputs")
    return rows


def _audit_benchmark_input(root: Path, summaries: list[audit.FileSummary], issues: list[audit.Issue]) -> None:
    path = root / "benchmark_prices.csv"
    if not path.exists():
        summaries.append(audit.FileSummary("benchmark_prices.csv", False, 0))
        issues.append(
            _issue(
                "benchmark_prices.csv",
                None,
                None,
                "optional_benchmark_prices_missing",
                GAP,
                "Optional benchmark_prices.csv is missing; benchmark-relative metrics will be unavailable.",
            )
        )
        return
    if not path.is_file():
        summaries.append(audit.FileSummary("benchmark_prices.csv", True, 0))
        issues.append(_issue("benchmark_prices.csv", None, None, "benchmark_input_not_file", ERROR, "benchmark_prices.csv must be a file."))
        return
    rows = _audit_price_file(path, "benchmark_prices.csv", issues, require_one_symbol=True)
    summaries.append(audit.FileSummary("benchmark_prices.csv", True, len(rows)))
    _audit_price_duplicates(rows, issues)
    _audit_timestamp_comparability(rows, issues, "benchmark_prices.csv")


def _audit_factor_input(root: Path, summaries: list[audit.FileSummary], issues: list[audit.Issue]) -> None:
    path = root / "factors.csv"
    if not path.exists():
        summaries.append(audit.FileSummary("factors.csv", False, 0))
        issues.append(
            _issue(
                "factors.csv",
                None,
                None,
                "optional_factors_missing",
                GAP,
                "Optional factors.csv is missing; factor-aware strategies and reports may be unavailable.",
            )
        )
        return
    if not path.is_file():
        summaries.append(audit.FileSummary("factors.csv", True, 0))
        issues.append(_issue("factors.csv", None, None, "factor_input_not_file", ERROR, "factors.csv must be a file."))
        return

    rows = _read_csv_rows(path, "factors.csv", ["timestamp", "symbol", "factor_name", "factor_value"], issues)
    factor_rows: list[FactorRow] = []
    seen: set[tuple[datetime, str, str]] = set()
    for row_number, row in rows:
        timestamp = _parse_datetime("factors.csv", row_number, "timestamp", row.get("timestamp", ""), issues)
        symbol = _required_text("factors.csv", row_number, "symbol", row.get("symbol", ""), issues)
        factor_name = _required_text("factors.csv", row_number, "factor_name", row.get("factor_name", ""), issues)
        factor_value = _parse_decimal("factors.csv", row_number, "factor_value", row.get("factor_value", ""), issues)
        if timestamp is None or symbol is None or factor_name is None or factor_value is None:
            continue
        key = (timestamp, symbol, factor_name)
        if key in seen:
            issues.append(
                _issue(
                    "factors.csv",
                    row_number,
                    "timestamp,symbol,factor_name",
                    "duplicate_factor_key",
                    ERROR,
                    "Duplicate factor timestamp/symbol/factor_name.",
                )
            )
        seen.add(key)
        factor_rows.append(FactorRow(row_number, timestamp, symbol, factor_name, factor_value))

    summaries.append(audit.FileSummary("factors.csv", True, len(factor_rows)))
    if not factor_rows:
        issues.append(_issue("factors.csv", None, None, "optional_factors_empty", GAP, "factors.csv contains no factor rows."))
    _audit_factor_timestamp_comparability(factor_rows, issues)


def _audit_manifest_input(root: Path, summaries: list[audit.FileSummary], issues: list[audit.Issue]) -> None:
    path = root / "dataset_manifest.json"
    if not path.exists():
        summaries.append(audit.FileSummary("dataset_manifest.json", False, 0))
        issues.append(
            _issue(
                "dataset_manifest.json",
                None,
                None,
                "optional_dataset_manifest_missing",
                GAP,
                "Optional dataset_manifest.json is missing; dataset provenance is unavailable.",
            )
        )
        return
    if not path.is_file():
        summaries.append(audit.FileSummary("dataset_manifest.json", True, 0))
        issues.append(_issue("dataset_manifest.json", None, None, "manifest_input_not_file", ERROR, "dataset_manifest.json must be a file."))
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        summaries.append(audit.FileSummary("dataset_manifest.json", True, 0))
        issues.append(_issue("dataset_manifest.json", None, None, "manifest_unreadable", ERROR, f"dataset_manifest.json cannot be read: {exc}"))
        return
    summaries.append(audit.FileSummary("dataset_manifest.json", True, 1))
    if not isinstance(payload, dict):
        issues.append(_issue("dataset_manifest.json", None, None, "manifest_not_object", ERROR, "dataset_manifest.json must contain a JSON object."))
        return
    for key in ["schema_version", "dataset_id"]:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            issues.append(
                _issue(
                    "dataset_manifest.json",
                    None,
                    key,
                    "manifest_required_field_missing",
                    GAP,
                    f"dataset_manifest.json should include non-empty {key}.",
                )
            )
    files = payload.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(item, str) and item.strip() for item in files):
        issues.append(
            _issue(
                "dataset_manifest.json",
                None,
                "files",
                "manifest_files_invalid",
                GAP,
                "dataset_manifest.json should include a non-empty files list of strings.",
            )
        )


def _audit_price_file(path: Path, file_name: str, issues: list[audit.Issue], require_one_symbol: bool) -> list[PriceRow]:
    csv_rows = _read_csv_rows(path, file_name, ["timestamp", "symbol", "price"], issues)
    rows: list[PriceRow] = []
    symbols: set[str] = set()
    previous_timestamp: datetime | None = None
    for row_number, row in csv_rows:
        timestamp = _parse_datetime(file_name, row_number, "timestamp", row.get("timestamp", ""), issues)
        symbol = _required_text(file_name, row_number, "symbol", row.get("symbol", ""), issues)
        price = _parse_decimal(file_name, row_number, "price", row.get("price", ""), issues)
        if timestamp is None or symbol is None or price is None:
            continue
        if require_one_symbol and previous_timestamp is not None and timestamp <= previous_timestamp:
            issues.append(_issue(file_name, row_number, "timestamp", "timestamp_order", ERROR, "Timestamps must be strictly increasing."))
        previous_timestamp = timestamp
        symbols.add(symbol)
        rows.append(PriceRow(file_name, row_number, timestamp, symbol, price))
    if not rows:
        issues.append(_issue(file_name, None, None, "required_price_file_empty", ERROR, f"{file_name} must contain at least one price row."))
    if require_one_symbol and len(symbols) > 1:
        issues.append(_issue(file_name, None, "symbol", "price_file_multiple_symbols", ERROR, f"{file_name} must contain exactly one symbol."))
    return rows


def _read_csv_rows(
    path: Path,
    file_name: str,
    required_headers: list[str],
    issues: list[audit.Issue],
) -> list[tuple[int, dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                raw_headers = next(reader)
            except StopIteration:
                issues.append(_issue(file_name, None, None, "csv_empty", ERROR, f"{file_name} is empty."))
                return []
            headers = [header.strip() for header in raw_headers]
            for required in required_headers:
                if required not in headers:
                    issues.append(_issue(file_name, None, required, "required_header_missing", ERROR, f"{file_name} missing required header: {required}."))
            rows: list[tuple[int, dict[str, str]]] = []
            for row_number, raw_row in enumerate(reader, start=2):
                if not raw_row or all(audit._is_blank(cell) for cell in raw_row):
                    continue
                if len(raw_row) > len(headers):
                    issues.append(_issue(file_name, row_number, None, "csv_row_width", ERROR, f"{file_name} row has more fields than headers."))
                    continue
                rows.append((row_number, dict(zip(headers, raw_row + [""] * (len(headers) - len(raw_row))))))
            return rows
    except UnicodeDecodeError as exc:
        issues.append(_issue(file_name, None, None, "decode_error", ERROR, f"{file_name} decode error: {exc}"))
    except csv.Error as exc:
        issues.append(_issue(file_name, None, None, "csv_parse_error", ERROR, f"{file_name} parse error: {exc}"))
    return []


def _parse_datetime(file_name: str, row_number: int, column: str, value: str, issues: list[audit.Issue]) -> datetime | None:
    if audit._is_blank(value):
        issues.append(_issue(file_name, row_number, column, "required_value_missing", ERROR, f"{column} is required."))
        return None
    parsed = audit._parse_timestamp(value, "datetime")
    if not isinstance(parsed, datetime):
        issues.append(_issue(file_name, row_number, column, "invalid_timestamp", ERROR, f"{column} must be an ISO datetime.", value))
        return None
    return parsed


def _parse_decimal(file_name: str, row_number: int, column: str, value: str, issues: list[audit.Issue]) -> Decimal | None:
    if audit._is_blank(value):
        issues.append(_issue(file_name, row_number, column, "required_value_missing", ERROR, f"{column} is required."))
        return None
    parsed = audit._parse_decimal(value)
    if parsed is None:
        issues.append(_issue(file_name, row_number, column, "invalid_numeric", ERROR, f"{column} must be a finite decimal.", value))
        return None
    return parsed


def _required_text(
    file_name: str,
    row_number: int,
    column: str,
    value: str,
    issues: list[audit.Issue],
) -> str | None:
    text = "" if value is None else value.strip()
    if text == "":
        issues.append(_issue(file_name, row_number, column, "required_value_missing", ERROR, f"{column} is required."))
        return None
    return text


def _audit_price_duplicates(rows: list[PriceRow], issues: list[audit.Issue]) -> None:
    seen: set[tuple[datetime, str]] = set()
    for row in rows:
        key = (row.timestamp, row.symbol)
        if key in seen:
            issues.append(
                _issue(
                    row.file_name,
                    row.row_number,
                    "timestamp,symbol",
                    "duplicate_price_key",
                    ERROR,
                    "Duplicate market price timestamp/symbol.",
                )
            )
        seen.add(key)


def _audit_timestamp_comparability(rows: list[PriceRow], issues: list[audit.Issue], file_name: str) -> None:
    if not rows:
        return
    first = rows[0].timestamp
    for row in rows[1:]:
        if not audit._timestamps_are_comparable(first, row.timestamp):
            issues.append(
                _issue(
                    file_name,
                    row.row_number,
                    "timestamp",
                    "mixed_timezone_mode",
                    ERROR,
                    "Cannot mix timezone-aware and timezone-naive timestamps.",
                )
            )
            return


def _audit_factor_timestamp_comparability(rows: list[FactorRow], issues: list[audit.Issue]) -> None:
    if not rows:
        return
    first = rows[0].timestamp
    for row in rows[1:]:
        if not audit._timestamps_are_comparable(first, row.timestamp):
            issues.append(
                _issue(
                    "factors.csv",
                    row.row_number,
                    "timestamp",
                    "mixed_timezone_mode",
                    ERROR,
                    "Cannot mix timezone-aware and timezone-naive timestamps.",
                )
            )
            return


def _failed_payload(dataset_dir: Path, message: str) -> dict[str, Any]:
    return {
        "status": "ERROR",
        "dataset": str(dataset_dir),
        "files": [],
        "issues": [
            {
                "file": "-",
                "row": None,
                "column": None,
                "check": "internal_error",
                "severity": ERROR,
                "message": message,
                "value": None,
            }
        ],
    }


def _issue(
    file_name: str,
    row: int | None,
    column: str | None,
    check: str,
    severity: str,
    message: str,
    value: str | None = None,
) -> audit.Issue:
    return audit.Issue(file_name, row, column, check, severity, message, value)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
