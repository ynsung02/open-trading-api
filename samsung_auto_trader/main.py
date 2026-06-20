from __future__ import annotations

import argparse

from account import AccountClient
from api_client import KISApiClient
from auth import TokenManager
from config import load_settings
from logger import setup_logger
from market_data import MarketDataClient
from orders import OrderClient
from trader import SamsungAutoTrader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Samsung Electronics mock auto trader using KIS REST API only")
    parser.add_argument("--poll-interval-seconds", type=int, default=None, help="Override polling interval in seconds")
    parser.add_argument("--order-qty", type=int, default=None, help="Override order quantity")
    parser.add_argument("--order-offset-krw", type=int, default=None, help="Override order offset in KRW")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings()

    if args.poll_interval_seconds is not None or args.order_qty is not None or args.order_offset_krw is not None:
        settings = settings.__class__(
            account_no=settings.account_no,
            account_prod=settings.account_prod,
            app_key=settings.app_key,
            app_secret=settings.app_secret,
            base_url=settings.base_url,
            symbol=settings.symbol,
            order_qty=args.order_qty if args.order_qty is not None else settings.order_qty,
            order_offset_krw=args.order_offset_krw if args.order_offset_krw is not None else settings.order_offset_krw,
            poll_interval_seconds=args.poll_interval_seconds if args.poll_interval_seconds is not None else settings.poll_interval_seconds,
            http_timeout_seconds=settings.http_timeout_seconds,
            max_retries=settings.max_retries,
            trading_start=settings.trading_start,
            trading_end=settings.trading_end,
            token_cache_path=settings.token_cache_path,
            log_path=settings.log_path,
        )

    logger = setup_logger(settings.log_path)
    logger.info("프로그램 시작: paper-only / REST-only")

    token_manager = TokenManager(settings=settings, logger=logger)
    api_client = KISApiClient(settings=settings, token_manager=token_manager, logger=logger)
    market_data = MarketDataClient(api_client=api_client, logger=logger)
    account_client = AccountClient(
        api_client=api_client,
        account_no=settings.account_no,
        account_prod=settings.account_prod,
        logger=logger,
    )
    order_client = OrderClient(
        api_client=api_client,
        account_no=settings.account_no,
        account_prod=settings.account_prod,
        logger=logger,
    )
    trader = SamsungAutoTrader(
        settings=settings,
        market_data=market_data,
        account_client=account_client,
        order_client=order_client,
        logger=logger,
    )

    try:
        trader.run()
    except KeyboardInterrupt:
        logger.info("사용자 중단으로 종료합니다.")


if __name__ == "__main__":
    main()
