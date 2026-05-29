from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from system_trading_s3 import simulate
from system_trading_s3 import validate_run


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def export_fixture_run(root: Path, run_id: str = "mvp3-test") -> Path:
    export_dir = root / "run"
    result = simulate.run_simulation(FIXTURES / "valid_complete")
    simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id=run_id)
    return export_dir


def export_friction_run(root: Path, run_id: str = "mvp5-test") -> Path:
    export_dir = root / "run"
    config = simulate.load_simulation_config(FIXTURES / "sample_config.json")
    strategy = simulate.create_strategy(config.strategy_name, config.strategy_params)
    result = simulate.run_simulation(
        FIXTURES / "valid_complete",
        config.initial_cash,
        strategy,
        config.friction,
        config.risk_free_rate,
    )
    simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id=run_id)
    return export_dir


def export_multisymbol_run(root: Path, run_id: str = "mvp7-test") -> Path:
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
    simulate.export_run_artifacts(result, FIXTURES / "valid_multisymbol", export_dir, run_id=run_id)
    return export_dir


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def rewrite_csv(path: Path, mutate_row) -> None:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0].keys()) if rows else []
    for row in rows:
        mutate_row(row)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class ValidateRunTests(unittest.TestCase):
    def test_validate_run_passes_on_fresh_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp))
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.PASS, result.status)
        self.assertEqual("mvp3-test", result.run_id)
        self.assertEqual("buy_and_hold_one_unit", result.strategy_name)
        self.assertEqual(2, result.order_count)
        self.assertEqual(2, result.fill_count)
        self.assertEqual(Decimal("100002"), result.replayed_final_cash)
        self.assertEqual({}, result.replayed_final_positions)
        self.assertEqual(Decimal("100002"), result.replayed_final_equity)
        self.assertEqual("INCONCLUSIVE", result.artifact_audit_status)

    def test_validate_run_passes_on_friction_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_friction_run(Path(tmp))
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.PASS, result.status)
        self.assertEqual("mvp5-test", result.run_id)
        self.assertEqual("EqualWeightRebalance", result.strategy_name)
        self.assertEqual(1, result.order_count)
        self.assertEqual(1, result.fill_count)
        self.assertEqual(Decimal("99.54"), result.replayed_final_cash)
        self.assertEqual({"SIM": Decimal("9")}, result.replayed_final_positions)
        self.assertEqual(Decimal("1017.5400"), result.replayed_final_equity)

    def test_validate_run_passes_on_multisymbol_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_multisymbol_run(Path(tmp))
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.PASS, result.status)
        self.assertEqual("mvp7-test", result.run_id)
        self.assertEqual(2, result.order_count)
        self.assertEqual(2, result.fill_count)
        self.assertEqual(Decimal("49.5050"), result.replayed_final_cash)
        self.assertEqual({"AAA": Decimal("5"), "BBB": Decimal("9")}, result.replayed_final_positions)
        self.assertEqual(Decimal("1054.5050"), result.replayed_final_equity)

    def test_missing_required_artifact_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp))
            (run_dir / "orders.csv").unlink()
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.FAIL, result.status)
        self.assertIn("orders.csv", result.errors[0])

    def test_fill_referencing_unknown_order_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp))
            rewrite_csv(run_dir / "fills.csv", lambda row: row.update({"order_id": "UNKNOWN"}) if row["fill_id"] == "F000001" else None)
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.FAIL, result.status)
        self.assertTrue(any("unknown order_id" in error for error in result.errors))

    def test_sell_more_than_held_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp))
            rewrite_csv(run_dir / "fills.csv", lambda row: row.update({"quantity": "2"}) if row["side"] == "sell" else None)
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.FAIL, result.status)
        self.assertTrue(any("sells more than held" in error for error in result.errors))

    def test_account_summary_final_cash_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp))
            summary = read_json(run_dir / "account_summary.json")
            summary["final_cash"] = "999999"
            write_json(run_dir / "account_summary.json", summary)
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.FAIL, result.status)
        self.assertTrue(any("final cash" in error for error in result.errors))

    def test_friction_total_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_friction_run(Path(tmp))
            summary = read_json(run_dir / "account_summary.json")
            summary["total_fees"] = "999"
            write_json(run_dir / "account_summary.json", summary)
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.FAIL, result.status)
        self.assertTrue(any("total_fees" in error for error in result.errors))

    def test_negative_fill_cost_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_friction_run(Path(tmp))
            rewrite_csv(run_dir / "fills.csv", lambda row: row.update({"fee": "-1"}) if row["fill_id"] == "F000001" else None)
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.FAIL, result.status)
        self.assertTrue(any("negative fee" in error for error in result.errors))

    def test_account_summary_final_positions_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp))
            summary = read_json(run_dir / "account_summary.json")
            summary["final_positions"] = {"SIM": "1"}
            write_json(run_dir / "account_summary.json", summary)
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.FAIL, result.status)
        self.assertTrue(any("final positions" in error for error in result.errors))

    def test_final_equity_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp))
            summary = read_json(run_dir / "account_summary.json")
            summary["final_equity"] = "999999"
            write_json(run_dir / "account_summary.json", summary)
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.FAIL, result.status)
        self.assertTrue(any("final equity" in error for error in result.errors))

    def test_audit_summary_fail_causes_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp))
            audit_summary = read_json(run_dir / "audit_summary.json")
            audit_summary["audit_status"] = "FAIL"
            write_json(run_dir / "audit_summary.json", audit_summary)
            result = validate_run.validate_run_artifacts(run_dir)
        self.assertEqual(validate_run.FAIL, result.status)
        self.assertTrue(any("audit_status" in error for error in result.errors))

    def test_repeated_validation_output_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp))
            first = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.validate_run", str(run_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            second = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.validate_run", str(run_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(0, first.returncode)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual("", first.stderr)
        self.assertEqual("", second.stderr)

    def test_cli_passes_on_fresh_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = export_fixture_run(Path(tmp), run_id="cli-test")
            completed = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.validate_run", str(run_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(0, completed.returncode)
        self.assertIn("VALIDATION STATUS: PASS", completed.stdout)
        self.assertIn("RUN ID: cli-test", completed.stdout)
        self.assertIn("REPLAYED FINAL CASH: 100002", completed.stdout)
        self.assertIn("ARTIFACT AUDIT STATUS: INCONCLUSIVE", completed.stdout)

    def test_cli_fails_on_modified_artifact_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = export_fixture_run(root)
            broken = root / "broken"
            shutil.copytree(run_dir, broken)
            (broken / "run_manifest.json").unlink()
            completed = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.validate_run", str(broken)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(1, completed.returncode)
        self.assertIn("VALIDATION STATUS: FAIL", completed.stdout)


if __name__ == "__main__":
    unittest.main()
