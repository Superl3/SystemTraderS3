from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from system_trading_s3 import simulate


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


VALID_MARKET = "timestamp,symbol,price\n2026-01-01T09:30:00,SIM,100\n2026-01-02T09:30:00,SIM,101\n"


def write_file(root: Path, name: str, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(content, encoding="utf-8", newline="")


class SimulationTests(unittest.TestCase):
    def test_simulation_runs_on_valid_complete(self) -> None:
        result = simulate.run_simulation(FIXTURES / "valid_complete")
        self.assertEqual(simulate.PASS, result.status)
        self.assertEqual("PASS", result.audit_status)
        self.assertEqual(2, result.order_count)
        self.assertEqual(2, result.fill_count)
        self.assertEqual(Decimal("100002"), result.final_cash)
        self.assertEqual({}, result.final_positions)
        self.assertEqual(Decimal("100002"), result.final_equity)

    def test_audit_inconclusive_does_not_block_valid_market_feed(self) -> None:
        result = simulate.run_simulation(FIXTURES / "valid_minimal")
        self.assertEqual(simulate.PASS, result.status)
        self.assertEqual("INCONCLUSIVE", result.audit_status)
        self.assertEqual(2, result.fill_count)

    def test_audit_fail_does_not_block_valid_market_feed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "market_prices.csv", VALID_MARKET)
            result = simulate.run_simulation(root)
        self.assertEqual(simulate.PASS, result.status)
        self.assertEqual("FAIL", result.audit_status)
        self.assertEqual(2, result.fill_count)

    def test_first_step_creates_buy_order(self) -> None:
        strategy = simulate.BuyAndHoldOneUnitStrategy()
        account = simulate.SimulatedAccount(Decimal("100000"))
        event = simulate.MarketPriceEvent(
            timestamp=simulate.audit._parse_timestamp("2026-01-01T09:30:00", "datetime"),
            symbol="SIM",
            price=Decimal("100"),
        )
        orders = strategy.on_event(event, account)
        self.assertEqual(1, len(orders))
        self.assertEqual("buy", orders[0].side)
        self.assertEqual(Decimal("1"), orders[0].quantity)

    def test_on_finish_creates_sell_order_without_step_lookahead(self) -> None:
        strategy = simulate.BuyAndHoldOneUnitStrategy()
        account = simulate.SimulatedAccount(Decimal("100000"))
        execution = simulate.ExecutionSimulator()
        first = simulate.MarketPriceEvent(
            timestamp=simulate.audit._parse_timestamp("2026-01-01T09:30:00", "datetime"),
            symbol="SIM",
            price=Decimal("100"),
        )
        final = simulate.MarketPriceEvent(
            timestamp=simulate.audit._parse_timestamp("2026-01-02T09:30:00", "datetime"),
            symbol="SIM",
            price=Decimal("101"),
        )

        buy_orders = strategy.on_event(first, account)
        self.assertEqual([], strategy.on_event(final, account))
        account.apply_fill(execution.fill(first.timestamp, buy_orders[0]))

        sell_orders = strategy.on_finish(final, account)
        self.assertEqual(1, len(sell_orders))
        self.assertEqual("sell", sell_orders[0].side)
        self.assertEqual(Decimal("1"), sell_orders[0].quantity)

    def test_account_cash_changes_and_position_opens_and_closes(self) -> None:
        account = simulate.SimulatedAccount(Decimal("100000"))
        execution = simulate.ExecutionSimulator()
        timestamp = simulate.audit._parse_timestamp("2026-01-01T09:30:00", "datetime")

        buy = simulate.Order(side="buy", symbol="SIM", quantity=Decimal("1"), price=Decimal("100"))
        account.apply_fill(execution.fill(timestamp, buy))
        self.assertEqual(Decimal("99900"), account.cash)
        self.assertEqual(Decimal("1"), account.positions["SIM"])

        sell = simulate.Order(side="sell", symbol="SIM", quantity=Decimal("1"), price=Decimal("101"))
        account.apply_fill(execution.fill(timestamp, sell))
        self.assertEqual(Decimal("100001"), account.cash)
        self.assertEqual({}, account.positions)

    def test_fill_log_records_fills(self) -> None:
        feed = simulate.DataFeed.from_dataset(FIXTURES / "valid_complete")
        benchmark_feed = simulate.BenchmarkFeed.from_dataset(FIXTURES / "valid_complete", feed.events, Decimal("100000"))
        account = simulate.SimulatedAccount(Decimal("100000"))
        engine = simulate.SimulationEngine(
            feed,
            benchmark_feed,
            account,
            simulate.BuyAndHoldOneUnitStrategy(),
            simulate.ExecutionSimulator(),
        )
        engine.run()
        self.assertEqual(2, len(engine.fills))
        self.assertEqual(["buy", "sell"], [fill.side for fill in engine.fills])

    def test_insufficient_cash_fails_clearly(self) -> None:
        result = simulate.run_simulation(FIXTURES / "valid_complete", Decimal("50"))
        self.assertEqual(simulate.FAIL, result.status)
        self.assertIn("Insufficient cash", result.error or "")
        self.assertEqual(0, result.fill_count)

    def test_missing_market_prices_prevents_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = simulate.run_simulation(Path(tmp))
        self.assertEqual(simulate.FAIL, result.status)
        self.assertIn("market_prices.csv is missing", result.error or "")

    def test_invalid_market_price_prevents_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "market_prices.csv", "timestamp,symbol,price\n2026-01-01T09:30:00,SIM,abc\n2026-01-02T09:30:00,SIM,101\n")
            result = simulate.run_simulation(root)
        self.assertEqual(simulate.FAIL, result.status)
        self.assertIn("invalid finite price", result.error or "")

    def test_one_row_feed_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "market_prices.csv", "timestamp,symbol,price\n2026-01-01T09:30:00,SIM,100\n")
            result = simulate.run_simulation(root)
        self.assertEqual(simulate.FAIL, result.status)
        self.assertIn("at least two", result.error or "")

    def test_duplicate_timestamps_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "market_prices.csv", "timestamp,symbol,price\n2026-01-01T09:30:00,SIM,100\n2026-01-01T09:30:00,SIM,101\n")
            result = simulate.run_simulation(root)
        self.assertEqual(simulate.FAIL, result.status)
        self.assertIn("strictly increasing", result.error or "")

    def test_multi_symbol_feed_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "market_prices.csv", "timestamp,symbol,price\n2026-01-01T09:30:00,SIM,100\n2026-01-02T09:30:00,ALT,101\n")
            result = simulate.run_simulation(root)
        self.assertEqual(simulate.FAIL, result.status)
        self.assertIn("exactly one symbol", result.error or "")

    def test_missing_benchmark_prices_warns_without_blocking_simulation(self) -> None:
        result = simulate.run_simulation(FIXTURES / "valid_minimal")
        self.assertEqual(simulate.PASS, result.status)
        self.assertTrue(any("benchmark_prices.csv missing" in warning for warning in result.warnings))
        self.assertTrue(all(snapshot.benchmark_equity is None for snapshot in result.equity_curve))

    def test_benchmark_prices_forward_fill_to_market_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(
                root,
                "market_prices.csv",
                "timestamp,symbol,price\n"
                "2026-01-01T09:30:00,SIM,100\n"
                "2026-01-02T09:30:00,SIM,101\n"
                "2026-01-03T09:30:00,SIM,102\n",
            )
            write_file(
                root,
                "benchmark_prices.csv",
                "timestamp,symbol,price\n"
                "2026-01-01T09:30:00,BENCH,100\n"
                "2026-01-03T09:30:00,BENCH,110\n",
            )
            result = simulate.run_simulation(root, Decimal("1000"))
        self.assertEqual(simulate.PASS, result.status)
        self.assertEqual([], result.warnings)
        self.assertEqual([Decimal("100"), Decimal("100"), Decimal("110")], [row.benchmark_price for row in result.equity_curve])
        self.assertEqual([Decimal("1000"), Decimal("1000"), Decimal("1100")], [row.benchmark_equity for row in result.equity_curve])

    def test_initial_cash_default_and_override_work(self) -> None:
        default_result = simulate.run_simulation(FIXTURES / "valid_complete")
        override_result = simulate.run_simulation(FIXTURES / "valid_complete", Decimal("1000"))
        self.assertEqual(Decimal("100000"), default_result.initial_cash)
        self.assertEqual(Decimal("1000"), override_result.initial_cash)
        self.assertEqual(Decimal("1002"), override_result.final_cash)

    def test_strategy_registry_exposes_configured_strategies(self) -> None:
        self.assertEqual({"BuyAndHold", "MovingAverageCross"}, set(simulate.STRATEGY_REGISTRY))

    def test_load_simulation_config_parses_sample_config(self) -> None:
        config = simulate.load_simulation_config(FIXTURES / "sample_config.json")
        self.assertEqual(Decimal("1000"), config.initial_cash)
        self.assertEqual("BuyAndHold", config.strategy_name)
        self.assertEqual({"quantity": "1"}, config.strategy_params)
        self.assertEqual(Decimal("0.0005"), config.friction.fee_rate)
        self.assertEqual(Decimal("0.01"), config.friction.slippage_per_trade)
        self.assertEqual(Decimal("0.02"), config.risk_free_rate)

    def test_create_buy_and_hold_strategy_from_registry(self) -> None:
        strategy = simulate.create_strategy("BuyAndHold", {"quantity": "2"})
        self.assertEqual("BuyAndHold", strategy.name)
        event = simulate.MarketState(
            timestamp=simulate.audit._parse_timestamp("2026-01-01T09:30:00", "datetime"),
            symbol="SIM",
            price=Decimal("100"),
        )
        orders = strategy.on_data(event, simulate.AccountState(cash=Decimal("1000"), positions={}))
        self.assertEqual(1, len(orders))
        self.assertEqual(Decimal("2"), orders[0].quantity)
        self.assertEqual([], strategy.on_data(event, simulate.AccountState(cash=Decimal("1000"), positions={})))

    def test_moving_average_cross_generates_deterministic_orders(self) -> None:
        strategy = simulate.create_strategy(
            "MovingAverageCross",
            {"short_window": 1, "long_window": 2, "quantity": "1"},
        )
        first = simulate.MarketState(
            timestamp=simulate.audit._parse_timestamp("2026-01-01T09:30:00", "datetime"),
            symbol="SIM",
            price=Decimal("100"),
        )
        second = simulate.MarketState(
            timestamp=simulate.audit._parse_timestamp("2026-01-02T09:30:00", "datetime"),
            symbol="SIM",
            price=Decimal("101"),
        )
        third = simulate.MarketState(
            timestamp=simulate.audit._parse_timestamp("2026-01-03T09:30:00", "datetime"),
            symbol="SIM",
            price=Decimal("99"),
        )
        self.assertEqual([], strategy.on_data(first, simulate.AccountState(cash=Decimal("1000"), positions={})))
        buy_orders = strategy.on_data(second, simulate.AccountState(cash=Decimal("1000"), positions={}))
        self.assertEqual("buy", buy_orders[0].side)
        sell_orders = strategy.on_data(third, simulate.AccountState(cash=Decimal("899"), positions={"SIM": Decimal("1")}))
        self.assertEqual("sell", sell_orders[0].side)
        self.assertEqual(Decimal("1"), sell_orders[0].quantity)

    def test_run_simulation_accepts_configured_strategy(self) -> None:
        config = simulate.load_simulation_config(FIXTURES / "sample_config.json")
        strategy = simulate.create_strategy(config.strategy_name, config.strategy_params)
        result = simulate.run_simulation(
            FIXTURES / "valid_complete",
            config.initial_cash,
            strategy,
            config.friction,
            config.risk_free_rate,
        )
        self.assertEqual(simulate.PASS, result.status)
        self.assertEqual("BuyAndHold", result.strategy_name)
        self.assertEqual(1, result.order_count)
        self.assertEqual(1, result.fill_count)
        self.assertEqual(Decimal("899.94"), result.final_cash)
        self.assertEqual({"SIM": Decimal("1")}, result.final_positions)
        self.assertEqual(Decimal("1001.9400"), result.final_equity)
        self.assertEqual(Decimal("0.05"), result.total_fees)
        self.assertEqual(Decimal("0.01"), result.total_slippage)
        self.assertEqual(Decimal("0.05"), result.fills[0].fee)
        self.assertEqual(Decimal("0.01"), result.fills[0].slippage)

    def test_default_zero_friction_preserves_legacy_behavior(self) -> None:
        result = simulate.run_simulation(FIXTURES / "valid_complete")
        self.assertEqual(Decimal("100002"), result.final_cash)
        self.assertEqual(Decimal("0"), result.total_fees)
        self.assertEqual(Decimal("0"), result.total_slippage)

    def test_insufficient_cash_includes_friction(self) -> None:
        strategy = simulate.create_strategy("BuyAndHold", {"quantity": "1"})
        friction = simulate.FrictionModel(fee_rate=Decimal("0.0005"), slippage_per_trade=Decimal("0.01"))
        result = simulate.run_simulation(FIXTURES / "valid_complete", Decimal("100.05"), strategy, friction)
        self.assertEqual(simulate.FAIL, result.status)
        self.assertIn("Insufficient cash", result.error or "")


