from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import time

from account import AccountClient, AccountSnapshot, Position
from config import Settings, is_trading_window, seconds_until, seconds_until_end
from market_data import MarketDataClient
from orders import OrderClient, OrderSide


@dataclass(frozen=True)
class ExecutionSummary:
    buy_executed: bool
    sell_executed: bool


class SamsungAutoTrader:
    def __init__(
        self,
        settings: Settings,
        market_data: MarketDataClient,
        account_client: AccountClient,
        order_client: OrderClient,
        logger: logging.Logger,
    ) -> None:
        self._settings = settings
        self._market_data = market_data
        self._account_client = account_client
        self._order_client = order_client
        self._logger = logger

    def run(self) -> None:
        self._wait_until_trading_window()
        self._logger.info("거래 윈도우 시작: %s ~ %s", self._settings.trading_start, self._settings.trading_end)

        while True:
            now = datetime.now()
            if not is_trading_window(now.time(), self._settings.trading_start, self._settings.trading_end):
                self._logger.info("거래 윈도우 종료: %s", now.strftime("%H:%M:%S"))
                break

            cycle_started = time.time()
            try:
                self._run_cycle()
            except Exception as exc:
                self._logger.exception("거래 사이클 실패: %s", exc)

            remaining = seconds_until_end(self._settings.trading_end)
            if remaining <= 0:
                self._logger.info("15:30 이후라 종료합니다.")
                break

            elapsed = int(time.time() - cycle_started)
            sleep_seconds = max(1, min(self._settings.poll_interval_seconds, remaining) - elapsed)
            self._logger.info("다음 사이클까지 %s초 대기", sleep_seconds)
            time.sleep(sleep_seconds)

    def _wait_until_trading_window(self) -> None:
        now = datetime.now()
        current_time = now.time()
        if current_time >= self._settings.trading_end:
            self._logger.info("이미 거래 종료 시각을 지났습니다.")
            return

        if current_time < self._settings.trading_start:
            wait_seconds = seconds_until(self._settings.trading_start)
            self._logger.info("거래 시작 전입니다. %s초 후 시작합니다.", wait_seconds)
            while True:
                remaining = seconds_until(self._settings.trading_start)
                if remaining <= 0:
                    break
                time.sleep(min(60, remaining))

    def _run_cycle(self) -> None:
        quote = self._market_data.get_current_price(self._settings.symbol)
        before_snapshot = self._account_client.get_snapshot()
        before_position = self._account_client.get_symbol_position(before_snapshot, self._settings.symbol)

        self._log_snapshot("주문 전", quote.current_price, before_snapshot, before_position)

        buy_price = quote.current_price - self._settings.order_offset_krw
        sell_price = quote.current_price + self._settings.order_offset_krw

        if buy_price <= 0:
            self._logger.warning("매수 가격이 0 이하입니다. 이번 사이클은 건너뜁니다.")
            return

        buy_receipt = self._order_client.place_buy_order(
            symbol=self._settings.symbol,
            quantity=self._settings.order_qty,
            price=buy_price,
        )
        self._logger.info("매수 주문 접수: order_no=%s", buy_receipt.order_no)

        after_buy_snapshot = self._account_client.get_snapshot()
        after_buy_position = self._account_client.get_symbol_position(after_buy_snapshot, self._settings.symbol)
        buy_executed = self._detect_buy_execution(before_position, after_buy_position)
        self._log_snapshot("매수 후", quote.current_price, after_buy_snapshot, after_buy_position)
        self._logger.info("매수 체결 추정: %s", "예" if buy_executed else "아니오")

        sell_executed = False
        sell_quantity = self._determine_sell_quantity(after_buy_position)
        if sell_quantity > 0:
            sell_receipt = self._order_client.place_sell_order(
                symbol=self._settings.symbol,
                quantity=sell_quantity,
                price=sell_price,
            )
            self._logger.info("매도 주문 접수: order_no=%s", sell_receipt.order_no)

            after_sell_snapshot = self._account_client.get_snapshot()
            after_sell_position = self._account_client.get_symbol_position(after_sell_snapshot, self._settings.symbol)
            sell_executed = self._detect_sell_execution(after_buy_position, after_sell_position)
            self._log_snapshot("매도 후", quote.current_price, after_sell_snapshot, after_sell_position)
            self._logger.info("매도 체결 추정: %s", "예" if sell_executed else "아니오")
        else:
            self._logger.info("매도 가능 수량이 없어 이번 사이클의 매도 주문은 생략합니다.")

        summary = ExecutionSummary(buy_executed=buy_executed, sell_executed=sell_executed)
        self._logger.info("사이클 종료 요약: buy=%s, sell=%s", summary.buy_executed, summary.sell_executed)

    def _log_snapshot(
        self,
        label: str,
        current_price: int,
        snapshot: AccountSnapshot,
        position: Position | None,
    ) -> None:
        balance = snapshot.balance
        self._logger.info(
            "%s - 현재가=%s, 예수금=%s, 주문가능=%s, 총평가=%s",
            label,
            f"{current_price:,}",
            f"{balance.total_cash:,.0f}",
            f"{balance.available_cash:,.0f}",
            f"{balance.total_equity:,.0f}",
        )
        if position is None:
            self._logger.info("%s - 삼성전자 보유 없음", label)
        else:
            self._logger.info(
                "%s - 삼성전자 보유: %s주, 평균단가=%s, 평가손익=%s",
                label,
                position.quantity,
                f"{position.average_price:,.0f}",
                f"{position.pnl:,.0f}",
            )

    def _detect_buy_execution(self, before: Position | None, after: Position | None) -> bool:
        before_qty = before.quantity if before else 0
        after_qty = after.quantity if after else 0
        return after_qty > before_qty

    def _detect_sell_execution(self, before: Position | None, after: Position | None) -> bool:
        before_qty = before.quantity if before else 0
        after_qty = after.quantity if after else 0
        return after_qty < before_qty

    def _determine_sell_quantity(self, position: Position | None) -> int:
        if position is None:
            return 0
        return max(0, min(self._settings.order_qty, position.quantity))
