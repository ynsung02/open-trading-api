from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
import os
import re


PROJECT_ROOT = Path(__file__).resolve().parent
MOCK_BASE_URL = "https://openapivts.koreainvestment.com:29443"
DEFAULT_SYMBOL = "005930"
DEFAULT_ORDER_QTY = 1
DEFAULT_ORDER_OFFSET_KRW = 2000
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_TRADING_START = time(9, 10)
DEFAULT_TRADING_END = time(15, 30)

TOKEN_CACHE_PATH = PROJECT_ROOT / "token_cache.json"
LOG_PATH = PROJECT_ROOT / "trader.log"


@dataclass(frozen=True)
class Settings:
    account_no: str
    account_prod: str
    app_key: str
    app_secret: str
    base_url: str = MOCK_BASE_URL
    symbol: str = DEFAULT_SYMBOL
    order_qty: int = DEFAULT_ORDER_QTY
    order_offset_krw: int = DEFAULT_ORDER_OFFSET_KRW
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    trading_start: time = DEFAULT_TRADING_START
    trading_end: time = DEFAULT_TRADING_END
    token_cache_path: Path = TOKEN_CACHE_PATH
    log_path: Path = LOG_PATH


def _split_account(raw_account: str) -> tuple[str, str]:
    cleaned = raw_account.strip()
    for separator in ("-", "_", "/", " "):
        if separator in cleaned:
            left, right = cleaned.split(separator, 1)
            return left.strip().zfill(8), right.strip().zfill(2)

    digits = re.sub(r"\D", "", cleaned)
    if len(digits) >= 10:
        return digits[:8], digits[8:10]
    if len(digits) == 8:
        return digits, os.getenv("GH_ACCOUNT_PROD", "01").strip().zfill(2)

    raise ValueError(
        "GH_ACCOUNT must include an 8-digit account number, optionally followed by a 2-digit product code."
    )


def load_settings() -> Settings:
    app_key = os.getenv("GH_APPKEY", "").strip()
    app_secret = os.getenv("GH_APPSECRET", "").strip()
    account_raw = os.getenv("GH_ACCOUNT", "").strip()

    missing = [name for name, value in {
        "GH_ACCOUNT": account_raw,
        "GH_APPKEY": app_key,
        "GH_APPSECRET": app_secret,
    }.items() if not value]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    account_no, account_prod = _split_account(account_raw)

    return Settings(
        account_no=account_no,
        account_prod=account_prod,
        app_key=app_key,
        app_secret=app_secret,
        base_url=MOCK_BASE_URL,
        symbol=os.getenv("KIS_SYMBOL", DEFAULT_SYMBOL).strip() or DEFAULT_SYMBOL,
        order_qty=int(os.getenv("KIS_ORDER_QTY", str(DEFAULT_ORDER_QTY))),
        order_offset_krw=int(os.getenv("KIS_ORDER_OFFSET_KRW", str(DEFAULT_ORDER_OFFSET_KRW))),
        poll_interval_seconds=int(os.getenv("KIS_POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS))),
        http_timeout_seconds=float(os.getenv("KIS_HTTP_TIMEOUT_SECONDS", str(DEFAULT_HTTP_TIMEOUT_SECONDS))),
        max_retries=int(os.getenv("KIS_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))),
    )


def is_trading_window(now_time: time, start: time, end: time) -> bool:
    return start <= now_time < end


def seconds_until(target: time) -> int:
    from datetime import datetime, timedelta

    now = datetime.now()
    target_dt = datetime.combine(now.date(), target)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    return max(0, int((target_dt - now).total_seconds()))


def seconds_until_end(end: time) -> int:
    from datetime import datetime

    now = datetime.now()
    end_dt = datetime.combine(now.date(), end)
    return max(0, int((end_dt - now).total_seconds()))