class SimulationCliTests(unittest.TestCase):
    def test_cli_output_contains_key_deterministic_lines(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "system_trading_s3.simulate", str(FIXTURES / "valid_complete")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode)
        lines = completed.stdout.splitlines()
        self.assertIn("SIMULATION STATUS: PASS", lines)
        self.assertIn("MVP0 AUDIT STATUS: PASS", lines)
        self.assertIn("STRATEGY: buy_and_hold_one_unit", lines)
        self.assertIn("INITIAL CASH: 100000", lines)
        self.assertIn("FINAL CASH: 100002", lines)
        self.assertIn("FINAL POSITIONS: none", lines)
        self.assertIn("ORDER COUNT: 2", lines)
        self.assertIn("FILL COUNT: 2", lines)
        self.assertIn("FINAL EQUITY: 100002", lines)

    def test_cli_initial_cash_override(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "system_trading_s3.simulate",
                "--initial-cash",
                "1000",
                str(FIXTURES / "valid_complete"),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode)
        self.assertIn("INITIAL CASH: 1000", completed.stdout)
        self.assertIn("FINAL CASH: 1002", completed.stdout)

    def test_cli_config_driven_run(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "system_trading_s3.simulate",
                str(FIXTURES / "valid_complete"),
                "--config",
                str(FIXTURES / "sample_config.json"),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode)
        self.assertIn("STRATEGY: BuyAndHold", completed.stdout)
        self.assertIn("INITIAL CASH: 1000", completed.stdout)
        self.assertIn("FINAL CASH: 899.9400", completed.stdout)
        self.assertIn("FINAL POSITIONS: SIM:1", completed.stdout)

    def test_cli_config_export_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "mvp4"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "system_trading_s3.simulate",
                    str(FIXTURES / "valid_complete"),
                    "--config",
                    str(FIXTURES / "sample_config.json"),
                    "--export-dir",
                    str(export_dir),
                    "--run-id",
                    "mvp4-test",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            validation = subprocess.run(
                [sys.executable, "-m", "system_trading_s3.validate_run", str(export_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(0, validation.returncode)
        self.assertIn("VALIDATION STATUS: PASS", validation.stdout)
        self.assertIn("STRATEGY: BuyAndHold", validation.stdout)


class SimulationExportTests(unittest.TestCase):
    def test_export_creates_all_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "run"
            result = simulate.run_simulation(FIXTURES / "valid_complete")
            simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id="mvp2-test")
            self.assertEqual(sorted(simulate.ARTIFACT_FILES), sorted(path.name for path in export_dir.iterdir()))

    def test_exported_equity_and_trades_can_be_audited_by_mvp0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "run"
            result = simulate.run_simulation(FIXTURES / "valid_complete")
            simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id="mvp2-test")
            audit_result = simulate.audit.audit_dataset(export_dir)
            self.assertEqual("INCONCLUSIVE", audit_result.status)
            self.assertFalse(any(issue.severity == simulate.audit.ERROR for issue in audit_result.issues))
            self.assertEqual({"optional_file_missing"}, {issue.check for issue in audit_result.issues})

    def test_run_manifest_contains_run_identity_and_assumptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "run"
            result = simulate.run_simulation(FIXTURES / "valid_complete", Decimal("1000"))
            simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id="mvp2-test")
            manifest = json.loads((export_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("mvp2-test", manifest["run_id"])
            self.assertEqual("buy_and_hold_one_unit", manifest["strategy_name"])
            self.assertEqual("1000", manifest["initial_cash"])
            self.assertEqual("0", manifest["risk_free_rate"])
            self.assertIn("immediate fills", manifest["simulation_assumptions"])
            self.assertEqual(["market_prices.csv", "benchmark_prices.csv"], manifest["input_files"])
            self.assertEqual("omitted_for_determinism", manifest["generated_at_policy"])

    def test_account_summary_matches_final_account_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "run"
            result = simulate.run_simulation(FIXTURES / "valid_complete")
            simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id="mvp2-test")
            account_summary = json.loads((export_dir / "account_summary.json").read_text(encoding="utf-8"))
            equity_curve = (export_dir / "equity_curve.csv").read_text(encoding="utf-8").splitlines()
            self.assertEqual("PASS", account_summary["status"])
            self.assertEqual("100000", account_summary["initial_cash"])
            self.assertEqual("100002", account_summary["final_cash"])
            self.assertEqual("100002", account_summary["final_equity"])
            self.assertEqual({}, account_summary["final_positions"])
            self.assertEqual(2, account_summary["order_count"])
            self.assertEqual(2, account_summary["fill_count"])
            self.assertEqual(2, account_summary["trade_count"])
            self.assertEqual("0", account_summary["total_fees"])
            self.assertEqual("0", account_summary["total_slippage"])
            self.assertEqual(
                "timestamp,equity,cash,position_value,symbol,position_quantity,last_price,benchmark_price,benchmark_equity",
                equity_curve[0],
            )
            self.assertIn("2026-01-01,100000,99900,100,SIM,1,100,100,100000", equity_curve)

    def test_export_records_configured_friction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "run"
            config = simulate.load_simulation_config(FIXTURES / "sample_config.json")
            strategy = simulate.create_strategy(config.strategy_name, config.strategy_params)
            result = simulate.run_simulation(
                FIXTURES / "valid_complete",
                config.initial_cash,
                strategy,
                config.friction,
                config.risk_free_rate,
            )
            simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id="mvp5-test")
            account_summary = json.loads((export_dir / "account_summary.json").read_text(encoding="utf-8"))
            manifest = json.loads((export_dir / "run_manifest.json").read_text(encoding="utf-8"))
            fills = (export_dir / "fills.csv").read_text(encoding="utf-8").splitlines()
            trades = (export_dir / "trades.csv").read_text(encoding="utf-8").splitlines()

            self.assertEqual("899.9400", account_summary["final_cash"])
            self.assertEqual("1001.9400", account_summary["final_equity"])
            self.assertEqual("0.0500", account_summary["total_fees"])
            self.assertEqual("0.01", account_summary["total_slippage"])
            self.assertEqual({"fee_rate": "0.0005", "slippage_per_trade": "0.01", "total_fees": "0.0500", "total_slippage": "0.01"}, manifest["friction"])
            self.assertEqual("0.02", manifest["risk_free_rate"])
            self.assertIn("F000001,O000001,2026-01-01T09:30:00,SIM,buy,1,100,0.0500,0.01", fills)
            self.assertIn("2026-01-01T09:30:00,T000001,market_follow,buy,1,100,0.0600,,SIM,BuyAndHold,market_follow,O000001,F000001", trades)

    def test_orders_and_fills_contain_expected_buy_and_sell_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "run"
            result = simulate.run_simulation(FIXTURES / "valid_complete")
            simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id="mvp2-test")
            orders = (export_dir / "orders.csv").read_text(encoding="utf-8").splitlines()
            fills = (export_dir / "fills.csv").read_text(encoding="utf-8").splitlines()
            self.assertEqual("order_id,timestamp,symbol,side,quantity,requested_price,status", orders[0])
            self.assertIn("O000001,2026-01-01T09:30:00,SIM,buy,1,100,filled", orders)
            self.assertIn("O000002,2026-01-03T09:30:00,SIM,sell,1,102,filled", orders)
            self.assertEqual("fill_id,order_id,timestamp,symbol,side,quantity,fill_price,fee,slippage", fills[0])
            self.assertIn("F000001,O000001,2026-01-01T09:30:00,SIM,buy,1,100,0,0", fills)
            self.assertIn("F000002,O000002,2026-01-03T09:30:00,SIM,sell,1,102,0,0", fills)

    def test_repeated_export_with_same_run_id_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_a = root / "run-a"
            export_b = root / "run-b"
            result_a = simulate.run_simulation(FIXTURES / "valid_complete")
            result_b = simulate.run_simulation(FIXTURES / "valid_complete")
            simulate.export_run_artifacts(result_a, FIXTURES / "valid_complete", export_a, run_id="same-run")
            simulate.export_run_artifacts(result_b, FIXTURES / "valid_complete", export_b, run_id="same-run")
            for file_name in simulate.ARTIFACT_FILES:
                self.assertEqual(
                    (export_a / file_name).read_bytes(),
                    (export_b / file_name).read_bytes(),
                    file_name,
                )

    def test_export_fails_on_existing_non_empty_dir_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "run"
            export_dir.mkdir()
            (export_dir / "old.txt").write_text("old", encoding="utf-8")
            result = simulate.run_simulation(FIXTURES / "valid_complete")
            with self.assertRaises(simulate.SimulationExportError):
                simulate.export_run_artifacts(result, FIXTURES / "valid_complete", export_dir, run_id="mvp2-test")
            self.assertEqual("old", (export_dir / "old.txt").read_text(encoding="utf-8"))

    def test_overwrite_replaces_previous_generated_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "run"
            export_dir.mkdir()
            (export_dir / "old.txt").write_text("old", encoding="utf-8")
            result = simulate.run_simulation(FIXTURES / "valid_complete")
            simulate.export_run_artifacts(
                result,
                FIXTURES / "valid_complete",
                export_dir,
                run_id="mvp2-test",
                overwrite=True,
            )
            self.assertFalse((export_dir / "old.txt").exists())
            self.assertEqual(sorted(simulate.ARTIFACT_FILES), sorted(path.name for path in export_dir.iterdir()))

    def test_cli_simulation_failure_does_not_leave_partial_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "failed-run"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "system_trading_s3.simulate",
                    "--initial-cash",
                    "50",
                    str(FIXTURES / "valid_complete"),
                    "--export-dir",
                    str(export_dir),
                    "--run-id",
                    "mvp2-fail",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertIn("SIMULATION STATUS: FAIL", completed.stdout)
            self.assertFalse(export_dir.exists())

    def test_cli_export_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "run"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "system_trading_s3.simulate",
                    str(FIXTURES / "valid_complete"),
                    "--export-dir",
                    str(export_dir),
                    "--run-id",
                    "mvp2-test",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            self.assertIn("SIMULATION STATUS: PASS", completed.stdout)
            self.assertTrue((export_dir / "run_manifest.json").exists())
            account_summary = json.loads((export_dir / "account_summary.json").read_text(encoding="utf-8"))
            self.assertIn(f"FINAL CASH: {account_summary['final_cash']}", completed.stdout)
            self.assertIn(f"FINAL EQUITY: {account_summary['final_equity']}", completed.stdout)
            self.assertIn(f"ORDER COUNT: {account_summary['order_count']}", completed.stdout)
            self.assertIn(f"FILL COUNT: {account_summary['fill_count']}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
