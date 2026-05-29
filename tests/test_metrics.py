from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from system_trading_s3 import metrics
from system_trading_s3 import simulate


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def write_file(root: Path, name: str, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(content, encoding="utf-8", newline="")


def export_default_run(root: Path) -> Path:
    export_dir = root / "run"
    result = simulate.run_simulation(FIXTURES / "valid_complete")
    simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id="metrics-test")
    return export_dir


def export_friction_config_run(root: Path) -> Path:
    export_dir = root / "run"
    strategy = simulate.create_strategy("EqualWeightRebalance", {})
    result = simulate.run_simulation(
        FIXTURES / "valid_complete",
        Decimal("1000"),
        strategy,
        simulate.FrictionModel(fee_rate=Decimal("0.0005"), slippage_per_trade=Decimal("0.01")),
        Decimal("0.02"),
    )
    simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id="metrics-friction-test")
    return export_dir


def export_multisymbol_run(root: Path) -> Path:
    export_dir = root / "run"
    config = simulate.load_simulation_config(FIXTURES / "sample_config.json")
    strategy = simulate.create_strategy(config.strategy_name, config.strategy_params)
    result = simulate.run_simulation(
        FIXTURES / "valid_multisymbol",
        config.initial_cash,
        strategy,
        config.friction,
        config.risk_free_rate,
    )
    simulate.export_run_artifacts(result, FIXTURES / "valid_multisymbol", export_dir, run_id="metrics-multisymbol-test")
    return export_dir


