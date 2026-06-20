from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests

from config import Settings


@dataclass(frozen=True)
class TokenState:
    access_token: str
    issued_date: str
    expires_at: str | None = None


class TokenManager:
    def __init__(self, settings: Settings, logger: logging.Logger, session: requests.Session | None = None) -> None:
        self._settings = settings
        self._logger = logger
        self._session = session or requests.Session()
        self._token: str | None = None

    def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._load_cached_token()
            if cached is not None and cached.issued_date == date.today().isoformat():
                self._token = cached.access_token
                self._logger.info("토큰 재사용: 같은 날짜 캐시를 사용합니다.")
                return cached.access_token

            if cached is not None:
                self._logger.info("토큰 캐시 만료: 새 토큰을 발급합니다.")

        token = self._request_new_token()
        self._token = token.access_token
        self._save_token(token)
        return token.access_token

    def invalidate(self) -> None:
        self._token = None

    def _request_new_token(self) -> TokenState:
        url = f"{self._settings.base_url}/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        payload = {
            "grant_type": "client_credentials",
            "appkey": self._settings.app_key,
            "appsecret": self._settings.app_secret,
        }

        self._logger.info("토큰 갱신 요청")
        response = self._session.post(
            url,
            headers=headers,
            data=json.dumps(payload),
            timeout=self._settings.http_timeout_seconds,
        )
        response.raise_for_status()

        data: dict[str, Any] = response.json()
        access_token = data.get("access_token", "").strip()
        if not access_token:
            raise RuntimeError(f"Token response missing access_token: {data}")

        issued_date = date.today().isoformat()
        expires_at = data.get("access_token_token_expired")
        self._logger.info("토큰 발급 완료")
        return TokenState(access_token=access_token, issued_date=issued_date, expires_at=expires_at)

    def _load_cached_token(self) -> TokenState | None:
        path = self._settings.token_cache_path
        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            token = str(raw.get("access_token", "")).strip()
            issued_date = str(raw.get("issued_date", "")).strip()
            expires_at = raw.get("expires_at")
            if not token or not issued_date:
                return None
            return TokenState(access_token=token, issued_date=issued_date, expires_at=expires_at)
        except Exception as exc:
            self._logger.warning("토큰 캐시 읽기 실패: %s", exc)
            return None

    def _save_token(self, token: TokenState) -> None:
        payload = {
            "access_token": token.access_token,
            "issued_date": token.issued_date,
            "expires_at": token.expires_at,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._settings.token_cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
