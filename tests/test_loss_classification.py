from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from system_trading_s3 import factor_attribution
from system_trading_s3 import factor_report
from system_trading_s3 import loss_classification
from system_trading_s3 import simulate


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def write_file(root: Path, name: str, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(content, encoding="utf-8", newline="")


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
    simulate.export_run_artifacts(result, FIXTURES / "valid_multisymbol", export_dir, run_id="loss-classification-test")
    factor_report.write_factor_report(export_dir)
    factor_attribution.write_factor_attribution(export_dir)
    return export_dir


class LossClassificationTests(unittest.TestCase):
    def test_classifies_benchmark_relative_loss_periods(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_file(
                run_dir,
                "equity_curve.csv",
                "timestamp,equity,benchmark_equity\n"
                "2026-01-01,100,100\n"
                "2026-01-02,95,94\n"
                "2026-01-03,90,94\n"
                "2026-01-04,91,95\n",
            )
            result = loss_classification.write_loss_classification(run_dir)
            payload = json.loads((run_dir / "loss_classification.json").read_text(encoding="utf-8"))

        self.assertEqual(loss_classification.INCONCLUSIVE, result.status)
        self.assertEqual(3, payload["summary"]["period_count"])
        self.assertEqual(2, payload["summary"]["loss_period_count"])
        classifications = [period["classification"] for period in payload["periods"]]
        self.assertEqual(["BENCHMARK_EXPLAINED_LOSS", "EXCESS_RELATIVE_LOSS", "NO_LOSS"], classifications)
        self.assertTrue(any("factor_report.json missing" in gap for gap in payload["gaps"]))

    def test_uses_factor_report_as_context_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_factor_run(Path(tmp))
            result = loss_classification.write_loss_classification(run_dir)
            payload = json.loads((run_dir / "loss_classification.json").read_text(encoding="utf-8"))

        self.assertEqual(loss_classification.PASS, result.status)
        self.assertEqual(True, payload["factor_context"]["available"])
        self.assertIn("momentum", payload["factor_context"]["holding_factor_exposure"])
        self.assertEqual(True, payload["factor_context"]["factor_attribution"]["available"])
        self.assertIn("momentum", payload["factor_context"]["factor_attribution"]["factor_summary"])
        self.assertEqual({"NO_LOSS": 2}, payload["summary"]["classification_counts"])

    def test_missing_benchmark_reports_data_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_file(
                run_dir,
                "equity_curve.csv",
                "timestamp,equity\n2026-01-01,100\n2026-01-02,99\n",
            )
            result = loss_classification.write_loss_classification(run_dir)
            payload = json.loads((run_dir / "loss_classification.json").read_text(encoding="utf-8"))

        self.assertEqual(loss_classification.INCONCLUSIVE, result.status)
        self.assertEqual("UNEXPLAINED_LOSS_DATA_GAP", payload["periods"][0]["classification"])
        self.assertTrue(any("benchmark_equity missing" in gap for gap in payload["gaps"]))


class LossClassificationCliTests(unittest.TestCase):
    def test_loss_classification_cli_writes_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_factor_run(Path(tmp))
            completed = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.loss_classification", str(run_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            payload = json.loads((run_dir / "loss_classification.json").read_text(encoding="utf-8"))

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("LOSS CLASSIFICATION STATUS: PASS", completed.stdout)
        self.assertEqual("PASS", payload["status"])


if __name__ == "__main__":
    unittest.main()
