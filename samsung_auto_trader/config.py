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
DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_TRADING_START = time(9, 10)
DEFAULT_TRADING_END = time(15, 30)

TOKEN_CACHE_PATH = PROJECT_ROOT / "token_cache.json"
RUNTIME_STATE_PATH = PROJECT_ROOT / "runtime_state.json"
RECORDS_DIR = PROJECT_ROOT / "records"
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
    runtime_state_path: Path = RUNTIME_STATE_PATH
    records_dir: Path = RECORDS_DIR
    log_path: Path = LOG_PATH


def _split_account(raw_account: str) -> tuple[str, str]:
    cleaned = raw_account.strip()
    for separator in ("-", "_", "/", " "):
        if separator in cleaned:
            left, right = cleaned.split(separator, 1)
            account_no = re.sub(r"\D", "", left)
            account_prod = re.sub(r"\D", "", right)
            if len(account_no) == 8 and len(account_prod) == 2:
                return account_no, account_prod
            break

    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return digits[:8], digits[8:]
    if len(digits) == 8:
        product_code = re.sub(r"\D", "", os.getenv("GH_ACCOUNT_PROD", "01"))
        if len(product_code) != 2:
            raise ValueError("GH_ACCOUNT_PROD must be a 2-digit product code.")
        return digits, product_code

    raise ValueError(
        "GH_ACCOUNT must be an 8-digit account number, optionally followed by a 2-digit product code."
    )


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0.")
    return value


def _nonnegative_int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value < 0:
        raise ValueError(f"{name} must be 0 or greater.")
    return value


def load_settings() -> Settings:
    app_key = os.getenv("GH_APPKEY", "").strip()
    app_secret = os.getenv("GH_APPSECRET", "").strip()
    account_raw = os.getenv("GH_ACCOUNT", "").strip()

    missing = [
        name
        for name, value in {
            "GH_ACCOUNT": account_raw,
            "GH_APPKEY": app_key,
            "GH_APPSECRET": app_secret,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    account_no, account_prod = _split_account(account_raw)

    try:
        timeout = float(os.getenv("KIS_HTTP_TIMEOUT_SECONDS", str(DEFAULT_HTTP_TIMEOUT_SECONDS)))
    except ValueError as exc:
        raise ValueError("KIS_HTTP_TIMEOUT_SECONDS must be numeric.") from exc
    if timeout <= 0:
        raise ValueError("KIS_HTTP_TIMEOUT_SECONDS must be greater than 0.")

    return Settings(
        account_no=account_no,
        account_prod=account_prod,
        app_key=app_key,
        app_secret=app_secret,
        # Mock-only by design. Do not make this configurable.
        base_url=MOCK_BASE_URL,
        symbol=DEFAULT_SYMBOL,
        order_qty=_positive_int_from_env("KIS_ORDER_QTY", DEFAULT_ORDER_QTY),
        order_offset_krw=_nonnegative_int_from_env("KIS_ORDER_OFFSET_KRW", DEFAULT_ORDER_OFFSET_KRW),
        poll_interval_seconds=_positive_int_from_env(
            "KIS_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS
        ),
        http_timeout_seconds=timeout,
        max_retries=_nonnegative_int_from_env("KIS_MAX_RETRIES", DEFAULT_MAX_RETRIES),
    )


def is_trading_window(now_time: time, start: time, end: time) -> bool:
    return start <= now_time < end
