"""Gated statistical factor risk model for factor attribution artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any


PASS = "PASS"
INCONCLUSIVE = "INCONCLUSIVE"
FAIL = "FAIL"
REPORT_SCHEMA_VERSION = "mvp14.factor_risk_model.v1"
REPORT_FILE_NAME = "factor_risk_model.json"
ATTRIBUTION_FILE_NAME = "factor_attribution.json"
RETURN_QUANT = Decimal("0.000001")
MIN_FACTOR_COUNT = 2
ABSOLUTE_MIN_OBSERVATIONS = 5


class FactorRiskModelError(Exception):
    """Raised when the source attribution report is malformed."""


@dataclass(frozen=True)
class ModelRow:
    dependent_value: Decimal
    factor_values: dict[str, Decimal]


@dataclass(frozen=True)
class FactorRiskModelResult:
    status: str
    payload: dict[str, Any]
    errors: list[str]


def calculate_factor_risk_model(run_artifact_dir: Path | str) -> FactorRiskModelResult:
    run_dir = Path(run_artifact_dir)
    attribution_path = run_dir / ATTRIBUTION_FILE_NAME
    if not attribution_path.is_file():
        gaps = ["factor_attribution.json missing; statistical factor risk model is unavailable."]
        return FactorRiskModelResult(status=INCONCLUSIVE, payload=_base_payload({}, gaps), errors=[])

    try:
        attribution = _load_json(attribution_path)
        factor_names = _factor_names(attribution)
        periods = _periods(attribution)
    except FactorRiskModelError as exc:
        return FactorRiskModelResult(status=FAIL, payload=_failed_payload([str(exc)]), errors=[str(exc)])

    gaps: list[str] = []
    if len(factor_names) < MIN_FACTOR_COUNT:
        gaps.append(f"at least {MIN_FACTOR_COUNT} factors are required for a multi-factor risk model; found {len(factor_names)}.")

    dependent_variable = _dependent_variable(periods)
    rows = _model_rows(periods, factor_names, dependent_variable)
    min_observations = _min_observations(len(factor_names))
    if len(rows) < min_observations:
        gaps.append(
            f"at least {min_observations} complete observations are required for {len(factor_names)} factor(s); found {len(rows)}."
        )

    payload = _base_payload(attribution, gaps)
    payload.update(
        {
            "dependent_variable": dependent_variable,
            "observation_count": len(rows),
            "factor_count": len(factor_names),
            "factor_names": factor_names,
            "requirements": {
                "min_factor_count": MIN_FACTOR_COUNT,
                "min_observation_count": min_observations,
                "complete_rows_required": True,
            },
        }
    )
    if gaps:
        return FactorRiskModelResult(status=INCONCLUSIVE, payload=payload, errors=[])

    try:
        model = _fit_ols(rows, factor_names)
    except FactorRiskModelError as exc:
        payload["gaps"] = [*gaps, str(exc)]
        payload["status"] = INCONCLUSIVE
        return FactorRiskModelResult(status=INCONCLUSIVE, payload=payload, errors=[])

    payload.update(model)
    payload["status"] = PASS
    payload["interpretation"] = (
        "This report is a deterministic OLS factor risk model over complete factor-attribution periods; "
        "it is not a forecast, trading signal, or profitability claim."
    )
    return FactorRiskModelResult(status=PASS, payload=payload, errors=[])


def write_factor_risk_model(run_artifact_dir: Path | str) -> FactorRiskModelResult:
    run_dir = Path(run_artifact_dir)
    result = calculate_factor_risk_model(run_dir)
    if result.status in {PASS, INCONCLUSIVE}:
        _write_json(run_dir / REPORT_FILE_NAME, result.payload)
    return result


def format_factor_risk_model_result(result: FactorRiskModelResult, report_path: Path | None = None) -> str:
    lines = [f"FACTOR RISK MODEL STATUS: {result.status}"]
    if report_path is not None:
        lines.append(f"FACTOR RISK MODEL FILE: {report_path}")
    lines.append(f"DEPENDENT VARIABLE: {result.payload.get('dependent_variable', 'UNAVAILABLE')}")
    lines.append(f"OBSERVATIONS: {result.payload.get('observation_count', 0)}")
    lines.append(f"FACTOR COUNT: {result.payload.get('factor_count', 0)}")
    if result.payload.get("coefficients"):
        lines.append("COEFFICIENTS:")
        coefficients = result.payload["coefficients"]
        if isinstance(coefficients, dict):
            for name in ["intercept", *result.payload.get("factor_names", [])]:
                if name in coefficients:
                    lines.append(f"- {name}: {coefficients[name]}")
        lines.append(f"R_SQUARED: {result.payload.get('r_squared', 'UNAVAILABLE')}")
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
    parser = argparse.ArgumentParser(description="Create a gated statistical factor risk model from factor attribution artifacts.")
    parser.add_argument("run_artifact_dir", type=Path, help="Directory containing factor_attribution.json.")
    args = parser.parse_args(argv)

    if not args.run_artifact_dir.exists() or not args.run_artifact_dir.is_dir():
        print(f"run_artifact_dir must be an existing directory: {args.run_artifact_dir}", file=sys.stderr)
        return 2

    try:
        result = write_factor_risk_model(args.run_artifact_dir)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary.
        print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    print(format_factor_risk_model_result(result, args.run_artifact_dir / REPORT_FILE_NAME))
    return 0 if result.status in {PASS, INCONCLUSIVE} else 1


def _base_payload(attribution: dict[str, Any], gaps: list[str]) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": INCONCLUSIVE,
        "run_id": attribution.get("run_id", ""),
        "strategy_name": attribution.get("strategy_name", ""),
        "source_files": [ATTRIBUTION_FILE_NAME],
        "dependent_variable": "UNAVAILABLE",
        "observation_count": 0,
        "factor_count": 0,
        "factor_names": [],
        "requirements": {
            "min_factor_count": MIN_FACTOR_COUNT,
            "min_observation_count": ABSOLUTE_MIN_OBSERVATIONS,
            "complete_rows_required": True,
        },
        "coefficients": {},
        "r_squared": "UNAVAILABLE",
        "residual_summary": {},
        "gaps": gaps,
        "interpretation": (
            "Statistical factor risk model is gated; it reports INCONCLUSIVE unless complete observations "
            "and multiple factors are sufficient for a deterministic OLS fit."
        ),
    }


def _failed_payload(errors: list[str]) -> dict[str, Any]:
    return {"schema_version": REPORT_SCHEMA_VERSION, "status": FAIL, "errors": errors, "gaps": []}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactorRiskModelError(f"factor_attribution.json cannot be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise FactorRiskModelError("factor_attribution.json must contain a JSON object.")
    return payload


def _factor_names(attribution: dict[str, Any]) -> list[str]:
    names = attribution.get("factor_names")
    if isinstance(names, list) and all(isinstance(item, str) and item for item in names):
        return sorted(names)
    summary = attribution.get("summary", {})
    if isinstance(summary, dict):
        factor_summary = summary.get("factor_summary", {})
        if isinstance(factor_summary, dict):
            return sorted(str(name) for name in factor_summary if str(name))
    return []


def _periods(attribution: dict[str, Any]) -> list[dict[str, Any]]:
    periods = attribution.get("periods", [])
    if not isinstance(periods, list):
        raise FactorRiskModelError("factor_attribution.json periods must be a list.")
    parsed: list[dict[str, Any]] = []
    for index, period in enumerate(periods, start=1):
        if not isinstance(period, dict):
            raise FactorRiskModelError(f"factor_attribution.json period {index} must be an object.")
        parsed.append(period)
    return parsed


def _dependent_variable(periods: list[dict[str, Any]]) -> str:
    if periods and all(_parse_decimal(period.get("active_return")) is not None for period in periods):
        return "active_return"
    return "strategy_return"


def _model_rows(periods: list[dict[str, Any]], factor_names: list[str], dependent_variable: str) -> list[ModelRow]:
    rows: list[ModelRow] = []
    for period in periods:
        y = _parse_decimal(period.get(dependent_variable))
        if y is None:
            continue
        factor_proxy = period.get("factor_return_proxy", {})
        if not isinstance(factor_proxy, dict):
            continue
        values: dict[str, Decimal] = {}
        for factor_name in factor_names:
            item = factor_proxy.get(factor_name, {})
            if not isinstance(item, dict):
                values = {}
                break
            contribution = _parse_decimal(item.get("proxy_contribution"))
            if contribution is None:
                values = {}
                break
            values[factor_name] = contribution
        if len(values) == len(factor_names):
            rows.append(ModelRow(dependent_value=y, factor_values=values))
    return rows


def _min_observations(factor_count: int) -> int:
    return max(ABSOLUTE_MIN_OBSERVATIONS, factor_count + 2)


def _fit_ols(rows: list[ModelRow], factor_names: list[str]) -> dict[str, Any]:
    with localcontext() as context:
        context.prec = 50
        x_rows = [[Decimal("1"), *[row.factor_values[name] for name in factor_names]] for row in rows]
        y_values = [row.dependent_value for row in rows]
        xtx = _matrix_product_transpose(x_rows)
        xty = _vector_product_transpose(x_rows, y_values)
        coefficients = _solve_linear_system(xtx, xty)
        fitted = [_dot(row, coefficients) for row in x_rows]
        residuals = [actual - estimate for actual, estimate in zip(y_values, fitted)]
        y_mean = sum(y_values, Decimal("0")) / Decimal(len(y_values))
        ss_total = sum(((value - y_mean) ** 2 for value in y_values), Decimal("0"))
        if ss_total == 0:
            raise FactorRiskModelError("dependent variable has zero variance; OLS risk model is not informative.")
        ss_residual = sum((residual ** 2 for residual in residuals), Decimal("0"))
        r_squared = Decimal("1") - (ss_residual / ss_total)
        residual_mean = sum(residuals, Decimal("0")) / Decimal(len(residuals))
        residual_variance = sum(((value - residual_mean) ** 2 for value in residuals), Decimal("0")) / Decimal(len(residuals))
        residual_std = residual_variance.sqrt()
    coefficient_payload = {"intercept": _format_decimal(coefficients[0])}
    coefficient_payload.update({name: _format_decimal(value) for name, value in zip(factor_names, coefficients[1:])})
    return {
        "coefficients": coefficient_payload,
        "r_squared": _format_decimal(r_squared),
        "residual_summary": {
            "residual_count": len(residuals),
            "residual_sum": _format_decimal(sum(residuals, Decimal("0"))),
            "residual_mean": _format_decimal(residual_mean),
            "residual_std": _format_decimal(residual_std),
            "sum_squared_residual": _format_decimal(ss_residual),
        },
    }


def _matrix_product_transpose(rows: list[list[Decimal]]) -> list[list[Decimal]]:
    width = len(rows[0])
    return [
        [sum((row[left] * row[right] for row in rows), Decimal("0")) for right in range(width)]
        for left in range(width)
    ]


def _vector_product_transpose(rows: list[list[Decimal]], values: list[Decimal]) -> list[Decimal]:
    width = len(rows[0])
    return [sum((row[index] * value for row, value in zip(rows, values)), Decimal("0")) for index in range(width)]


def _solve_linear_system(matrix: list[list[Decimal]], vector: list[Decimal]) -> list[Decimal]:
    size = len(vector)
    augmented = [row.copy() + [value] for row, value in zip(matrix, vector)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row_index: abs(augmented[row_index][column]))
        if augmented[pivot][column] == 0:
            raise FactorRiskModelError("factor design matrix is singular; OLS risk model is not identifiable.")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        augmented[column] = [value / pivot_value for value in augmented[column]]
        for row_index in range(size):
            if row_index == column:
                continue
            factor = augmented[row_index][column]
            if factor == 0:
                continue
            augmented[row_index] = [
                current - factor * pivot_current
                for current, pivot_current in zip(augmented[row_index], augmented[column])
            ]
    return [augmented[row_index][-1] for row_index in range(size)]


def _dot(left: list[Decimal], right: list[Decimal]) -> Decimal:
    return sum((a * b for a, b in zip(left, right)), Decimal("0"))


def _parse_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str) or value == "UNAVAILABLE" or not value.strip():
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _format_decimal(value: Decimal) -> str:
    return format(value.quantize(RETURN_QUANT, rounding=ROUND_HALF_UP), "f")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
