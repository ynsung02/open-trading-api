from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from account import AccountBalance, AccountSnapshot, Position
from api_client import KISResponse
from config import Settings
from market_data import PriceQuote
from orders import OrderReceipt, OrderSide
from state import RuntimeState, RuntimeStateStore, TradeRecordWriter
from trader import SamsungAutoTrader, TraderState


SEOUL = ZoneInfo("Asia/Seoul")
FIXED_NOW = datetime(2026, 6, 22, 13, 0, 0, tzinfo=SEOUL)


class FakeApiClient:
    def __init__(self, responses: list[KISResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(self, *, path: str, tr_id: str, params: dict[str, object]) -> KISResponse:
        self.calls.append({"path": path, "tr_id": tr_id, "params": params})
        if not self.responses:
            raise AssertionError("No fake API response remains")
        return self.responses.pop(0)


class FakeAccountClient:
    def __init__(self, position: Position | None) -> None:
        self.position = position
        self.get_snapshot_calls = 0

    def get_snapshot(self) -> AccountSnapshot:
        self.get_snapshot_calls += 1
        positions = [] if self.position is None else [self.position]
        return AccountSnapshot(
            balance=AccountBalance(10_000_000, 10_000_000, 10_000_000, 0, 0),
            positions=positions,
        )

    def get_symbol_position(self, snapshot: AccountSnapshot, symbol: str) -> Position | None:
        return next((item for item in snapshot.positions if item.symbol == symbol), None)


class FakeMarketData:
    def __init__(self, price: int = 352_000) -> None:
        self.price = price
        self.calls = 0

    def get_current_price(self, symbol: str) -> PriceQuote:
        self.calls += 1
        return PriceQuote(symbol, self.price, self.price, self.price, self.price, 0, 0.0)


class FakeOrderClient:
    def __init__(self) -> None:
        self.buy_calls: list[tuple[str, int, int]] = []
        self.sell_calls: list[tuple[str, int, int]] = []

    def place_buy_order(self, symbol: str, quantity: int, price: int) -> OrderReceipt:
        self.buy_calls.append((symbol, quantity, price))
        order_no = f"{1000 + len(self.buy_calls):010d}"
        return OrderReceipt(order_no, symbol, OrderSide.BUY, quantity, price, {})

    def place_sell_order(self, symbol: str, quantity: int, price: int) -> OrderReceipt:
        self.sell_calls.append((symbol, quantity, price))
        order_no = f"{2000 + len(self.sell_calls):010d}"
        return OrderReceipt(order_no, symbol, OrderSide.SELL, quantity, price, {})


def order_row(
    *,
    order_no: str,
    side: str,
    requested_price: int,
    requested_qty: int = 1,
    filled_qty: int = 0,
    remaining_qty: int = 1,
    average_price: float = 0.0,
    rejected_qty: int = 0,
    cancelled: str = "N",
) -> dict[str, str]:
    return {
        "odno": order_no,
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02" if side == "BUY" else "01",
        "ord_qty": str(requested_qty),
        "ord_unpr": str(requested_price),
        "tot_ccld_qty": str(filled_qty),
        "avg_prvs": str(average_price),
        "rmn_qty": str(remaining_qty),
        "rjct_qty": str(rejected_qty),
        "cncl_yn": cancelled,
    }


def response(rows: list[dict[str, str]]) -> KISResponse:
    return KISResponse({"rt_cd": "0", "msg_cd": "", "msg1": "", "output1": rows})


class TraderStateMachineTests(TestCase):
    def _settings(self, root: Path) -> Settings:
        return Settings(
            account_no="12345678",
            account_prod="01",
            app_key="key",
            app_secret="secret",
            order_qty=1,
            order_offset_krw=2000,
            poll_interval_seconds=60,
            token_cache_path=root / "token_cache.json",
            runtime_state_path=root / "runtime_state.json",
            records_dir=root / "records",
            log_path=root / "trader.log",
        )

    def _trader(
        self,
        root: Path,
        *,
        api_responses: list[KISResponse],
        initial_state: RuntimeState | None = None,
        position: Position | None = None,
        resume_buy_order_no: str = "",
        max_round_trips: int = 1,
        clock=lambda: FIXED_NOW,
    ) -> tuple[SamsungAutoTrader, FakeApiClient, FakeOrderClient, FakeAccountClient]:
        settings = self._settings(root)
        store = RuntimeStateStore(settings.runtime_state_path)
        if initial_state is not None:
            store.save(initial_state)
        api = FakeApiClient(api_responses)
        account = FakeAccountClient(position)
        orders = FakeOrderClient()
        trader = SamsungAutoTrader(
            settings=settings,
            api_client=api,
            market_data=FakeMarketData(),
            account_client=account,
            order_client=orders,
            runtime_state_store=store,
            trade_recorder=TradeRecordWriter(settings.records_dir),
            max_round_trips=max_round_trips,
            resume_buy_order_no=resume_buy_order_no,
            resume_order_date="20260622" if resume_buy_order_no else "",
            poll_interval_seconds=60,
            logger=Mock(),
            clock=clock,
            sleep_fn=lambda _: None,
        )
        return trader, api, orders, account

    @staticmethod
    def _position() -> Position:
        return Position("005930", "삼성전자", 1, 1, 350000, 352000, 2000, 0.57)

    def test_resume_filled_buy_places_no_new_buy_then_one_sell(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            trader, _, orders, _ = self._trader(
                root,
                api_responses=[
                    response(
                        [
                            order_row(
                                order_no="0000001234",
                                side="BUY",
                                requested_price=350000,
                                filled_qty=1,
                                remaining_qty=0,
                                average_price=350000,
                            )
                        ]
                    ),
                    response([]),
                ],
                position=self._position(),
                resume_buy_order_no="0000001234",
                max_round_trips=3,
            )

            first = trader.step()
            second = trader.step()

            self.assertEqual(first.state, TraderState.HOLDING.value)
            self.assertEqual(second.state, TraderState.SELL_PENDING.value)
            self.assertEqual(orders.buy_calls, [])
            self.assertEqual(len(orders.sell_calls), 1)
            self.assertEqual(orders.sell_calls[0][1], 1)

    def test_buy_pending_does_not_submit_another_order(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            trader, _, orders, _ = self._trader(
                root,
                api_responses=[
                    response(
                        [
                            order_row(
                                order_no="0000001001",
                                side="BUY",
                                requested_price=350000,
                            )
                        ]
                    )
                ],
                resume_buy_order_no="0000001001",
            )

            result = trader.step()

            self.assertEqual(result.state, TraderState.BUY_PENDING.value)
            self.assertEqual(orders.buy_calls, [])
            self.assertEqual(orders.sell_calls, [])

    def test_sell_pending_does_not_submit_another_order(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = RuntimeState(
                state=TraderState.SELL_PENDING.value,
                symbol="005930",
                sell_order_no="0000002001",
                sell_order_date="20260622",
                requested_qty=1,
                requested_sell_price=354000,
                max_round_trips=1,
            )
            trader, _, orders, _ = self._trader(
                root,
                api_responses=[
                    response(
                        [
                            order_row(
                                order_no="0000002001",
                                side="SELL",
                                requested_price=354000,
                            )
                        ]
                    )
                ],
                initial_state=initial,
            )

            result = trader.step()

            self.assertEqual(result.state, TraderState.SELL_PENDING.value)
            self.assertEqual(orders.buy_calls, [])
            self.assertEqual(orders.sell_calls, [])

    def test_sell_fill_reaches_completed_target_and_records_profit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = RuntimeState(
                state=TraderState.SELL_PENDING.value,
                symbol="005930",
                buy_order_no="0000001001",
                sell_order_no="0000002001",
                sell_order_date="20260622",
                requested_qty=1,
                requested_buy_price=350000,
                requested_sell_price=354000,
                buy_filled_qty=1,
                buy_average_price=350000,
                max_round_trips=1,
                round_trip_id=1,
            )
            trader, _, _, _ = self._trader(
                root,
                api_responses=[
                    response(
                        [
                            order_row(
                                order_no="0000002001",
                                side="SELL",
                                requested_price=354000,
                                filled_qty=1,
                                remaining_qty=0,
                                average_price=354000,
                            )
                        ]
                    )
                ],
                initial_state=initial,
            )

            result = trader.step()

            self.assertEqual(result.state, TraderState.COMPLETED.value)
            self.assertEqual(result.completed_round_trips, 1)
            csv_path = root / "records" / "trades_20260622.csv"
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["side"], "SELL")
            self.assertEqual(rows[0]["realized_profit_krw"], "4000.0")

    def test_sell_fill_below_target_returns_to_idle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = RuntimeState(
                state=TraderState.SELL_PENDING.value,
                symbol="005930",
                sell_order_no="0000002001",
                sell_order_date="20260622",
                requested_qty=1,
                buy_average_price=350000,
                max_round_trips=3,
                round_trip_id=1,
            )
            trader, _, _, _ = self._trader(
                root,
                api_responses=[
                    response(
                        [
                            order_row(
                                order_no="0000002001",
                                side="SELL",
                                requested_price=354000,
                                filled_qty=1,
                                remaining_qty=0,
                                average_price=354000,
                            )
                        ]
                    )
                ],
                initial_state=initial,
                max_round_trips=3,
            )

            result = trader.step()

            self.assertEqual(result.state, TraderState.IDLE.value)
            self.assertEqual(result.completed_round_trips, 1)
            self.assertEqual(result.round_trip_id, 2)

    def test_multiple_active_orders_fail_without_new_post(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            trader, _, orders, account = self._trader(
                root,
                api_responses=[
                    response(
                        [
                            order_row(order_no="1", side="BUY", requested_price=350000),
                            order_row(order_no="2", side="SELL", requested_price=354000),
                        ]
                    )
                ],
            )

            result = trader.step()

            self.assertEqual(result.state, TraderState.FAILED.value)
            self.assertEqual(orders.buy_calls, [])
            self.assertEqual(orders.sell_calls, [])
            self.assertEqual(account.get_snapshot_calls, 0)


    def test_two_round_trips_run_without_duplicate_orders(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_responses = [
                response([]),
                response([order_row(order_no="0000001001", side="BUY", requested_price=350000, filled_qty=1, remaining_qty=0, average_price=350000)]),
                response([]),
                response([order_row(order_no="0000002001", side="SELL", requested_price=354000, filled_qty=1, remaining_qty=0, average_price=354000)]),
                response([]),
                response([order_row(order_no="0000001002", side="BUY", requested_price=350000, filled_qty=1, remaining_qty=0, average_price=350000)]),
                response([]),
                response([order_row(order_no="0000002002", side="SELL", requested_price=354000, filled_qty=1, remaining_qty=0, average_price=354000)]),
            ]
            trader, _, orders, account = self._trader(
                root,
                api_responses=api_responses,
                position=None,
                max_round_trips=2,
            )

            trader.step()  # BUY_PENDING round 1
            trader.step()  # HOLDING round 1
            account.position = self._position()
            trader.step()  # SELL_PENDING round 1
            trader.step()  # IDLE, completed=1
            account.position = None
            trader.step()  # BUY_PENDING round 2
            trader.step()  # HOLDING round 2
            account.position = self._position()
            trader.step()  # SELL_PENDING round 2
            result = trader.step()  # COMPLETED

            self.assertEqual(result.state, TraderState.COMPLETED.value)
            self.assertEqual(result.completed_round_trips, 2)
            self.assertEqual(len(orders.buy_calls), 2)
            self.assertEqual(len(orders.sell_calls), 2)
            csv_path = root / "records" / "trades_20260622.csv"
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["side"] for row in rows], ["BUY", "SELL", "BUY", "SELL"])

    def test_after_close_submits_no_order(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            after_close = lambda: datetime(2026, 6, 22, 15, 31, tzinfo=SEOUL)
            trader, api, orders, _ = self._trader(
                root,
                api_responses=[],
                clock=after_close,
            )

            result = trader.run(run_once=True)

            self.assertEqual(result.state, TraderState.IDLE.value)
            self.assertEqual(api.calls, [])
            self.assertEqual(orders.buy_calls, [])
            self.assertEqual(orders.sell_calls, [])

class TraderTickIntegrationTests(TestCase):
    def test_holding_rounds_invalid_sell_price_up_before_order(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = RuntimeState(
                state=TraderState.HOLDING.value,
                symbol="005930",
                buy_order_no="0000029504",
                buy_order_date="20260622",
                requested_qty=1,
                buy_filled_qty=1,
                buy_average_price=350000,
                max_round_trips=1,
                round_trip_id=1,
            )
            settings = Settings(
                account_no="12345678",
                account_prod="01",
                app_key="key",
                app_secret="secret",
                order_qty=1,
                order_offset_krw=2000,
                poll_interval_seconds=60,
                token_cache_path=root / "token_cache.json",
                runtime_state_path=root / "runtime_state.json",
                records_dir=root / "records",
                log_path=root / "trader.log",
            )
            store = RuntimeStateStore(settings.runtime_state_path)
            store.save(initial)
            api = FakeApiClient([response([])])
            account = FakeAccountClient(
                Position("005930", "삼성전자", 1, 1, 350000, 352000, 2000, 0.57)
            )
            orders = FakeOrderClient()
            trader = SamsungAutoTrader(
                settings=settings,
                api_client=api,
                market_data=FakeMarketData(price=352_750),
                account_client=account,
                order_client=orders,
                runtime_state_store=store,
                trade_recorder=TradeRecordWriter(settings.records_dir),
                max_round_trips=1,
                resume_buy_order_no="",
                resume_order_date="",
                poll_interval_seconds=60,
                logger=Mock(),
                clock=lambda: FIXED_NOW,
                sleep_fn=lambda _: None,
            )

            result = trader.step()

            self.assertEqual(result.state, TraderState.SELL_PENDING.value)
            self.assertEqual(orders.sell_calls, [("005930", 1, 355_000)])
