from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import re

from account import AccountClient
from api_client import KISApiError, KISApiClient
from auth import TokenManager
from config import load_settings
from logger import setup_logger
from market_data import MarketDataClient
from orders import OrderClient
from state import RuntimeStateStore, TradeRecordWriter
from trader import SamsungAutoTrader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Samsung Electronics mock auto trader using KIS REST API only"
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=None,
        help="Polling interval for pending orders (default: settings value)",
    )
    parser.add_argument("--order-qty", type=int, default=None, help="Order quantity")
    parser.add_argument(
        "--order-offset-krw",
        type=int,
        default=None,
        help="Buy below / sell above current price by this amount",
    )
    parser.add_argument(
        "--max-round-trips",
        type=int,
        default=1,
        help="Number of fully filled buy+sell round trips (default: 1)",
    )
    parser.add_argument(
        "--resume-buy-order-no",
        type=str,
        default="",
        help="Resume from an existing buy order number",
    )
    parser.add_argument(
        "--resume-order-date",
        type=str,
        default="",
        help="Date of the resumed order in YYYYMMDD (default: today)",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Advance exactly one state-machine step and exit",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Delete local runtime_state.json before server reconciliation",
    )
    # Backward-compatible alias from the earlier draft.
    parser.add_argument(
        "--stop-after-round-trip",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for name in ("poll_interval_seconds", "order_qty", "max_round_trips"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than 0")
    if args.order_offset_krw is not None and args.order_offset_krw < 0:
        parser.error("--order-offset-krw must be 0 or greater")
    if args.resume_order_date:
        if not re.fullmatch(r"\d{8}", args.resume_order_date):
            parser.error("--resume-order-date must be YYYYMMDD")
        try:
            datetime.strptime(args.resume_order_date, "%Y%m%d")
        except ValueError:
            parser.error("--resume-order-date is not a valid calendar date")
    if args.resume_order_date and not args.resume_buy_order_no:
        parser.error("--resume-order-date requires --resume-buy-order-no")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _validate_args(parser, args)

    settings = load_settings()
    settings = replace(
        settings,
        order_qty=args.order_qty if args.order_qty is not None else settings.order_qty,
        order_offset_krw=(
            args.order_offset_krw
            if args.order_offset_krw is not None
            else settings.order_offset_krw
        ),
        poll_interval_seconds=(
            args.poll_interval_seconds
            if args.poll_interval_seconds is not None
            else settings.poll_interval_seconds
        ),
    )

    max_round_trips = 1 if args.stop_after_round_trip else args.max_round_trips
    logger = setup_logger(settings.log_path)
    logger.info("프로그램 시작: paper-only / REST-only / symbol=%s", settings.symbol)

    runtime_state_store = RuntimeStateStore(settings.runtime_state_path)
    if args.reset_state:
        runtime_state_store.clear()
        logger.info("로컬 runtime_state.json을 초기화했습니다.")

    token_manager = TokenManager(settings=settings, logger=logger)
    api_client = KISApiClient(
        settings=settings,
        token_manager=token_manager,
        logger=logger,
    )
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
        api_client=api_client,
        market_data=market_data,
        account_client=account_client,
        order_client=order_client,
        runtime_state_store=runtime_state_store,
        trade_recorder=TradeRecordWriter(settings.records_dir),
        max_round_trips=max_round_trips,
        resume_buy_order_no=args.resume_buy_order_no,
        resume_order_date=args.resume_order_date,
        poll_interval_seconds=settings.poll_interval_seconds,
        logger=logger,
    )

    try:
        final_state = trader.run(run_once=args.run_once)
        logger.info(
            "최종 상태: %s, 왕복 완료=%s/%s",
            final_state.state,
            final_state.completed_round_trips,
            final_state.max_round_trips,
        )
        if final_state.state == "FAILED":
            raise SystemExit(1)
    except KISApiError as exc:
        logger.error("KIS API 오류로 종료합니다: %s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        logger.info("사용자 중단으로 상태를 보존하고 종료합니다.")


if __name__ == "__main__":
    main()
