from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from system_trading_s3 import factor_attribution
from system_trading_s3 import factor_risk_model
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
    simulate.export_run_artifacts(result, FIXTURES / "valid_multisymbol", export_dir, run_id="factor-risk-model-test")
    factor_attribution.write_factor_attribution(export_dir)
    return export_dir


def write_synthetic_two_factor_attribution(run_dir: Path) -> None:
    observations = [
        ("0.000000", "0.000000", "0.001000"),
        ("0.001000", "0.000000", "0.003000"),
        ("0.000000", "0.001000", "-0.002000"),
        ("0.002000", "0.001000", "0.002000"),
        ("0.001000", "0.002000", "-0.003000"),
        ("0.003000", "-0.001000", "0.010000"),
    ]
    periods = []
    for index, (quality, momentum, active_return) in enumerate(observations, start=1):
        periods.append(
            {
                "start_timestamp": f"2026-01-0{index}",
                "end_timestamp": f"2026-01-0{index + 1}",
                "strategy_return": active_return,
                "benchmark_return": "0.000000",
                "active_return": active_return,
                "factor_return_proxy": {
                    "momentum": {"proxy_contribution": momentum},
                    "quality": {"proxy_contribution": quality},
                },
            }
        )
    payload = {
        "schema_version": "mvp12.factor_attribution.v3",
        "status": "PASS",
        "run_id": "synthetic-factor-risk-model",
        "strategy_name": "SyntheticFactorStrategy",
        "factor_names": ["momentum", "quality"],
        "summary": {
            "period_count": len(periods),
            "factor_summary": {
                "momentum": {},
                "quality": {},
            },
        },
        "periods": periods,
        "gaps": [],
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "factor_attribution.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class FactorRiskModelTests(unittest.TestCase):
    def test_factor_risk_model_is_inconclusive_for_single_factor_demo_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_factor_run(Path(tmp))
            result = factor_risk_model.write_factor_risk_model(run_dir)
            payload = json.loads((run_dir / "factor_risk_model.json").read_text(encoding="utf-8"))

        self.assertEqual(factor_risk_model.INCONCLUSIVE, result.status)
        self.assertEqual("INCONCLUSIVE", payload["status"])
        self.assertEqual(["momentum"], payload["factor_names"])
        self.assertEqual(1, payload["factor_count"])
        self.assertEqual(2, payload["observation_count"])
        self.assertTrue(any("at least 2 factors" in gap for gap in payload["gaps"]))
        self.assertTrue(any("complete observations" in gap for gap in payload["gaps"]))

    def test_factor_risk_model_passes_for_synthetic_two_factor_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_synthetic_two_factor_attribution(run_dir)
            result = factor_risk_model.write_factor_risk_model(run_dir)
            payload = json.loads((run_dir / "factor_risk_model.json").read_text(encoding="utf-8"))

        self.assertEqual(factor_risk_model.PASS, result.status)
        self.assertEqual("PASS", payload["status"])
        self.assertEqual("active_return", payload["dependent_variable"])
        self.assertEqual(6, payload["observation_count"])
        self.assertEqual(2, payload["factor_count"])
        self.assertEqual({"intercept": "0.001000", "momentum": "-3.000000", "quality": "2.000000"}, payload["coefficients"])
        self.assertEqual("1.000000", payload["r_squared"])
        self.assertEqual("0.000000", payload["residual_summary"]["sum_squared_residual"])
        self.assertEqual([], payload["gaps"])
        self.assertIn("not a forecast", payload["interpretation"])


class FactorRiskModelCliTests(unittest.TestCase):
    def test_factor_risk_model_cli_writes_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_synthetic_two_factor_attribution(run_dir)
            completed = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.factor_risk_model", str(run_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            payload = json.loads((run_dir / "factor_risk_model.json").read_text(encoding="utf-8"))

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("FACTOR RISK MODEL STATUS: PASS", completed.stdout)
        self.assertEqual("PASS", payload["status"])


if __name__ == "__main__":
    unittest.main()
