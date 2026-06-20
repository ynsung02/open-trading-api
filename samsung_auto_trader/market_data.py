from __future__ import annotations

from dataclasses import dataclass
import logging

from api_client import KISApiClient


@dataclass(frozen=True)
class PriceQuote:
    symbol: str
    current_price: int
    open_price: int
    high_price: int
    low_price: int
    change: int
    change_percent: float


class MarketDataClient:
    def __init__(self, api_client: KISApiClient, logger: logging.Logger) -> None:
        self._api_client = api_client
        self._logger = logger

    def get_current_price(self, symbol: str) -> PriceQuote:
        response = self._api_client.get(
            path="/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            },
        )

        output = response.output or {}
        current_price = self._parse_int(output.get("stck_prpr"))
        quote = PriceQuote(
            symbol=symbol,
            current_price=current_price,
            open_price=self._parse_int(output.get("stck_oprc")),
            high_price=self._parse_int(output.get("stck_hgpr")),
            low_price=self._parse_int(output.get("stck_lwpr")),
            change=self._parse_int(output.get("prdy_vrss")),
            change_percent=self._parse_float(output.get("prdy_ctrt")),
        )

        self._logger.info("현재가 조회: %s / %s원", symbol, f"{quote.current_price:,}")
        return quote

    @staticmethod
    def _parse_int(value: object) -> int:
        try:
            return int(str(value).replace(",", "").strip() or 0)
        except ValueError:
            return 0

    @staticmethod
    def _parse_float(value: object) -> float:
        try:
            return float(str(value).replace(",", "").strip() or 0)
        except ValueError:
            return 0.0
