from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from api_client import KISApiClient


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    quantity: int
    sellable_quantity: int
    average_price: float
    current_price: float
    pnl: float
    pnl_percent: float


@dataclass(frozen=True)
class AccountBalance:
    total_cash: float
    available_cash: float
    total_equity: float
    total_pnl: float
    total_pnl_percent: float


@dataclass(frozen=True)
class AccountSnapshot:
    balance: AccountBalance
    positions: list[Position]


class AccountClient:
    def __init__(self, api_client: KISApiClient, account_no: str, account_prod: str, logger: logging.Logger) -> None:
        self._api_client = api_client
        self._account_no = account_no
        self._account_prod = account_prod
        self._logger = logger

    def get_snapshot(self) -> AccountSnapshot:
        response = self._api_client.get(
            path="/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id="VTTC8434R",
            params={
                "CANO": self._account_no,
                "ACNT_PRDT_CD": self._account_prod,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )

        balance = self._parse_balance(response.output2)
        positions = self._parse_positions(response.output1)
        self._logger.info("잔고/보유 조회 완료: 보유 종목 %s건", len(positions))
        return AccountSnapshot(balance=balance, positions=positions)

    def get_symbol_position(self, snapshot: AccountSnapshot, symbol: str) -> Position | None:
        for position in snapshot.positions:
            if position.symbol == symbol:
                return position
        return None

    def _parse_balance(self, data: Any) -> AccountBalance:
        item = data[0] if isinstance(data, list) and data else (data or {})
        return AccountBalance(
            total_cash=self._to_float(item.get("dnca_tot_amt")),
            available_cash=self._to_float(item.get("nass_amt")),
            total_equity=self._to_float(item.get("tot_evlu_amt")),
            total_pnl=self._to_float(item.get("evlu_pfls_smtl_amt")),
            total_pnl_percent=self._to_float(item.get("evlu_pfls_rt")),
        )

    def _parse_positions(self, data: Any) -> list[Position]:
        items = data if isinstance(data, list) else []
        positions: list[Position] = []

        for item in items:
            quantity = self._to_int(item.get("hldg_qty"))
            if quantity <= 0:
                continue

            positions.append(
                Position(
                    symbol=str(item.get("pdno", "")).strip(),
                    name=str(item.get("prdt_name", "")).strip(),
                    quantity=quantity,
                    sellable_quantity=self._to_int(item.get("ord_psbl_qty", item.get("hldg_qty"))),
                    average_price=self._to_float(item.get("pchs_avg_pric")),
                    current_price=self._to_float(item.get("prpr")),
                    pnl=self._to_float(item.get("evlu_pfls_amt")),
                    pnl_percent=self._to_float(item.get("evlu_pfls_rt")),
                )
            )

        return positions

    @staticmethod
    def _to_int(value: object) -> int:
        try:
            return int(str(value).replace(",", "").strip() or 0)
        except ValueError:
            return 0

    @staticmethod
    def _to_float(value: object) -> float:
        try:
            return float(str(value).replace(",", "").strip() or 0)
        except ValueError:
            return 0.0
