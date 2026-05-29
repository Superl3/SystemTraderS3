"""Read-only simulated trading dataset audit CLI."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


PASS = "PASS"
INCONCLUSIVE = "INCONCLUSIVE"
FAIL = "FAIL"

ERROR = "error"
GAP = "gap"

CONTRACT_FILES = [
    "equity_curve.schema.json",
    "trades.schema.json",
    "benchmark.schema.json",
    "factor_exposure.schema.json",
]

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
COST_FIELDS = {"cost", "fee", "commission", "slippage", "spread_cost"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$")


@dataclass(frozen=True)
class Issue:
    file: str
    row: int | None
    column: str | None
    check: str
    severity: str
    message: str
    value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "row": self.row,
            "column": self.column,
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
        }


@dataclass(frozen=True)
class FileSummary:
    file: str
    present: bool
    rows: int

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "present": self.present, "rows": self.rows}


@dataclass(frozen=True)
class AuditResult:
    status: str
    dataset: str
    files: list[FileSummary]
    issues: list[Issue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dataset": self.dataset,
            "files": [summary.to_dict() for summary in self.files],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class FileAudit:
    summary: FileSummary
    issues: list[Issue]
    timestamps: list[date | datetime]
    headers: list[str]
    nonblank_columns: set[str]


def load_contracts(schema_dir: Path = SCHEMA_DIR) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for file_name in CONTRACT_FILES:
        contract_path = schema_dir / file_name
        with contract_path.open("r", encoding="utf-8") as handle:
            contracts.append(json.load(handle))
    return contracts


def audit_dataset(dataset_dir: Path | str) -> AuditResult:
    dataset_path = Path(dataset_dir)
    contracts = load_contracts()
    issues: list[Issue] = []
    summaries: list[FileSummary] = []
    audits: dict[str, FileAudit] = {}

    for contract in contracts:
        file_name = contract["file_name"]
        file_path = dataset_path / file_name
        if not file_path.exists():
            summaries.append(FileSummary(file_name, False, 0))
            if contract["required"]:
                issues.append(
                    Issue(
                        file_name,
                        None,
                        None,
                        "required_file_missing",
                        ERROR,
                        f"Required file {file_name} is missing.",
                    )
                )
            else:
                issues.append(
                    Issue(
                        file_name,
                        None,
                        None,
                        "optional_file_missing",
                        GAP,
                        f"Optional file {file_name} is missing; future analysis using it is unavailable.",
                    )
                )
            continue

        audit = _audit_file(file_path, contract)
        audits[file_name] = audit
        summaries.append(audit.summary)
        issues.extend(audit.issues)

    _audit_cost_readiness(audits.get("trades.csv"), issues)
    _audit_required_range_readiness(audits, issues)

    status = _compose_status(issues)
    return AuditResult(status=status, dataset=str(dataset_path), files=summaries, issues=issues)


def format_human(result: AuditResult) -> str:
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


def exit_code_for_status(status: str, strict: bool) -> int:
    if status == FAIL:
        return 1
    if status == INCONCLUSIVE and strict:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a simulated trading dataset.")
    parser.add_argument("dataset_dir", type=Path, help="Directory containing simulated CSV files.")
    parser.add_argument("--strict", action="store_true", help="Treat INCONCLUSIVE as exit code 1.")
    parser.add_argument("--json", action="store_true", help="Emit deterministic JSON output.")
    args = parser.parse_args(argv)

    if not args.dataset_dir.exists() or not args.dataset_dir.is_dir():
        print(f"dataset_dir must be an existing directory: {args.dataset_dir}", file=sys.stderr)
        return 2

    try:
        result = audit_dataset(args.dataset_dir)
    except Exception as exc:  # pragma: no cover - defensive boundary.
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "ERROR",
                        "dataset": str(args.dataset_dir),
                        "files": [],
                        "issues": [
                            {
                                "file": "-",
                                "row": None,
                                "column": None,
                                "check": "internal_error",
                                "severity": ERROR,
                                "message": str(exc),
                                "value": None,
                            }
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_human(result))
    return exit_code_for_status(result.status, args.strict)


def _audit_file(path: Path, contract: dict[str, Any]) -> FileAudit:
    file_name = contract["file_name"]
    issues: list[Issue] = []
    timestamps: list[date | datetime] = []
    rows, headers, read_issues = _read_csv(path, file_name)
    issues.extend(read_issues)

    required_headers = contract["required_headers"]
    for header in required_headers:
        if header not in headers:
            issues.append(
                Issue(file_name, None, header, "required_header_missing", ERROR, f"Required header {header} is missing.")
            )

    if not rows:
        severity = ERROR if contract["required"] else GAP
        check = "required_file_empty" if contract["required"] else "optional_file_empty"
        issues.append(
            Issue(
                file_name,
                None,
                None,
                check,
                severity,
                f"{file_name} contains no data rows.",
            )
        )
        return FileAudit(FileSummary(file_name, True, 0), issues, timestamps, headers, set())

    if any(issue.severity == ERROR and issue.check in {"duplicate_header", "row_width"} for issue in issues):
        return FileAudit(FileSummary(file_name, True, len(rows)), issues, timestamps, headers, set())

    previous_timestamp: date | datetime | None = None
    seen_keys: set[tuple[Any, ...]] = set()
    timezone_mode: str | None = None
    nonblank_columns: set[str] = set()

    for row_number, row in rows:
        nonblank_columns.update(column for column, value in row.items() if not _is_blank(value))
        parsed_numeric: dict[str, Decimal] = {}
        missing_required = {header for header in required_headers if _is_blank(row.get(header))}
        for column in sorted(missing_required):
            issues.append(
                Issue(file_name, row_number, column, "required_value_missing", ERROR, f"Required value {column} is missing.")
            )

        timestamp_column = contract["timestamp"]["column"]
        parsed_timestamp: date | datetime | None = None
        if timestamp_column not in missing_required and timestamp_column in headers:
            parsed_timestamp = _parse_timestamp(row.get(timestamp_column, ""), contract["timestamp"]["kind"])
            if parsed_timestamp is None:
                issues.append(
                    Issue(
                        file_name,
                        row_number,
                        timestamp_column,
                        "invalid_timestamp",
                        ERROR,
                        "Timestamp must be ISO 8601 and match the file timestamp kind.",
                        row.get(timestamp_column),
                    )
                )
            else:
                timestamps.append(parsed_timestamp)
                if isinstance(parsed_timestamp, datetime):
                    current_timezone_mode = "aware" if parsed_timestamp.tzinfo is not None else "naive"
                    if timezone_mode is None:
                        timezone_mode = current_timezone_mode
                    elif timezone_mode != current_timezone_mode:
                        issues.append(
                            Issue(
                                file_name,
                                row_number,
                                timestamp_column,
                                "mixed_timezone_mode",
                                ERROR,
                                "Timezone-aware and timezone-naive datetimes are mixed within one file.",
                                row.get(timestamp_column),
                            )
                        )

                if previous_timestamp is not None and _timestamps_are_comparable(previous_timestamp, parsed_timestamp):
                    order = contract["timestamp"]["order"]
                    if order == "strictly_increasing" and parsed_timestamp <= previous_timestamp:
                        issues.append(
                            Issue(
                                file_name,
                                row_number,
                                timestamp_column,
                                "timestamp_order",
                                ERROR,
                                "Timestamp must be strictly increasing.",
                                row.get(timestamp_column),
                            )
                        )
                    elif order == "non_decreasing" and parsed_timestamp < previous_timestamp:
                        issues.append(
                            Issue(
                                file_name,
                                row_number,
                                timestamp_column,
                                "timestamp_order",
                                ERROR,
                                "Timestamp must be non-decreasing.",
                                row.get(timestamp_column),
                            )
                        )
                previous_timestamp = parsed_timestamp

        for column in contract["numeric_fields"]["required"]:
            if column in missing_required or column not in headers:
                continue
            value = _parse_decimal(row.get(column, ""))
            if value is None:
                issues.append(
                    Issue(file_name, row_number, column, "invalid_numeric", ERROR, "Numeric value is invalid.", row.get(column))
                )
            else:
                parsed_numeric[column] = value

        for column in contract["numeric_fields"]["optional"]:
            if column not in headers or _is_blank(row.get(column)):
                continue
            value = _parse_decimal(row.get(column, ""))
            if value is None:
                issues.append(
                    Issue(file_name, row_number, column, "invalid_numeric", ERROR, "Numeric value is invalid.", row.get(column))
                )
                continue
            parsed_numeric[column] = value
            if column in contract["nonnegative_fields"] and value < 0:
                issues.append(
                    Issue(
                        file_name,
                        row_number,
                        column,
                        "negative_cost",
                        ERROR,
                        "Cost-like fields must be nonnegative.",
                        row.get(column),
                    )
                )

        for column, allowed_values in contract["enum_fields"].items():
            if column in missing_required or column not in headers:
                continue
            value = row.get(column, "")
            if value != value.strip():
                issues.append(
                    Issue(file_name, row_number, column, "enum_whitespace", ERROR, "Enum values must not contain whitespace.", value)
                )
            elif value not in allowed_values:
                issues.append(
                    Issue(file_name, row_number, column, "unknown_enum", ERROR, "Enum value is not allowed.", value)
                )

        key = _build_key(contract["key_columns"], row, parsed_timestamp)
        if key is not None:
            if key in seen_keys:
                issues.append(
                    Issue(
                        file_name,
                        row_number,
                        ",".join(contract["key_columns"]),
                        "duplicate_key",
                        ERROR,
                        "Duplicate row key is not allowed.",
                        "|".join(str(part) for part in key),
                    )
                )
            seen_keys.add(key)

        if contract["alternative_groups"]:
            _audit_alternative_groups(file_name, row_number, contract, row, parsed_numeric, issues)

    return FileAudit(FileSummary(file_name, True, len(rows)), issues, timestamps, headers, nonblank_columns)


def _read_csv(path: Path, file_name: str) -> tuple[list[tuple[int, dict[str, str]]], list[str], list[Issue]]:
    issues: list[Issue] = []
    rows: list[tuple[int, dict[str, str]]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                raw_headers = next(reader)
            except StopIteration:
                return rows, [], [
                    Issue(file_name, None, None, "empty_csv", ERROR, f"{file_name} is empty and has no header row.")
                ]

            headers = [header.strip() for header in raw_headers]
            duplicates = sorted({header for header in headers if headers.count(header) > 1})
            for header in duplicates:
                issues.append(
                    Issue(file_name, None, header, "duplicate_header", ERROR, "Duplicate header after trimming whitespace.")
                )

            for row_number, raw_row in enumerate(reader, start=2):
                if not raw_row or all(_is_blank(cell) for cell in raw_row):
                    continue
                if len(raw_row) > len(headers):
                    issues.append(
                        Issue(
                            file_name,
                            row_number,
                            None,
                            "row_width",
                            ERROR,
                            "Row has more fields than the header row.",
                            str(len(raw_row)),
                        )
                    )
                    continue
                padded = raw_row + [""] * (len(headers) - len(raw_row))
                rows.append((row_number, dict(zip(headers, padded))))
            return rows, headers, issues
    except csv.Error as exc:
        return rows, [], [Issue(file_name, None, None, "csv_parse_error", ERROR, str(exc))]
    except UnicodeDecodeError as exc:
        return rows, [], [Issue(file_name, None, None, "csv_decode_error", ERROR, str(exc))]


def _audit_alternative_groups(
    file_name: str,
    row_number: int,
    contract: dict[str, Any],
    row: dict[str, str],
    parsed_numeric: dict[str, Decimal],
    issues: list[Issue],
) -> None:
    has_realized = "realized_pnl" in parsed_numeric
    has_strategy_return = "strategy_return" in parsed_numeric
    quantity = parsed_numeric.get("quantity")
    has_quantity = quantity is not None and quantity != 0
    has_price = any(price_field in parsed_numeric for price_field in contract["price_fields"])

    if has_realized or has_strategy_return or (has_quantity and has_price):
        return

    value = {
        "realized_pnl": row.get("realized_pnl"),
        "strategy_return": row.get("strategy_return"),
        "quantity": row.get("quantity"),
        "price_fields": {field: row.get(field) for field in contract["price_fields"] if field in row},
    }
    issues.append(
        Issue(
            file_name,
            row_number,
            None,
            "trade_readiness_group",
            ERROR,
            "Trade row must include realized_pnl, strategy_return, or nonzero quantity plus a price field.",
            json.dumps(value, sort_keys=True),
        )
    )


def _audit_cost_readiness(trades_audit: FileAudit | None, issues: list[Issue]) -> None:
    if trades_audit is None or trades_audit.summary.rows == 0:
        return
    if COST_FIELDS.intersection(trades_audit.nonblank_columns):
        return
    issues.append(
        Issue(
            "trades.csv",
            None,
            None,
            "optional_cost_fields_missing",
            GAP,
            "No optional cost fields are present; future cost-drag analysis is unavailable.",
        )
    )


def _audit_required_range_readiness(audits: dict[str, FileAudit], issues: list[Issue]) -> None:
    equity = audits.get("equity_curve.csv")
    trades = audits.get("trades.csv")
    if equity is None or trades is None or not equity.timestamps or not trades.timestamps:
        return
    if any(issue.severity == ERROR for issue in equity.issues + trades.issues):
        return

    equity_dates = [stamp for stamp in equity.timestamps if isinstance(stamp, date) and not isinstance(stamp, datetime)]
    trade_dates = [stamp.date() for stamp in trades.timestamps if isinstance(stamp, datetime)]
    if not equity_dates or not trade_dates:
        return

    min_equity = min(equity_dates)
    max_equity = max(equity_dates)
    outside = [stamp.isoformat() for stamp in trade_dates if stamp < min_equity or stamp > max_equity]
    if outside:
        issues.append(
            Issue(
                "trades.csv",
                None,
                "timestamp",
                "range_coverage",
                GAP,
                "Some trade dates fall outside the equity_curve.csv date range.",
                ",".join(outside),
            )
        )


def _compose_status(issues: list[Issue]) -> str:
    if any(issue.severity == ERROR for issue in issues):
        return FAIL
    if any(issue.severity == GAP for issue in issues):
        return INCONCLUSIVE
    return PASS


def _parse_timestamp(value: str, kind: str) -> date | datetime | None:
    text = value.strip()
    if kind == "date":
        if DATE_RE.fullmatch(text) is None:
            return None
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            return None
        return parsed

    if kind == "datetime":
        if DATETIME_RE.fullmatch(text) is None:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed

    return None


def _parse_decimal(value: str) -> Decimal | None:
    text = value.strip()
    if text == "":
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _timestamps_are_comparable(left: date | datetime, right: date | datetime) -> bool:
    if isinstance(left, datetime) and isinstance(right, datetime):
        left_aware = left.tzinfo is not None
        right_aware = right.tzinfo is not None
        return left_aware == right_aware
    return type(left) is type(right)


def _build_key(columns: list[str], row: dict[str, str], parsed_timestamp: date | datetime | None) -> tuple[Any, ...] | None:
    parts: list[Any] = []
    for column in columns:
        if column == "timestamp":
            if parsed_timestamp is None:
                return None
            parts.append(parsed_timestamp)
            continue
        value = row.get(column)
        if _is_blank(value):
            return None
        parts.append(value)
    return tuple(parts)


def _is_blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
