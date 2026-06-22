from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from config import Settings


SEOUL_TZ = ZoneInfo("Asia/Seoul")
TOKEN_EXPIRY_MARGIN = timedelta(seconds=60)


@dataclass(frozen=True)
class TokenState:
    access_token: str
    issued_date: str
    expires_at: str


class TokenManager:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        session: requests.Session | None = None,
    ) -> None:
        self._settings = settings
        self._logger = logger
        self._session = session or requests.Session()
        self._token_state: TokenState | None = None

    def get_token(self, force_refresh: bool = False) -> str:
        now = datetime.now(SEOUL_TZ)

        if not force_refresh and self._token_state is not None:
            if self._is_valid(self._token_state, now):
                self._logger.info("토큰 재사용: 메모리 캐시를 사용합니다.")
                return self._token_state.access_token

        if not force_refresh:
            cached = self._load_cached_token()
            if cached is not None and self._is_valid(cached, now):
                self._token_state = cached
                self._logger.info("토큰 재사용: 유효한 파일 캐시를 사용합니다.")
                return cached.access_token
            if cached is not None:
                self._logger.info("토큰 캐시 만료: 새 토큰을 발급합니다.")
                self.invalidate(remove_cache=True)

        token = self._request_new_token()
        self._token_state = token
        self._save_token(token)
        return token.access_token

    def invalidate(self, remove_cache: bool = True) -> None:
        self._token_state = None
        if remove_cache:
            try:
                self._settings.token_cache_path.unlink(missing_ok=True)
            except OSError as exc:
                self._logger.warning("토큰 캐시 삭제 실패: %s", exc)

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
            json=payload,
            timeout=self._settings.http_timeout_seconds,
        )
        response.raise_for_status()

        data: dict[str, Any] = response.json()
        access_token = str(data.get("access_token", "")).strip()
        expires_at = str(data.get("access_token_token_expired", "")).strip()
        if not access_token:
            raise RuntimeError("Token response is missing access_token.")
        if not expires_at or self._parse_expiry(expires_at) is None:
            raise RuntimeError("Token response is missing a parseable expiration time.")

        issued_date = datetime.now(SEOUL_TZ).date().isoformat()
        self._logger.info("토큰 발급 완료")
        return TokenState(
            access_token=access_token,
            issued_date=issued_date,
            expires_at=expires_at,
        )

    def _load_cached_token(self) -> TokenState | None:
        path = self._settings.token_cache_path
        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            token = str(raw.get("access_token", "")).strip()
            issued_date = str(raw.get("issued_date", "")).strip()
            expires_at = str(raw.get("expires_at", "")).strip()
            if not token or not issued_date or not expires_at:
                return None
            return TokenState(
                access_token=token,
                issued_date=issued_date,
                expires_at=expires_at,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._logger.warning("토큰 캐시 읽기 실패: %s", exc)
            return None

    def _save_token(self, token: TokenState) -> None:
        payload = {
            "access_token": token.access_token,
            "issued_date": token.issued_date,
            "expires_at": token.expires_at,
            "saved_at": datetime.now(SEOUL_TZ).isoformat(timespec="seconds"),
        }
        path = self._settings.token_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _is_valid(self, token: TokenState, now: datetime) -> bool:
        expiry = self._parse_expiry(token.expires_at)
        if expiry is None:
            return False
        return now + TOKEN_EXPIRY_MARGIN < expiry

    @staticmethod
    def _parse_expiry(raw: str) -> datetime | None:
        value = raw.strip()
        if not value:
            return None

        candidates = [value]
        if value.endswith("Z"):
            candidates.insert(0, value[:-1] + "+00:00")

        for candidate in candidates:
            try:
                parsed = datetime.fromisoformat(candidate)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=SEOUL_TZ)
                return parsed.astimezone(SEOUL_TZ)
            except ValueError:
                pass

        for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=SEOUL_TZ)
            except ValueError:
                continue
        return None
