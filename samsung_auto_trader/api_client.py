from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from auth import TokenManager
from config import Settings


TOKEN_EXPIRED_CODE = "EGW00123"
RATE_LIMIT_CODE = "EGW00201"
# Mock REST access is intentionally throttled conservatively.
MIN_REQUEST_INTERVAL_SECONDS = 1.1


class KISApiError(RuntimeError):
    pass


class KISQueryError(KISApiError):
    pass


class KISTransientApiError(KISApiError):
    pass


class KISOrderError(KISApiError):
    pass


@dataclass(frozen=True)
class KISResponse:
    raw: dict[str, Any]

    @property
    def rt_cd(self) -> str:
        return str(self.raw.get("rt_cd", ""))

    @property
    def msg_cd(self) -> str:
        return str(self.raw.get("msg_cd", ""))

    @property
    def msg1(self) -> str:
        return str(self.raw.get("msg1", ""))

    @property
    def output(self) -> Any:
        return self.raw.get("output")

    @property
    def output1(self) -> Any:
        return self.raw.get("output1")

    @property
    def output2(self) -> Any:
        return self.raw.get("output2")


class KISApiClient:
    def __init__(
        self,
        settings: Settings,
        token_manager: TokenManager,
        logger: logging.Logger,
        session: requests.Session | None = None,
        sleep_fn: Any = time.sleep,
        monotonic_fn: Any = time.monotonic,
        min_request_interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings
        self._token_manager = token_manager
        self._logger = logger
        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must be 0 or greater.")

        self._session = session or requests.Session()
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._min_request_interval_seconds = min_request_interval_seconds
        self._last_request_started_at: float | None = None

    def get(self, path: str, tr_id: str, params: dict[str, Any]) -> KISResponse:
        return self._request("GET", path, tr_id, params=params)

    def post(self, path: str, tr_id: str, body: dict[str, Any]) -> KISResponse:
        return self._request("POST", path, tr_id, json_body=body)

    def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> KISResponse:
        method = method.upper()
        is_get = method == "GET"
        remaining_transient_retries = self._settings.max_retries if is_get else 0
        token_retry_used = False
        force_refresh = False

        while True:
            token = self._token_manager.get_token(force_refresh=force_refresh)
            force_refresh = False
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": self._settings.app_key,
                "appsecret": self._settings.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
            }
            url = f"{self._settings.base_url}{path}"

            try:
                self._wait_for_request_slot()
                self._logger.info("API 호출 [%s] %s", method, path)
                response = self._session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self._settings.http_timeout_seconds,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if is_get and remaining_transient_retries > 0:
                    remaining_transient_retries -= 1
                    self._warn_and_backoff("조회 API 통신 오류", exc, remaining_transient_retries)
                    continue
                if is_get:
                    raise KISTransientApiError(
                        self._sanitize_error(f"조회 API 일시 오류: {type(exc).__name__} (path={path})")
                    ) from exc
                raise KISOrderError(
                    self._sanitize_error(
                        "주문 결과가 불명확하므로 주문내역을 확인하세요 "
                        f"({type(exc).__name__}, path={path})"
                    )
                ) from exc
            except requests.RequestException as exc:
                if is_get and remaining_transient_retries > 0:
                    remaining_transient_retries -= 1
                    self._warn_and_backoff("조회 API 요청 오류", exc, remaining_transient_retries)
                    continue
                if is_get:
                    raise KISTransientApiError(
                        self._sanitize_error(f"조회 API 요청 오류: {type(exc).__name__} (path={path})")
                    ) from exc
                raise KISOrderError(
                    self._sanitize_error(
                        "주문 결과가 불명확하므로 주문내역을 확인하세요 "
                        f"({type(exc).__name__}, path={path})"
                    )
                ) from exc

            payload = self._safe_json(response)
            kis_response = KISResponse(payload) if payload is not None else None

            if kis_response is not None and kis_response.msg_cd == TOKEN_EXPIRED_CODE:
                if is_get and not token_retry_used:
                    self._logger.info("조회 토큰 만료 감지: 토큰을 갱신하고 1회 재시도합니다.")
                    self._token_manager.invalidate(remove_cache=True)
                    token_retry_used = True
                    force_refresh = True
                    continue
                message = self._build_kis_error_message(path, kis_response)
                if is_get:
                    raise KISQueryError(message)
                # Never retry an order POST, even for an expired token.
                raise KISOrderError(
                    self._sanitize_error(
                        f"주문 토큰 오류로 재시도하지 않습니다. 주문내역을 확인하세요. ({message})"
                    )
                )

            if kis_response is not None and kis_response.msg_cd == RATE_LIMIT_CODE:
                message = self._build_kis_error_message(path, kis_response)
                if is_get and remaining_transient_retries > 0:
                    remaining_transient_retries -= 1
                    self._warn_and_backoff(
                        "조회 API 호출 제한",
                        KISTransientApiError(message),
                        remaining_transient_retries,
                    )
                    continue
                if is_get:
                    raise KISTransientApiError(message)
                # Never retry an order POST. A repeated order can duplicate execution.
                raise KISOrderError(
                    self._sanitize_error(
                        f"주문 호출 제한으로 재시도하지 않습니다. ({message})"
                    )
                )

            if response.status_code >= 400:
                if kis_response is not None and (kis_response.msg_cd or kis_response.msg1):
                    message = self._build_kis_error_message(path, kis_response)
                    if is_get:
                        raise KISQueryError(message)
                    raise KISOrderError(
                        self._sanitize_error(
                            f"KIS order rejected: {kis_response.msg_cd}: {kis_response.msg1}"
                        )
                    )

                if is_get and self._is_retryable_status(response.status_code):
                    if remaining_transient_retries > 0:
                        remaining_transient_retries -= 1
                        self._warn_and_backoff(
                            f"조회 API HTTP {response.status_code}",
                            None,
                            remaining_transient_retries,
                        )
                        continue
                    raise KISTransientApiError(
                        self._sanitize_error(
                            f"조회 API 일시 오류: HTTP {response.status_code} (path={path})"
                        )
                    )

                if is_get:
                    raise KISQueryError(
                        self._sanitize_error(
                            f"조회 API HTTP 오류: {response.status_code} (path={path})"
                        )
                    )
                raise KISOrderError(
                    "주문 결과가 불명확하므로 주문내역을 확인하세요"
                )

            if kis_response is None:
                if is_get:
                    raise KISQueryError(
                        self._sanitize_error(f"조회 API JSON 파싱 실패 (path={path})")
                    )
                raise KISOrderError(
                    "주문 결과가 불명확하므로 주문내역을 확인하세요"
                )

            if kis_response.rt_cd != "0":
                message = self._build_kis_error_message(path, kis_response)
                if is_get:
                    raise KISQueryError(message)
                raise KISOrderError(
                    self._sanitize_error(
                        f"KIS order rejected: {kis_response.msg_cd}: {kis_response.msg1}"
                    )
                )

            return kis_response

    def _wait_for_request_slot(self) -> None:
        """Keep every REST request start at least the configured interval apart."""
        now = self._monotonic()
        if self._last_request_started_at is not None:
            remaining = (
                self._min_request_interval_seconds
                - (now - self._last_request_started_at)
            )
            if remaining > 0:
                self._logger.info("API 호출 간격 보호: %.2f초 대기", remaining)
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request_started_at = now

    def _warn_and_backoff(
        self,
        label: str,
        exc: Exception | None,
        remaining_retries: int,
    ) -> None:
        detail = "" if exc is None else f": {self._sanitize_error(str(exc))}"
        self._logger.warning(
            "%s%s (남은 재시도 %s회)",
            label,
            detail,
            remaining_retries,
        )
        used_retries = max(1, self._settings.max_retries - remaining_retries)
        self._sleep(min(2 ** (used_retries - 1), 5))

    @staticmethod
    def _safe_json(response: requests.Response) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code < 600

    @staticmethod
    def _sanitize_error(text: str) -> str:
        sanitized = re.sub(r"(CANO=)\d{8}", r"\1********", text)
        sanitized = re.sub(r"(ACNT_PRDT_CD=)\d{2}", r"\1**", sanitized)
        sanitized = re.sub(r"\b\d{8}-\d{2}\b", "********-**", sanitized)
        return sanitized

    def _build_kis_error_message(self, path: str, response: KISResponse) -> str:
        return self._sanitize_error(
            f"KIS error {response.msg_cd}: {response.msg1} (path={path})"
        )
