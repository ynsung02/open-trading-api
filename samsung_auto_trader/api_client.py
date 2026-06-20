from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from auth import TokenManager
from config import Settings


class KISApiError(RuntimeError):
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
    def __init__(self, settings: Settings, token_manager: TokenManager, logger: logging.Logger, session: requests.Session | None = None) -> None:
        self._settings = settings
        self._token_manager = token_manager
        self._logger = logger
        self._session = session or requests.Session()

    def get(self, path: str, tr_id: str, params: dict[str, Any]) -> KISResponse:
        return self._request("GET", path, tr_id, params=params, retry_on_timeout=True)

    def post(self, path: str, tr_id: str, body: dict[str, Any]) -> KISResponse:
        return self._request("POST", path, tr_id, json_body=body, retry_on_timeout=False)

    def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_on_timeout: bool = True,
    ) -> KISResponse:
        attempts = self._settings.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            token = self._token_manager.get_token(force_refresh=False)
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
                self._logger.info("API 호출 [%s] %s", method, path)
                response = self._session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    data=json.dumps(json_body) if json_body is not None else None,
                    timeout=self._settings.http_timeout_seconds,
                )
                response.raise_for_status()

                payload = response.json()
                kis_response = KISResponse(raw=payload)
                if kis_response.rt_cd != "0":
                    if kis_response.msg_cd in {"EGW00123", "EGW00121", "EGW00113"}:
                        self._logger.info("토큰 만료 감지: 새 토큰으로 1회 재시도합니다.")
                        self._token_manager.invalidate()
                        self._token_manager.get_token(force_refresh=True)
                        continue
                    raise KISApiError(f"KIS error {kis_response.msg_cd}: {kis_response.msg1}")

                return kis_response
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                self._logger.warning("API 호출 실패(시도 %s/%s): %s", attempt, attempts, exc)
                if not retry_on_timeout or attempt >= attempts:
                    break
                if attempt < attempts:
                    time.sleep(min(2 ** (attempt - 1), 5))
                continue
            except requests.HTTPError as exc:
                last_error = exc
                self._logger.warning("HTTP 오류(시도 %s/%s): %s", attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(min(2 ** (attempt - 1), 5))
                continue
            except ValueError as exc:
                last_error = exc
                self._logger.error("JSON 파싱 실패: %s", exc)
                raise KISApiError(f"Invalid JSON response from {path}") from exc

        raise KISApiError(f"API request failed after retries: {last_error}")