class MetricsTests(unittest.TestCase):
    def test_metrics_json_created_for_exported_default_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_default_run(Path(tmp))
            result = metrics.write_metrics(run_dir)
            payload = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))

        self.assertEqual(metrics.PASS, result.status)
        self.assertEqual("mvp6.metrics.v1", payload["schema_version"])
        self.assertEqual("0.002000", payload["total_return_pct"])
        self.assertEqual("0.000000", payload["max_drawdown_pct"])
        self.assertEqual("100.000000", payload["win_rate_pct"])
        self.assertEqual("INF", payload["profit_factor"])
        self.assertEqual(2, payload["total_number_of_trades"])
        self.assertEqual(1, payload["realized_trade_count"])
        self.assertIn("benchmark_relative", payload)
        self.assertNotEqual("UNAVAILABLE", payload["benchmark_relative"]["alpha_pct"])
        self.assertIn("realized_pnl missing for 1 of 2 trade rows.", payload["gaps"])

    def test_metrics_reports_unavailable_when_realized_pnl_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_friction_config_run(Path(tmp))
            result = metrics.write_metrics(run_dir)
            payload = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))

        self.assertEqual(metrics.PASS, result.status)
        self.assertEqual("UNAVAILABLE", payload["win_rate_pct"])
        self.assertEqual("UNAVAILABLE", payload["profit_factor"])
        self.assertEqual(1, payload["total_number_of_trades"])
        self.assertEqual(0, payload["realized_trade_count"])
        self.assertNotEqual("UNAVAILABLE", payload["benchmark_relative"]["alpha_pct"])
        self.assertEqual("0.020000", payload["benchmark_relative"]["risk_free_rate"])
        self.assertIn("win_rate_pct and profit_factor unavailable because no realized_pnl rows are available.", payload["gaps"])

    def test_metrics_calculates_drawdown_and_trade_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_file(
                run_dir,
                "equity_curve.csv",
                "timestamp,equity\n2026-01-01,100\n2026-01-02,120\n2026-01-03,90\n2026-01-04,110\n",
            )
            write_file(
                run_dir,
                "trades.csv",
                "timestamp,trade_id,realized_pnl\n2026-01-02T09:30:00,T1,10\n2026-01-03T09:30:00,T2,-5\n",
            )
            result = metrics.write_metrics(run_dir)
            payload = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))

        self.assertEqual(metrics.PASS, result.status)
        self.assertEqual("10.000000", payload["total_return_pct"])
        self.assertEqual("25.000000", payload["max_drawdown_pct"])
        self.assertEqual("50.000000", payload["win_rate_pct"])
        self.assertEqual("2.000000", payload["profit_factor"])
        self.assertNotEqual("UNAVAILABLE", payload["cagr_pct"])
        self.assertEqual("UNAVAILABLE", payload["benchmark_relative"]["alpha_pct"])
        self.assertIn("annualized metrics may be unstable because equity_curve has fewer than 20 rows.", payload["gaps"])

    def test_benchmark_relative_metrics_calculate_from_equity_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_file(
                run_dir,
                "run_manifest.json",
                "{\"risk_free_rate\":\"0.01\"}\n",
            )
            write_file(
                run_dir,
                "equity_curve.csv",
                "timestamp,equity,benchmark_equity\n"
                "2026-01-01,100,100\n"
                "2026-01-02,110,105\n"
                "2026-01-03,121,110.25\n",
            )
            write_file(
                run_dir,
                "trades.csv",
                "timestamp,trade_id,realized_pnl\n2026-01-02T09:30:00,T1,10\n",
            )
            result = metrics.write_metrics(run_dir)
            payload = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))

        self.assertEqual(metrics.PASS, result.status)
        relative = payload["benchmark_relative"]
        self.assertEqual(True, relative["benchmark_available"])
        self.assertEqual("1260.000000", relative["alpha_pct"])
        self.assertEqual("UNAVAILABLE", relative["beta"])
        self.assertEqual("UNAVAILABLE", relative["sharpe_ratio"])
        self.assertEqual("0.000000", relative["tracking_error_pct"])
        self.assertEqual("UNAVAILABLE", relative["information_ratio"])

    def test_benchmark_relative_beta_and_volatility_math(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_file(run_dir, "run_manifest.json", "{\"risk_free_rate\":\"0.01\"}\n")
            write_file(
                run_dir,
                "equity_curve.csv",
                "timestamp,equity,benchmark_equity\n"
                "2026-01-01,100,100\n"
                "2026-01-02,110,105\n"
                "2026-01-03,99,99.75\n",
            )
            write_file(
                run_dir,
                "trades.csv",
                "timestamp,trade_id,realized_pnl\n2026-01-02T09:30:00,T1,10\n",
            )
            result = metrics.write_metrics(run_dir)
            payload = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))

        self.assertEqual(metrics.PASS, result.status)
        relative = payload["benchmark_relative"]
        self.assertEqual("0.000000", relative["alpha_pct"])
        self.assertEqual("2.000000", relative["beta"])
        self.assertEqual("-0.006299", relative["sharpe_ratio"])
        self.assertEqual("79.372539", relative["tracking_error_pct"])
        self.assertEqual("0.000000", relative["information_ratio"])

    def test_metrics_output_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_default_run(Path(tmp))
            metrics.write_metrics(run_dir)
            first = (run_dir / "metrics.json").read_bytes()
            metrics.write_metrics(run_dir)
            second = (run_dir / "metrics.json").read_bytes()
        self.assertEqual(first, second)

    def test_metrics_work_on_multisymbol_portfolio_equity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_multisymbol_run(Path(tmp))
            result = metrics.write_metrics(run_dir)
            payload = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))

        self.assertEqual(metrics.PASS, result.status)
        self.assertEqual("1.703384", payload["total_return_pct"])
        self.assertEqual("0.000000", payload["max_drawdown_pct"])
        self.assertEqual(True, payload["benchmark_relative"]["benchmark_available"])
        self.assertNotEqual("UNAVAILABLE", payload["benchmark_relative"]["alpha_pct"])

    def test_missing_required_metrics_input_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_file(run_dir, "trades.csv", "timestamp,trade_id,realized_pnl\n")
            result = metrics.write_metrics(run_dir)
        self.assertEqual(metrics.FAIL, result.status)
        self.assertIn("equity_curve.csv is missing.", result.errors)


class MetricsCliTests(unittest.TestCase):
    def test_metrics_cli_generates_metrics_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_friction_config_run(Path(tmp))
            completed = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.metrics", str(run_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            payload = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))

        self.assertEqual(0, completed.returncode)
        self.assertIn("METRICS STATUS: PASS", completed.stdout)
        self.assertEqual("PASS", payload["status"])
        self.assertEqual("UNAVAILABLE", payload["win_rate_pct"])


if __name__ == "__main__":
    unittest.main()
