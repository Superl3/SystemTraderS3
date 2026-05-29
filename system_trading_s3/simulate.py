"""Thin paper-trading simulation loop for simulated market data."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from system_trading_s3 import audit


PASS = "PASS"
FAIL = "FAIL"
DEFAULT_INITIAL_CASH = Decimal("100000")
ONE_UNIT = Decimal("1")
STRATEGY_NAME = "buy_and_hold_one_unit"
DEFAULT_RUN_ID = "default"
SIMULATION_PRESET_NAME = "market_follow"
RUN_ARTIFACT_SCHEMA_VERSION = "mvp2.run_artifacts.v1"
ARTIFACT_FILES = [
    "run_manifest.json",
    "equity_curve.csv",
    "trades.csv",
    "orders.csv",
    "fills.csv",
    "account_summary.json",
    "audit_summary.json",
]


class SimulationInputError(Exception):
    """Raised when simulation input data is invalid."""


class SimulationExecutionError(Exception):
    """Raised when the simulated account cannot apply a fill."""


class SimulationExportError(Exception):
    """Raised when deterministic run artifacts cannot be exported."""


@dataclass(frozen=True)
class MarketPriceEvent:
    timestamp: datetime
    symbol: str
    price: Decimal


@dataclass(frozen=True)
class Order:
    side: str
    symbol: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    side: str
    symbol: str
    quantity: Decimal
    price: Decimal
    order_id: str = ""
    fill_id: str = ""


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    timestamp: datetime
    order: Order
    status: str


@dataclass(frozen=True)
class AccountSnapshot:
    timestamp: datetime
    equity: Decimal
    cash: Decimal
    position_value: Decimal
    symbol: str
    position_quantity: Decimal
    last_price: Decimal


@dataclass(frozen=True)
class SimulationResult:
    status: str
    dataset: str
    audit_status: str
    strategy_name: str
    initial_cash: Decimal
    final_cash: Decimal | None
    final_positions: dict[str, Decimal]
    order_count: int
    fill_count: int
    final_equity: Decimal | None
    fills: list[Fill]
    orders: list[OrderRecord]
    equity_curve: list[AccountSnapshot]
    error: str | None = None


class DataFeed:
    """Validated one-symbol simulated price feed."""

    def __init__(self, events: list[MarketPriceEvent]) -> None:
        self._events = events

    @classmethod
    def from_dataset(cls, dataset_dir: Path | str) -> "DataFeed":
        path = Path(dataset_dir) / "market_prices.csv"
        if not path.exists():
            raise SimulationInputError("market_prices.csv is missing.")
        if not path.is_file():
            raise SimulationInputError("market_prices.csv must be a file.")

        events = _read_market_prices(path)
        if len(events) < 2:
            raise SimulationInputError("market_prices.csv must contain at least two price rows.")

        symbols = sorted({event.symbol for event in events})
        if len(symbols) != 1:
            raise SimulationInputError("market_prices.csv must contain exactly one symbol.")

        previous: datetime | None = None
        for event in events:
            if previous is not None:
                if not audit._timestamps_are_comparable(previous, event.timestamp):
                    raise SimulationInputError("market_prices.csv cannot mix timezone-aware and timezone-naive timestamps.")
                if event.timestamp <= previous:
                    raise SimulationInputError("market_prices.csv timestamps must be strictly increasing.")
            previous = event.timestamp

        return cls(events)

    def __iter__(self) -> Iterable[MarketPriceEvent]:
        return iter(self._events)

    @property
    def events(self) -> list[MarketPriceEvent]:
        return list(self._events)


class SimulatedAccount:
    def __init__(self, initial_cash: Decimal) -> None:
        if not initial_cash.is_finite():
            raise SimulationInputError("initial cash must be finite.")
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: dict[str, Decimal] = {}

    def apply_fill(self, fill: Fill) -> None:
        notional = fill.quantity * fill.price
        current_position = self.positions.get(fill.symbol, Decimal("0"))

        if fill.side == "buy":
            if self.cash < notional:
                raise SimulationExecutionError(
                    f"Insufficient cash to buy {format_decimal(fill.quantity)} {fill.symbol} at {format_decimal(fill.price)}."
                )
            self.cash -= notional
            self.positions[fill.symbol] = current_position + fill.quantity
            return

        if fill.side == "sell":
            if current_position < fill.quantity:
                raise SimulationExecutionError(
                    f"Insufficient position to sell {format_decimal(fill.quantity)} {fill.symbol}."
                )
            self.cash += notional
            new_position = current_position - fill.quantity
            if new_position == 0:
                self.positions.pop(fill.symbol, None)
            else:
                self.positions[fill.symbol] = new_position
            return

        raise SimulationExecutionError(f"Unsupported order side: {fill.side}.")

    def final_equity(self, last_prices: dict[str, Decimal]) -> Decimal:
        equity = self.cash
        for symbol in sorted(self.positions):
            if symbol not in last_prices:
                raise SimulationExecutionError(f"Missing final price for open position: {symbol}.")
            equity += self.positions[symbol] * last_prices[symbol]
        return equity


class ExecutionSimulator:
    def fill(self, timestamp: datetime, order: Order, order_id: str = "", fill_id: str = "") -> Fill:
        return Fill(
            timestamp=timestamp,
            side=order.side,
            symbol=order.symbol,
            quantity=order.quantity,
            price=order.price,
            order_id=order_id,
            fill_id=fill_id,
        )


class BuyAndHoldOneUnitStrategy:
    name = STRATEGY_NAME

    def __init__(self) -> None:
        self._bought = False

    def on_event(self, event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        del account
        if self._bought:
            return []
        self._bought = True
        return [Order(side="buy", symbol=event.symbol, quantity=ONE_UNIT, price=event.price)]

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        held_quantity = account.positions.get(final_event.symbol, Decimal("0"))
        if held_quantity <= 0:
            return []
        return [Order(side="sell", symbol=final_event.symbol, quantity=held_quantity, price=final_event.price)]


class SimulationEngine:
    def __init__(
        self,
        feed: DataFeed,
        account: SimulatedAccount,
        strategy: BuyAndHoldOneUnitStrategy,
        execution: ExecutionSimulator,
    ) -> None:
        self.feed = feed
        self.account = account
        self.strategy = strategy
        self.execution = execution
        self.order_count = 0
        self.orders: list[OrderRecord] = []
        self.fills: list[Fill] = []
        self.equity_curve: list[AccountSnapshot] = []
        self.last_prices: dict[str, Decimal] = {}

    def run(self) -> None:
        events = self.feed.events
        final_event = events[-1]

        for event in events:
            self.last_prices[event.symbol] = event.price
            self._execute_orders(event, self.strategy.on_event(event, self.account))
            if event == final_event:
                self._execute_orders(final_event, self.strategy.on_finish(final_event, self.account))
            self._record_account_snapshot(event)

    def _execute_orders(self, event: MarketPriceEvent, orders: list[Order]) -> None:
        for order in orders:
            order_id = f"O{len(self.orders) + 1:06d}"
            fill_id = f"F{len(self.fills) + 1:06d}"
            self.orders.append(OrderRecord(order_id=order_id, timestamp=event.timestamp, order=order, status="filled"))
            self.order_count += 1
            fill = self.execution.fill(event.timestamp, order, order_id=order_id, fill_id=fill_id)
            self.account.apply_fill(fill)
            self.fills.append(fill)

    def _record_account_snapshot(self, event: MarketPriceEvent) -> None:
        position_quantity = self.account.positions.get(event.symbol, Decimal("0"))
        position_value = position_quantity * event.price
        self.equity_curve.append(
            AccountSnapshot(
                timestamp=event.timestamp,
                equity=self.account.cash + position_value,
                cash=self.account.cash,
                position_value=position_value,
                symbol=event.symbol,
                position_quantity=position_quantity,
                last_price=event.price,
            )
        )


def run_simulation(dataset_dir: Path | str, initial_cash: Decimal = DEFAULT_INITIAL_CASH) -> SimulationResult:
    dataset_path = Path(dataset_dir)
    audit_status = _audit_status_for_context(dataset_path)

    try:
        feed = DataFeed.from_dataset(dataset_path)
        account = SimulatedAccount(initial_cash)
        strategy = BuyAndHoldOneUnitStrategy()
        engine = SimulationEngine(feed, account, strategy, ExecutionSimulator())
        engine.run()
        final_equity = account.final_equity(engine.last_prices)
        return SimulationResult(
            status=PASS,
            dataset=str(dataset_path),
            audit_status=audit_status,
            strategy_name=strategy.name,
            initial_cash=initial_cash,
            final_cash=account.cash,
            final_positions=dict(sorted(account.positions.items())),
            order_count=engine.order_count,
            fill_count=len(engine.fills),
            final_equity=final_equity,
            fills=list(engine.fills),
            orders=list(engine.orders),
            equity_curve=list(engine.equity_curve),
        )
    except (SimulationInputError, SimulationExecutionError) as exc:
        return SimulationResult(
            status=FAIL,
            dataset=str(dataset_path),
            audit_status=audit_status,
            strategy_name=STRATEGY_NAME,
            initial_cash=initial_cash,
            final_cash=None,
            final_positions={},
            order_count=0,
            fill_count=0,
            final_equity=None,
            fills=[],
            orders=[],
            equity_curve=[],
            error=str(exc),
        )


def format_result(result: SimulationResult) -> str:
    lines = [
        f"SIMULATION STATUS: {result.status}",
        f"DATASET: {result.dataset}",
        f"MVP0 AUDIT STATUS: {result.audit_status}",
        f"STRATEGY: {result.strategy_name}",
        f"INITIAL CASH: {format_decimal(result.initial_cash)}",
        f"FINAL CASH: {_format_optional_decimal(result.final_cash)}",
        f"FINAL POSITIONS: {format_positions(result.final_positions)}",
        f"ORDER COUNT: {result.order_count}",
        f"FILL COUNT: {result.fill_count}",
        f"FINAL EQUITY: {_format_optional_decimal(result.final_equity)}",
    ]
    if result.error is not None:
        lines.append(f"ERROR: {result.error}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a thin simulated paper-trading loop.")
    parser.add_argument("dataset_dir", type=Path, help="Directory containing market_prices.csv.")
    parser.add_argument(
        "--initial-cash",
        default=str(DEFAULT_INITIAL_CASH),
        type=_parse_initial_cash,
        help="Initial simulated cash. Defaults to 100000.",
    )
    parser.add_argument("--export-dir", type=Path, help="Write deterministic run artifacts to this directory.")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing non-empty export directory.")
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID, help="Deterministic run identifier for exported artifacts.")
    args = parser.parse_args(argv)

    if not args.dataset_dir.exists() or not args.dataset_dir.is_dir():
        print(f"dataset_dir must be an existing directory: {args.dataset_dir}", file=sys.stderr)
        return 2

    try:
        result = run_simulation(args.dataset_dir, args.initial_cash)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary.
        print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    print(format_result(result))
    if args.export_dir is not None:
        if result.status != PASS:
            return 1
        try:
            export_run_artifacts(
                result=result,
                dataset_dir=args.dataset_dir,
                export_dir=args.export_dir,
                run_id=args.run_id,
                overwrite=args.overwrite,
            )
        except SimulationExportError as exc:
            print(f"EXPORT ERROR: {exc}", file=sys.stderr)
            return 1
    return 0 if result.status == PASS else 1


def export_run_artifacts(
    result: SimulationResult,
    dataset_dir: Path | str,
    export_dir: Path | str,
    run_id: str = DEFAULT_RUN_ID,
    overwrite: bool = False,
) -> None:
    if result.status != PASS:
        raise SimulationExportError("simulation must pass before artifacts can be exported.")

    target = Path(export_dir)
    if target.exists() and not target.is_dir():
        raise SimulationExportError(f"export path exists and is not a directory: {target}")
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise SimulationExportError(f"export directory is non-empty: {target}")

    parent = target.parent if target.parent != Path("") else Path(".")
    parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=str(parent)))
    try:
        _write_run_artifacts(temp_root, result, Path(dataset_dir), run_id)
        if target.exists():
            shutil.rmtree(target)
        temp_root.replace(target)
    except Exception as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        if isinstance(exc, SimulationExportError):
            raise
        raise SimulationExportError(str(exc)) from exc


def _read_market_prices(path: Path) -> list[MarketPriceEvent]:
    rows: list[MarketPriceEvent] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                raw_headers = next(reader)
            except StopIteration:
                raise SimulationInputError("market_prices.csv is empty.")

            headers = [header.strip() for header in raw_headers]
            for required in ["timestamp", "symbol", "price"]:
                if required not in headers:
                    raise SimulationInputError(f"market_prices.csv missing required header: {required}.")

            for row_number, raw_row in enumerate(reader, start=2):
                if not raw_row or all(audit._is_blank(cell) for cell in raw_row):
                    continue
                if len(raw_row) > len(headers):
                    raise SimulationInputError(f"market_prices.csv row {row_number} has more fields than headers.")
                row = dict(zip(headers, raw_row + [""] * (len(headers) - len(raw_row))))

                timestamp_text = row.get("timestamp", "")
                symbol = row.get("symbol", "").strip()
                price_text = row.get("price", "")

                if audit._is_blank(timestamp_text):
                    raise SimulationInputError(f"market_prices.csv row {row_number} missing timestamp.")
                if symbol == "":
                    raise SimulationInputError(f"market_prices.csv row {row_number} missing symbol.")
                if audit._is_blank(price_text):
                    raise SimulationInputError(f"market_prices.csv row {row_number} missing price.")

                timestamp = audit._parse_timestamp(timestamp_text, "datetime")
                if not isinstance(timestamp, datetime):
                    raise SimulationInputError(f"market_prices.csv row {row_number} has invalid ISO datetime.")

                price = audit._parse_decimal(price_text)
                if price is None:
                    raise SimulationInputError(f"market_prices.csv row {row_number} has invalid finite price.")

                rows.append(MarketPriceEvent(timestamp=timestamp, symbol=symbol, price=price))
    except UnicodeDecodeError as exc:
        raise SimulationInputError(f"market_prices.csv decode error: {exc}") from exc
    except csv.Error as exc:
        raise SimulationInputError(f"market_prices.csv parse error: {exc}") from exc
    return rows


def _write_run_artifacts(export_dir: Path, result: SimulationResult, dataset_dir: Path, run_id: str) -> None:
    _write_json(export_dir / "run_manifest.json", _manifest_payload(result, dataset_dir, run_id))
    _write_equity_curve(export_dir / "equity_curve.csv", result)
    _write_trades(export_dir / "trades.csv", result)
    _write_orders(export_dir / "orders.csv", result)
    _write_fills(export_dir / "fills.csv", result)
    _write_json(export_dir / "account_summary.json", _account_summary_payload(result))

    audit_result = audit.audit_dataset(export_dir)
    _write_json(export_dir / "audit_summary.json", _audit_summary_payload(audit_result))


def _manifest_payload(result: SimulationResult, dataset_dir: Path, run_id: str) -> dict[str, object]:
    return {
        "schema_version": RUN_ARTIFACT_SCHEMA_VERSION,
        "run_id": run_id,
        "dataset_dir": str(dataset_dir),
        "strategy_name": result.strategy_name,
        "initial_cash": format_decimal(result.initial_cash),
        "input_files": ["market_prices.csv"],
        "simulation_assumptions": [
            "immediate fills",
            "zero fee",
            "zero slippage",
            "one symbol only",
            "one hard-coded buy/hold/sell strategy",
        ],
        "generated_at_policy": "omitted_for_determinism",
    }


def _account_summary_payload(result: SimulationResult) -> dict[str, object]:
    return {
        "initial_cash": format_decimal(result.initial_cash),
        "final_cash": _format_optional_decimal(result.final_cash),
        "final_equity": _format_optional_decimal(result.final_equity),
        "final_positions": {symbol: format_decimal(quantity) for symbol, quantity in sorted(result.final_positions.items())},
        "order_count": result.order_count,
        "fill_count": result.fill_count,
        "trade_count": len(result.fills),
        "status": result.status,
    }


def _audit_summary_payload(audit_result: audit.AuditResult) -> dict[str, object]:
    error_count = sum(1 for issue in audit_result.issues if issue.severity == audit.ERROR)
    gap_count = sum(1 for issue in audit_result.issues if issue.severity == audit.GAP)
    return {
        "audit_status": audit_result.status,
        "optional_gaps_only": error_count == 0 and gap_count > 0,
        "required_generated_outputs_valid": error_count == 0,
        "issues": [issue.to_dict() for issue in audit_result.issues],
    }


def _write_equity_curve(path: Path, result: SimulationResult) -> None:
    headers = ["timestamp", "equity", "cash", "position_value", "symbol", "position_quantity", "last_price"]
    rows = [
        [
            snapshot.timestamp.date().isoformat(),
            format_decimal(snapshot.equity),
            format_decimal(snapshot.cash),
            format_decimal(snapshot.position_value),
            snapshot.symbol,
            format_decimal(snapshot.position_quantity),
            format_decimal(snapshot.last_price),
        ]
        for snapshot in result.equity_curve
    ]
    _write_csv(path, headers, rows)


def _write_trades(path: Path, result: SimulationResult) -> None:
    headers = [
        "timestamp",
        "trade_id",
        "strategy",
        "side",
        "quantity",
        "price",
        "cost",
        "realized_pnl",
        "symbol",
        "strategy_name",
        "preset_name",
        "order_id",
        "fill_id",
    ]
    buy_prices: dict[str, Decimal] = {}
    rows: list[list[str]] = []
    for index, fill in enumerate(result.fills, start=1):
        realized_pnl = ""
        if fill.side == "buy":
            buy_prices[fill.symbol] = fill.price
        elif fill.side == "sell" and fill.symbol in buy_prices:
            realized_pnl = format_decimal((fill.price - buy_prices[fill.symbol]) * fill.quantity)
        rows.append(
            [
                fill.timestamp.isoformat(),
                f"T{index:06d}",
                SIMULATION_PRESET_NAME,
                fill.side,
                format_decimal(fill.quantity),
                format_decimal(fill.price),
                "0",
                realized_pnl,
                fill.symbol,
                result.strategy_name,
                SIMULATION_PRESET_NAME,
                fill.order_id,
                fill.fill_id,
            ]
        )
    _write_csv(path, headers, rows)


def _write_orders(path: Path, result: SimulationResult) -> None:
    headers = ["order_id", "timestamp", "symbol", "side", "quantity", "requested_price", "status"]
    rows = [
        [
            record.order_id,
            record.timestamp.isoformat(),
            record.order.symbol,
            record.order.side,
            format_decimal(record.order.quantity),
            format_decimal(record.order.price),
            record.status,
        ]
        for record in result.orders
    ]
    _write_csv(path, headers, rows)


def _write_fills(path: Path, result: SimulationResult) -> None:
    headers = ["fill_id", "order_id", "timestamp", "symbol", "side", "quantity", "fill_price", "fee", "slippage"]
    rows = [
        [
            fill.fill_id,
            fill.order_id,
            fill.timestamp.isoformat(),
            fill.symbol,
            fill.side,
            format_decimal(fill.quantity),
            format_decimal(fill.price),
            "0",
            "0",
        ]
        for fill in result.fills
    ]
    _write_csv(path, headers, rows)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


def _audit_status_for_context(dataset_path: Path) -> str:
    try:
        return audit.audit_dataset(dataset_path).status
    except Exception:
        return "ERROR"


def _parse_initial_cash(value: str) -> Decimal:
    parsed = audit._parse_decimal(value)
    if parsed is None or parsed < 0:
        raise argparse.ArgumentTypeError("initial cash must be a finite nonnegative decimal.")
    return parsed


def format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _format_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    return format_decimal(value)


def format_positions(positions: dict[str, Decimal]) -> str:
    if not positions:
        return "none"
    return ", ".join(f"{symbol}:{format_decimal(quantity)}" for symbol, quantity in sorted(positions.items()))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
