from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import logging
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from account import AccountClient
from api_client import KISOrderError, KISQueryError, KISResponse, KISTransientApiError
from config import Settings, is_trading_window
from market_data import MarketDataClient
from orders import OrderClient, OrderSide
from state import RuntimeState, RuntimeStateStore, TradeRecordWriter


SEOUL_TZ = ZoneInfo("Asia/Seoul")


def krx_stock_tick_size(price: int) -> int:
    """Return the KRX cash-equity tick size for a positive KRW price."""
    if price <= 0:
        raise ValueError("price must be greater than 0")
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def round_buy_price_to_tick(price: int) -> int:
    """Round a buy limit down so it never exceeds the strategy target."""
    tick = krx_stock_tick_size(price)
    return (price // tick) * tick


def round_sell_price_to_tick(price: int) -> int:
    """Round a sell limit up so it never falls below the strategy target."""
    tick = krx_stock_tick_size(price)
    return ((price + tick - 1) // tick) * tick


class TraderState(str, Enum):
    IDLE = "IDLE"
    BUY_PENDING = "BUY_PENDING"
    HOLDING = "HOLDING"
    SELL_PENDING = "SELL_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class TrackedOrderStatus:
    order_no: str
    side: str
    order_date: str
    requested_qty: int
    requested_price: int
    filled_qty: int
    average_filled_price: float
    remaining_qty: int
    rejected_qty: int
    cancelled: str
    status: str

    @property
    def is_pending(self) -> bool:
        return self.status in {"PENDING", "PARTIAL"}

    @property
    def is_filled(self) -> bool:
        return self.status == "FILLED"

    @property
    def is_failed(self) -> bool:
        return self.status in {"REJECTED", "CANCELED", "UNKNOWN"}


class SamsungAutoTrader:
    """State-based, mock-only round-trip trader for Samsung Electronics."""

    def __init__(
        self,
        *,
        settings: Settings,
        api_client: Any,
        market_data: MarketDataClient,
        account_client: AccountClient,
        order_client: OrderClient,
        runtime_state_store: RuntimeStateStore,
        trade_recorder: TradeRecordWriter,
        max_round_trips: int,
        resume_buy_order_no: str,
        resume_order_date: str,
        poll_interval_seconds: int,
        logger: logging.Logger,
        clock: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_round_trips <= 0:
            raise ValueError("max_round_trips must be greater than 0.")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than 0.")

        self._settings = settings
        self._api_client = api_client
        self._market_data = market_data
        self._account_client = account_client
        self._order_client = order_client
        self._runtime_state_store = runtime_state_store
        self._trade_recorder = trade_recorder
        self._max_round_trips = max_round_trips
        self._resume_buy_order_no = resume_buy_order_no.strip()
        self._resume_order_date = resume_order_date.strip()
        self._poll_interval_seconds = poll_interval_seconds
        self._logger = logger
        self._clock = clock or (lambda: datetime.now(SEOUL_TZ))
        self._sleep = sleep_fn
        self._runtime_state = self._load_initial_state()

    @property
    def runtime_state(self) -> RuntimeState:
        return self._runtime_state

    def run(self, run_once: bool = False) -> RuntimeState:
        self._logger.info(
            "자동매매 시작: state=%s, completed=%s/%s",
            self._runtime_state.state,
            self._runtime_state.completed_round_trips,
            self._runtime_state.max_round_trips,
        )

        self._wait_until_trading_window()

        while True:
            now = self._now()
            if not is_trading_window(
                now.time(), self._settings.trading_start, self._settings.trading_end
            ):
                self._logger.info(
                    "거래 가능 시간이 아니므로 추가 주문 없이 종료합니다: %s",
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                )
                self._save_runtime_state(self._runtime_state)
                return self._runtime_state

            previous = self._runtime_state
            self._runtime_state = self.step()
            self._save_runtime_state(self._runtime_state)
            self._logger.info(
                "상태 갱신: %s -> %s (왕복 %s/%s)",
                previous.state,
                self._runtime_state.state,
                self._runtime_state.completed_round_trips,
                self._runtime_state.max_round_trips,
            )

            if self._runtime_state.state in {
                TraderState.COMPLETED.value,
                TraderState.FAILED.value,
            }:
                self._logger.info("자동매매 종료 상태: %s", self._runtime_state.state)
                return self._runtime_state

            if run_once:
                self._logger.info("--run-once: 상태 전이 1회 후 종료합니다.")
                return self._runtime_state

            remaining = self._seconds_until_end()
            if remaining <= 0:
                self._logger.info("15:30 도달: 상태를 저장하고 종료합니다.")
                return self._runtime_state

            sleep_seconds = self._next_sleep_seconds(previous, self._runtime_state, remaining)
            self._logger.info("다음 상태 확인까지 %s초 대기", sleep_seconds)
            self._sleep(sleep_seconds)

    def step(self) -> RuntimeState:
        """Advance exactly one state-machine step. Useful for tests and --run-once."""
        now = self._now()
        if not is_trading_window(
            now.time(), self._settings.trading_start, self._settings.trading_end
        ):
            return self._runtime_state

        state = self._normalize_state(self._runtime_state.state)
        if state == TraderState.IDLE:
            next_state = self._handle_idle(self._runtime_state)
        elif state == TraderState.BUY_PENDING:
            next_state = self._handle_buy_pending(self._runtime_state)
        elif state == TraderState.HOLDING:
            next_state = self._handle_holding(self._runtime_state)
        elif state == TraderState.SELL_PENDING:
            next_state = self._handle_sell_pending(self._runtime_state)
        else:
            next_state = self._runtime_state

        self._runtime_state = next_state
        return next_state

    def _handle_idle(self, runtime_state: RuntimeState) -> RuntimeState:
        if runtime_state.completed_round_trips >= runtime_state.max_round_trips:
            return replace(runtime_state, state=TraderState.COMPLETED.value)

        try:
            active_orders = self._fetch_active_orders()
        except (KISQueryError, KISTransientApiError, RuntimeError) as exc:
            self._logger.warning("활성 주문 조회 실패: %s", exc)
            return runtime_state

        if len(active_orders) > 1:
            return self._fail(runtime_state, "활성 미체결 주문이 2건 이상이라 자동 선택하지 않습니다.")
        if len(active_orders) == 1:
            active = active_orders[0]
            if active.side == "BUY":
                self._logger.info("기존 미체결 매수 주문을 이어받습니다: %s", active.order_no)
                return replace(
                    runtime_state,
                    state=TraderState.BUY_PENDING.value,
                    buy_order_no=active.order_no,
                    buy_order_date=active.order_date,
                    requested_qty=active.requested_qty,
                    requested_buy_price=active.requested_price,
                    round_trip_id=runtime_state.completed_round_trips + 1,
                )
            if active.side == "SELL":
                self._logger.info("기존 미체결 매도 주문을 이어받습니다: %s", active.order_no)
                return replace(
                    runtime_state,
                    state=TraderState.SELL_PENDING.value,
                    sell_order_no=active.order_no,
                    sell_order_date=active.order_date,
                    requested_qty=active.requested_qty,
                    requested_sell_price=active.requested_price,
                    round_trip_id=max(1, runtime_state.completed_round_trips + 1),
                )
            return self._fail(runtime_state, "활성 주문의 매수/매도 구분을 확인할 수 없습니다.")

        try:
            snapshot = self._account_client.get_snapshot()
            position = self._account_client.get_symbol_position(snapshot, runtime_state.symbol)
        except (KISQueryError, KISTransientApiError) as exc:
            self._logger.warning("IDLE 잔고 조회 실패: %s", exc)
            return runtime_state

        if position is not None and position.quantity > 0:
            self._logger.info(
                "기존 보유수량을 감지해 새 매수 없이 HOLDING으로 전환합니다: qty=%s",
                position.quantity,
            )
            target_qty = min(
                position.quantity,
                runtime_state.requested_qty or self._settings.order_qty,
            )
            return replace(
                runtime_state,
                state=TraderState.HOLDING.value,
                requested_qty=target_qty,
                buy_filled_qty=runtime_state.buy_filled_qty or target_qty,
                round_trip_id=max(1, runtime_state.completed_round_trips + 1),
            )

        try:
            quote = self._market_data.get_current_price(runtime_state.symbol)
        except (KISQueryError, KISTransientApiError) as exc:
            self._logger.warning("매수 전 현재가 조회 실패: %s", exc)
            return runtime_state

        raw_buy_price = quote.current_price - self._settings.order_offset_krw
        if raw_buy_price <= 0:
            return self._fail(runtime_state, "계산된 매수 가격이 0 이하입니다.")
        buy_price = round_buy_price_to_tick(raw_buy_price)
        if buy_price != raw_buy_price:
            self._logger.info(
                "매수 주문가격 호가단위 보정: %s -> %s",
                raw_buy_price,
                buy_price,
            )

        try:
            receipt = self._order_client.place_buy_order(
                symbol=runtime_state.symbol,
                quantity=self._settings.order_qty,
                price=buy_price,
            )
        except KISOrderError as exc:
            return self._fail(runtime_state, f"매수 주문 실패: {exc}")

        self._logger.info("매수 주문 접수: order_no=%s", receipt.order_no)
        today = self._now().strftime("%Y%m%d")
        return replace(
            runtime_state,
            state=TraderState.BUY_PENDING.value,
            buy_order_no=receipt.order_no,
            buy_order_date=today,
            sell_order_no="",
            sell_order_date="",
            requested_qty=receipt.quantity,
            requested_buy_price=receipt.price,
            requested_sell_price=0,
            buy_filled_qty=0,
            buy_average_price=0.0,
            sell_filled_qty=0,
            sell_average_price=0.0,
            round_trip_id=runtime_state.completed_round_trips + 1,
            last_error="",
        )

    def _handle_buy_pending(self, runtime_state: RuntimeState) -> RuntimeState:
        if not runtime_state.buy_order_no:
            return self._fail(runtime_state, "BUY_PENDING 상태에 매수 주문번호가 없습니다.")

        try:
            status = self._fetch_order_status(
                order_no=runtime_state.buy_order_no,
                side="BUY",
                order_date=runtime_state.buy_order_date or self._today(),
            )
        except (KISQueryError, KISTransientApiError) as exc:
            self._logger.warning("매수 주문 상태 조회 실패: %s", exc)
            return runtime_state
        except RuntimeError as exc:
            return self._fail(runtime_state, f"매수 주문 상태 확인 실패: {exc}")

        updated = replace(
            runtime_state,
            buy_order_date=status.order_date,
            requested_qty=status.requested_qty or runtime_state.requested_qty,
            requested_buy_price=status.requested_price or runtime_state.requested_buy_price,
            buy_filled_qty=status.filled_qty,
            buy_average_price=status.average_filled_price,
        )

        if status.is_pending:
            self._logger.info(
                "매수 체결 대기: order_no=%s, filled=%s, remaining=%s",
                status.order_no,
                status.filled_qty,
                status.remaining_qty,
            )
            return updated

        if status.is_failed:
            self._record_status(updated, "BUY", status)
            return self._fail(updated, f"매수 주문 상태={status.status}")

        self._record_status(updated, "BUY", status)
        self._logger.info(
            "매수 체결 완료: order_no=%s, qty=%s, avg=%s",
            status.order_no,
            status.filled_qty,
            status.average_filled_price,
        )
        return replace(updated, state=TraderState.HOLDING.value, last_error="")

    def _handle_holding(self, runtime_state: RuntimeState) -> RuntimeState:
        try:
            active_orders = self._fetch_active_orders()
        except (KISQueryError, KISTransientApiError, RuntimeError) as exc:
            self._logger.warning("매도 전 활성 주문 조회 실패: %s", exc)
            return runtime_state

        if len(active_orders) > 1:
            return self._fail(runtime_state, "활성 미체결 주문이 2건 이상이라 매도하지 않습니다.")
        if len(active_orders) == 1:
            active = active_orders[0]
            if active.side == "SELL":
                return replace(
                    runtime_state,
                    state=TraderState.SELL_PENDING.value,
                    sell_order_no=active.order_no,
                    sell_order_date=active.order_date,
                    requested_sell_price=active.requested_price,
                    requested_qty=active.requested_qty or runtime_state.requested_qty,
                )
            return self._fail(runtime_state, "보유 중 미체결 매수 주문이 발견되어 중단합니다.")

        try:
            snapshot = self._account_client.get_snapshot()
            position = self._account_client.get_symbol_position(snapshot, runtime_state.symbol)
        except (KISQueryError, KISTransientApiError) as exc:
            self._logger.warning("매도 전 잔고 조회 실패: %s", exc)
            return runtime_state

        if position is None or position.quantity <= 0:
            self._logger.warning("HOLDING 상태지만 보유수량이 아직 확인되지 않습니다.")
            return runtime_state

        target_qty = (
            runtime_state.buy_filled_qty
            or runtime_state.requested_qty
            or self._settings.order_qty
        )
        sell_qty = min(target_qty, max(0, position.sellable_quantity))
        if sell_qty <= 0:
            self._logger.info("현재 매도 가능 수량이 없어 대기합니다.")
            return runtime_state

        try:
            quote = self._market_data.get_current_price(runtime_state.symbol)
        except (KISQueryError, KISTransientApiError) as exc:
            self._logger.warning("매도 전 현재가 조회 실패: %s", exc)
            return runtime_state

        raw_sell_price = quote.current_price + self._settings.order_offset_krw
        if raw_sell_price <= 0:
            return self._fail(runtime_state, "계산된 매도 가격이 0 이하입니다.")
        sell_price = round_sell_price_to_tick(raw_sell_price)
        if sell_price != raw_sell_price:
            self._logger.info(
                "매도 주문가격 호가단위 보정: %s -> %s",
                raw_sell_price,
                sell_price,
            )

        try:
            receipt = self._order_client.place_sell_order(
                symbol=runtime_state.symbol,
                quantity=sell_qty,
                price=sell_price,
            )
        except KISOrderError as exc:
            return self._fail(runtime_state, f"매도 주문 실패: {exc}")

        self._logger.info("매도 주문 접수: order_no=%s", receipt.order_no)
        return replace(
            runtime_state,
            state=TraderState.SELL_PENDING.value,
            sell_order_no=receipt.order_no,
            sell_order_date=self._today(),
            requested_qty=sell_qty,
            requested_sell_price=sell_price,
            last_error="",
        )

    def _handle_sell_pending(self, runtime_state: RuntimeState) -> RuntimeState:
        if not runtime_state.sell_order_no:
            return self._fail(runtime_state, "SELL_PENDING 상태에 매도 주문번호가 없습니다.")

        try:
            status = self._fetch_order_status(
                order_no=runtime_state.sell_order_no,
                side="SELL",
                order_date=runtime_state.sell_order_date or self._today(),
            )
        except (KISQueryError, KISTransientApiError) as exc:
            self._logger.warning("매도 주문 상태 조회 실패: %s", exc)
            return runtime_state
        except RuntimeError as exc:
            return self._fail(runtime_state, f"매도 주문 상태 확인 실패: {exc}")

        updated = replace(
            runtime_state,
            sell_order_date=status.order_date,
            requested_sell_price=status.requested_price or runtime_state.requested_sell_price,
            sell_filled_qty=status.filled_qty,
            sell_average_price=status.average_filled_price,
        )

        if status.is_pending:
            self._logger.info(
                "매도 체결 대기: order_no=%s, filled=%s, remaining=%s",
                status.order_no,
                status.filled_qty,
                status.remaining_qty,
            )
            return updated

        if status.is_failed:
            self._record_status(updated, "SELL", status)
            return self._fail(updated, f"매도 주문 상태={status.status}")

        self._record_status(updated, "SELL", status)
        completed = updated.completed_round_trips + 1
        self._logger.info(
            "왕복 거래 %s회 완료: buy_avg=%s, sell_avg=%s",
            completed,
            updated.buy_average_price,
            status.average_filled_price,
        )

        if completed >= updated.max_round_trips:
            return replace(
                updated,
                state=TraderState.COMPLETED.value,
                completed_round_trips=completed,
                last_error="",
            )

        # Prepare the next independent round trip. Server-side reconciliation is
        # performed again in IDLE before any new order is submitted.
        return RuntimeState(
            state=TraderState.IDLE.value,
            symbol=updated.symbol,
            completed_round_trips=completed,
            max_round_trips=updated.max_round_trips,
            round_trip_id=completed + 1,
        )

    def _fetch_order_status(
        self,
        *,
        order_no: str,
        side: str,
        order_date: str,
    ) -> TrackedOrderStatus:
        response = self._query_orders(
            order_date=order_date,
            side_code="02" if side == "BUY" else "01",
            order_no=order_no,
            completion_code="00",
        )
        rows = response.output1 or []
        if not isinstance(rows, list):
            raise RuntimeError("주문 상태 응답 output1이 목록이 아닙니다.")

        normalized_target = self._normalize_order_no(order_no)
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and self._normalize_order_no(str(row.get("odno", ""))) == normalized_target
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"주문번호 {order_no} 조회 결과가 {len(matches)}건입니다."
            )

        status = self._parse_order_status(matches[0], order_date=order_date, expected_side=side)
        self._logger.info(
            "%s 주문 상태: order_no=%s, status=%s, filled=%s, remaining=%s",
            side,
            status.order_no,
            status.status,
            status.filled_qty,
            status.remaining_qty,
        )
        return status

    def _fetch_active_orders(self) -> list[TrackedOrderStatus]:
        today = self._today()
        response = self._query_orders(
            order_date=today,
            side_code="00",
            order_no="",
            completion_code="02",
        )
        rows = response.output1 or []
        if not isinstance(rows, list):
            raise RuntimeError("활성 주문 응답 output1이 목록이 아닙니다.")

        active_by_order: dict[str, TrackedOrderStatus] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("pdno", "")).strip()
            if symbol and symbol != self._settings.symbol:
                continue
            try:
                status = self._parse_order_status(row, order_date=today, expected_side="")
            except RuntimeError:
                continue
            if status.remaining_qty <= 0 or status.cancelled == "Y" or status.rejected_qty > 0:
                continue
            active_by_order[self._normalize_order_no(status.order_no)] = status

        return list(active_by_order.values())

    def _query_orders(
        self,
        *,
        order_date: str,
        side_code: str,
        order_no: str,
        completion_code: str,
    ) -> KISResponse:
        return self._api_client.get(
            path="/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id="VTTC0081R",
            params={
                "CANO": self._settings.account_no,
                "ACNT_PRDT_CD": self._settings.account_prod,
                "INQR_STRT_DT": order_date,
                "INQR_END_DT": order_date,
                "SLL_BUY_DVSN_CD": side_code,
                "PDNO": self._settings.symbol,
                "CCLD_DVSN": completion_code,
                "INQR_DVSN": "00",
                "INQR_DVSN_3": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": order_no,
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "EXCG_ID_DVSN_CD": "KRX",
            },
        )

    def _parse_order_status(
        self,
        row: dict[str, Any],
        *,
        order_date: str,
        expected_side: str,
    ) -> TrackedOrderStatus:
        order_no = str(row.get("odno", "")).strip()
        if not order_no:
            raise RuntimeError("주문번호가 비어 있습니다.")

        requested_qty = self._to_int(row.get("ord_qty"))
        requested_price = self._to_int(row.get("ord_unpr"))
        filled_qty = self._to_int(row.get("tot_ccld_qty"))
        average_filled_price = self._to_float(row.get("avg_prvs"))
        remaining_qty = self._to_int(row.get("rmn_qty"))
        rejected_qty = self._to_int(row.get("rjct_qty"))
        cancelled = str(row.get("cncl_yn", "")).strip().upper()
        side = expected_side or self._parse_side(row)

        if cancelled == "Y":
            status = "CANCELED"
        elif rejected_qty > 0:
            status = "REJECTED"
        elif filled_qty > 0 and remaining_qty == 0:
            status = "FILLED"
        elif filled_qty > 0 and remaining_qty > 0:
            status = "PARTIAL"
        elif remaining_qty > 0:
            status = "PENDING"
        else:
            status = "UNKNOWN"

        return TrackedOrderStatus(
            order_no=order_no,
            side=side,
            order_date=order_date,
            requested_qty=requested_qty,
            requested_price=requested_price,
            filled_qty=filled_qty,
            average_filled_price=average_filled_price,
            remaining_qty=remaining_qty,
            rejected_qty=rejected_qty,
            cancelled=cancelled,
            status=status,
        )

    @staticmethod
    def _parse_side(row: dict[str, Any]) -> str:
        code = str(row.get("sll_buy_dvsn_cd", "")).strip()
        name = str(
            row.get("sll_buy_dvsn_cd_name")
            or row.get("sll_buy_dvsn_name")
            or ""
        ).strip()
        if code == "02" or "매수" in name:
            return "BUY"
        if code == "01" or "매도" in name:
            return "SELL"
        return "UNKNOWN"

    def _record_status(
        self,
        runtime_state: RuntimeState,
        side: str,
        status: TrackedOrderStatus,
    ) -> None:
        realized_profit: float | None = None
        buy_average = runtime_state.buy_average_price
        sell_average = runtime_state.sell_average_price
        if side == "BUY":
            buy_average = status.average_filled_price
        elif side == "SELL":
            sell_average = status.average_filled_price
            if buy_average > 0 and sell_average > 0:
                realized_profit = round(
                    (sell_average - buy_average) * status.filled_qty,
                    2,
                )

        path = self._trade_recorder.record(
            round_trip_id=runtime_state.round_trip_id,
            timestamp=self._now(),
            symbol=runtime_state.symbol,
            side=side,
            order_no=status.order_no,
            requested_price=status.requested_price,
            requested_qty=status.requested_qty,
            filled_qty=status.filled_qty,
            average_filled_price=status.average_filled_price,
            remaining_qty=status.remaining_qty,
            rejected_qty=status.rejected_qty,
            cancelled=status.cancelled,
            status=status.status,
            buy_average_price=buy_average,
            sell_average_price=sell_average,
            realized_profit_krw=realized_profit,
        )
        self._logger.info("거래 기록 저장: %s", path.name)

    def _load_initial_state(self) -> RuntimeState:
        loaded = self._runtime_state_store.load()
        today = self._today()

        if self._resume_buy_order_no:
            completed = 0
            if loaded is not None and self._same_order_no(
                loaded.buy_order_no, self._resume_buy_order_no
            ):
                completed = loaded.completed_round_trips
            return RuntimeState(
                state=TraderState.BUY_PENDING.value,
                symbol=self._settings.symbol,
                buy_order_no=self._resume_buy_order_no,
                buy_order_date=self._resume_order_date or today,
                completed_round_trips=completed,
                max_round_trips=self._max_round_trips,
                round_trip_id=completed + 1,
            )

        if loaded is None:
            return RuntimeState(
                state=TraderState.IDLE.value,
                symbol=self._settings.symbol,
                max_round_trips=self._max_round_trips,
                round_trip_id=1,
            )

        normalized = replace(
            loaded,
            symbol=self._settings.symbol,
            max_round_trips=self._max_round_trips,
            round_trip_id=max(1, loaded.completed_round_trips + 1)
            if loaded.state == TraderState.IDLE.value
            else max(1, loaded.round_trip_id),
        )
        if (
            normalized.state == TraderState.COMPLETED.value
            and normalized.completed_round_trips < self._max_round_trips
        ):
            return RuntimeState(
                state=TraderState.IDLE.value,
                symbol=self._settings.symbol,
                completed_round_trips=normalized.completed_round_trips,
                max_round_trips=self._max_round_trips,
                round_trip_id=normalized.completed_round_trips + 1,
            )
        return normalized

    def _fail(self, runtime_state: RuntimeState, message: str) -> RuntimeState:
        self._logger.error(message)
        return replace(
            runtime_state,
            state=TraderState.FAILED.value,
            last_error=message,
        )

    def _save_runtime_state(self, runtime_state: RuntimeState) -> None:
        self._runtime_state_store.save(runtime_state)

    def _wait_until_trading_window(self) -> None:
        now = self._now()
        if now.time() >= self._settings.trading_end:
            return
        while now.time() < self._settings.trading_start:
            remaining = self._seconds_until_start()
            self._logger.info("거래 시작 전: %s초 대기", remaining)
            self._sleep(min(60, max(1, remaining)))
            now = self._now()

    def _next_sleep_seconds(
        self,
        previous: RuntimeState,
        current: RuntimeState,
        remaining: int,
    ) -> int:
        # When a fill was just detected, advance to the next action quickly.
        if previous.state != current.state and current.state in {
            TraderState.HOLDING.value,
            TraderState.IDLE.value,
        }:
            return max(1, min(2, remaining))
        return max(1, min(self._poll_interval_seconds, remaining))

    def _seconds_until_start(self) -> int:
        now = self._now()
        start_dt = datetime.combine(
            now.date(), self._settings.trading_start, tzinfo=now.tzinfo
        )
        return max(0, int((start_dt - now).total_seconds()))

    def _seconds_until_end(self) -> int:
        now = self._now()
        end_dt = datetime.combine(
            now.date(), self._settings.trading_end, tzinfo=now.tzinfo
        )
        return max(0, int((end_dt - now).total_seconds()))

    def _today(self) -> str:
        return self._now().strftime("%Y%m%d")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            return now.replace(tzinfo=SEOUL_TZ)
        return now.astimezone(SEOUL_TZ)

    @staticmethod
    def _normalize_state(state: str) -> TraderState:
        try:
            return TraderState(state)
        except ValueError:
            return TraderState.IDLE

    @staticmethod
    def _normalize_order_no(order_no: str) -> str:
        value = str(order_no).strip()
        return value.lstrip("0") or "0"

    @classmethod
    def _same_order_no(cls, left: str, right: str) -> bool:
        if not left or not right:
            return False
        return cls._normalize_order_no(left) == cls._normalize_order_no(right)

    @staticmethod
    def _to_int(value: object) -> int:
        try:
            return int(str(value).replace(",", "").strip() or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_float(value: object) -> float:
        try:
            return float(str(value).replace(",", "").strip() or 0)
        except (TypeError, ValueError):
            return 0.0
