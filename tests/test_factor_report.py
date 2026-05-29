from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from system_trading_s3 import factor_report
from system_trading_s3 import simulate


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def export_factor_run(root: Path) -> Path:
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
    simulate.export_run_artifacts(result, FIXTURES / "valid_multisymbol", export_dir, run_id="factor-report-test")
    return export_dir


class FactorReportTests(unittest.TestCase):
    def test_factor_report_passes_for_periodic_factor_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_factor_run(Path(tmp))
            result = factor_report.write_factor_report(run_dir)
            payload = json.loads((run_dir / "factor_report.json").read_text(encoding="utf-8"))

        self.assertEqual(factor_report.PASS, result.status)
        self.assertEqual("PeriodicFactorWeight", payload["strategy_name"])
        self.assertEqual(["momentum"], payload["factor_names"])
        momentum = payload["factor_exposure"]["momentum"]
        self.assertEqual(2, momentum["buy_fills_with_factor"])
        self.assertEqual("1.350000", momentum["average_buy_factor_value"])
        self.assertEqual("1.000000", momentum["average_buy_factor_rank"])
        self.assertEqual(1, momentum["best_buy_factor_rank"])
        self.assertEqual(1, momentum["worst_buy_factor_rank"])
        self.assertEqual(2, momentum["top_rank_buy_count"])
        self.assertEqual([], payload["gaps"])
        self.assertIn("not a profitability claim", payload["interpretation"])

    def test_factor_report_is_inconclusive_without_factors_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            result = simulate.run_simulation(FIXTURES / "valid_complete")
            simulate.export_run_artifacts(result, FIXTURES / "valid_complete", run_dir, run_id="no-factor-report-test")
            report = factor_report.write_factor_report(run_dir)
            payload = json.loads((run_dir / "factor_report.json").read_text(encoding="utf-8"))

        self.assertEqual(factor_report.INCONCLUSIVE, report.status)
        self.assertEqual("INCONCLUSIVE", payload["status"])
        self.assertIn("factors.csv missing; factor exposure report is unavailable.", payload["gaps"])


class FactorReportCliTests(unittest.TestCase):
    def test_factor_report_cli_writes_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_factor_run(Path(tmp))
            completed = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.factor_report", str(run_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            payload = json.loads((run_dir / "factor_report.json").read_text(encoding="utf-8"))

        self.assertEqual(0, completed.returncode)
        self.assertIn("FACTOR REPORT STATUS: PASS", completed.stdout)
        self.assertEqual("PASS", payload["status"])


if __name__ == "__main__":
    unittest.main()
