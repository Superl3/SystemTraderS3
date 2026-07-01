from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from system_trading_s3 import factor_attribution
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
    simulate.export_run_artifacts(result, FIXTURES / "valid_multisymbol", export_dir, run_id="factor-attribution-test")
    return export_dir


class FactorAttributionTests(unittest.TestCase):
    def test_factor_attribution_passes_for_periodic_factor_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_factor_run(Path(tmp))
            result = factor_attribution.write_factor_attribution(run_dir)
            payload = json.loads((run_dir / "factor_attribution.json").read_text(encoding="utf-8"))

        self.assertEqual(factor_attribution.PASS, result.status)
        self.assertEqual("PeriodicFactorWeight", payload["strategy_name"])
        self.assertEqual(["momentum"], payload["factor_names"])
        self.assertEqual(2, payload["summary"]["period_count"])
        momentum = payload["summary"]["factor_summary"]["momentum"]
        self.assertEqual(2, momentum["periods_with_exposure"])
        self.assertEqual(2, momentum["periods_with_proxy"])
        self.assertEqual("1.200000", momentum["average_portfolio_exposure"])
        self.assertEqual("-0.040050", momentum["average_factor_spread_return"])
        self.assertEqual("-0.048060", momentum["average_proxy_contribution"])
        decomposition = payload["summary"]["return_decomposition"]
        self.assertEqual(2, decomposition["periods_with_active_return"])
        self.assertEqual(2, decomposition["periods_with_factor_proxy_total"])
        self.assertEqual("-0.006420", decomposition["average_active_return"])
        self.assertEqual("-0.048060", decomposition["average_factor_proxy_total_contribution"])
        self.assertEqual("0.041640", decomposition["average_active_residual_return"])
        self.assertEqual("17.026000", decomposition["total_equity_change"])
        self.assertEqual("18.000000", decomposition["total_holding_price_pnl"])
        self.assertEqual("-0.974000", decomposition["total_trade_cashflow_impact"])
        self.assertEqual("0.974000", decomposition["total_trading_costs"])
        self.assertEqual("0.000000", decomposition["total_unexplained_pnl"])
        first_period = payload["periods"][0]["factor_return_proxy"]["momentum"]
        self.assertEqual("1.200000", first_period["portfolio_exposure"])
        self.assertEqual("0.010000", first_period["factor_spread_return"])
        self.assertEqual("0.012000", first_period["proxy_contribution"])
        self.assertEqual(2, first_period["priced_symbol_count"])
        first_pnl = payload["periods"][0]["pnl_attribution"]
        self.assertEqual("9.000000", first_pnl["equity_change"])
        self.assertEqual("9.000000", first_pnl["holding_price_pnl"])
        self.assertEqual("0.000000", first_pnl["unexplained_pnl"])
        second_pnl = payload["periods"][1]["pnl_attribution"]
        self.assertEqual("8.026000", second_pnl["equity_change"])
        self.assertEqual("-0.974000", second_pnl["trade_cashflow_impact"])
        self.assertEqual("0.974000", second_pnl["trading_costs"])
        self.assertEqual("0.000000", second_pnl["unexplained_pnl"])
        first_decomposition = payload["periods"][0]["return_decomposition"]
        self.assertEqual("0.012000", first_decomposition["factor_proxy_total_contribution"])
        self.assertEqual("-0.012996", first_decomposition["active_residual_return"])
        self.assertEqual([], payload["gaps"])
        self.assertIn("PnL reconciliation", payload["interpretation"])

    def test_factor_attribution_is_inconclusive_without_factors_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            result = simulate.run_simulation(FIXTURES / "valid_complete")
            simulate.export_run_artifacts(result, FIXTURES / "valid_complete", run_dir, run_id="no-factor-attribution-test")
            report = factor_attribution.write_factor_attribution(run_dir)
            payload = json.loads((run_dir / "factor_attribution.json").read_text(encoding="utf-8"))

        self.assertEqual(factor_attribution.INCONCLUSIVE, report.status)
        self.assertEqual("INCONCLUSIVE", payload["status"])
        self.assertIn("factors.csv missing; factor attribution is unavailable.", payload["gaps"])


class FactorAttributionCliTests(unittest.TestCase):
    def test_factor_attribution_cli_writes_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_factor_run(Path(tmp))
            completed = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.factor_attribution", str(run_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            payload = json.loads((run_dir / "factor_attribution.json").read_text(encoding="utf-8"))

        self.assertEqual(0, completed.returncode)
        self.assertIn("FACTOR ATTRIBUTION STATUS: PASS", completed.stdout)
        self.assertEqual("PASS", payload["status"])


if __name__ == "__main__":
    unittest.main()
