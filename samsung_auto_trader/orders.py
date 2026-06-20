from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import Any

from api_client import KISApiClient


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderReceipt:
    order_no: str
    symbol: str
    side: OrderSide
    quantity: int
    price: int
    raw: dict[str, Any]


class OrderClient:
    def __init__(self, api_client: KISApiClient, account_no: str, account_prod: str, logger: logging.Logger) -> None:
        self._api_client = api_client
        self._account_no = account_no
        self._account_prod = account_prod
        self._logger = logger

    def place_buy_order(self, symbol: str, quantity: int, price: int) -> OrderReceipt:
        return self._place_limit_order(symbol=symbol, quantity=quantity, price=price, side=OrderSide.BUY)

    def place_sell_order(self, symbol: str, quantity: int, price: int) -> OrderReceipt:
        return self._place_limit_order(symbol=symbol, quantity=quantity, price=price, side=OrderSide.SELL)

    def _place_limit_order(self, symbol: str, quantity: int, price: int, side: OrderSide) -> OrderReceipt:
        tr_id = "VTTC0012U" if side == OrderSide.BUY else "VTTC0011U"
        self._logger.info(
            "%s 주문 요청: symbol=%s, qty=%s, price=%s",
            "매수" if side == OrderSide.BUY else "매도",
            symbol,
            quantity,
            f"{price:,}",
        )

        response = self._api_client.post(
            path="/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_id,
            body={
                "CANO": self._account_no,
                "ACNT_PRDT_CD": self._account_prod,
                "PDNO": symbol,
                "ORD_DVSN": "00",
                "ORD_QTY": str(quantity),
                "ORD_UNPR": str(price),
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "" if side == OrderSide.BUY else "01",
                "CNDT_PRIC": "",
            },
        )

        output = response.output or {}
        order_no = str(output.get("ODNO", "")).strip()
        if not order_no:
            raise RuntimeError(f"Order number missing in response: {response.raw}")

        return OrderReceipt(
            order_no=order_no,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            raw=response.raw,
        )
