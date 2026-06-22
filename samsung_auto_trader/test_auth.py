from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from auth import TokenManager
from config import Settings


class TokenManagerTests(TestCase):
    def _settings(self, root: Path) -> Settings:
        return Settings(
            account_no="12345678",
            account_prod="01",
            app_key="app-key",
            app_secret="app-secret",
            token_cache_path=root / "token_cache.json",
            runtime_state_path=root / "runtime_state.json",
            records_dir=root / "records",
            log_path=root / "trader.log",
        )

    def test_expired_cache_is_not_reused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            settings.token_cache_path.write_text(
                json.dumps(
                    {
                        "access_token": "expired",
                        "issued_date": "2026-06-22",
                        "expires_at": "2000-01-01 00:00:00",
                    }
                ),
                encoding="utf-8",
            )

            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "access_token": "fresh",
                "access_token_token_expired": "2099-01-01 00:00:00",
            }
            session = Mock()
            session.post.return_value = response

            manager = TokenManager(settings, Mock(), session=session)
            token = manager.get_token()

            self.assertEqual(token, "fresh")
            self.assertEqual(session.post.call_count, 1)

    def test_valid_cache_is_reused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            settings.token_cache_path.write_text(
                json.dumps(
                    {
                        "access_token": "cached",
                        "issued_date": "2026-06-22",
                        "expires_at": "2099-01-01 00:00:00",
                    }
                ),
                encoding="utf-8",
            )
            session = Mock()

            manager = TokenManager(settings, Mock(), session=session)
            token = manager.get_token()

            self.assertEqual(token, "cached")
            session.post.assert_not_called()

    def test_invalidate_removes_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            settings.token_cache_path.write_text("{}", encoding="utf-8")
            manager = TokenManager(settings, Mock(), session=Mock())

            manager.invalidate(remove_cache=True)

            self.assertFalse(settings.token_cache_path.exists())
