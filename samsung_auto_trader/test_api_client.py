from __future__ import annotations

from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from api_client import (
    KISApiClient,
    KISOrderError,
)
from config import Settings


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class KISApiClientTests(TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            account_no="12345678",
            account_prod="01",
            app_key="app-key",
            app_secret="app-secret",
            max_retries=1,
            token_cache_path=Path("/tmp/kis-token-cache.json"),
            runtime_state_path=Path("/tmp/kis-runtime-state.json"),
            records_dir=Path("/tmp/kis-records"),
            log_path=Path("/tmp/kis-trader.log"),
        )
        self.logger = Mock()
        self.token_manager = Mock()
        self.token_manager.get_token.return_value = "token"

    def _client(
        self,
        session: Mock,
        *,
        clock: FakeClock | None = None,
    ) -> KISApiClient:
        if clock is None:
            return KISApiClient(
                settings=self.settings,
                token_manager=self.token_manager,
                logger=self.logger,
                session=session,
                sleep_fn=lambda _: None,
                min_request_interval_seconds=0,
            )
        return KISApiClient(
            settings=self.settings,
            token_manager=self.token_manager,
            logger=self.logger,
            session=session,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            min_request_interval_seconds=1.1,
        )

    @staticmethod
    def _response(status: int, payload: object | None) -> Mock:
        response = Mock()
        response.status_code = status
        if payload is None:
            response.json.side_effect = ValueError("not json")
        else:
            response.json.return_value = payload
        return response

    def test_post_http_500_json_business_error_is_not_retried(self) -> None:
        session = Mock()
        session.request.return_value = self._response(
            500, {"rt_cd": "1", "msg_cd": "E12345", "msg1": "주문 거절"}
        )
        client = self._client(session)

        with self.assertRaisesRegex(
            KISOrderError, "KIS order rejected: E12345: 주문 거절"
        ):
            client.post(
                path="/uapi/domestic-stock/v1/trading/order-cash",
                tr_id="VTTC0012U",
                body={"CANO": "12345678"},
            )

        self.assertEqual(session.request.call_count, 1)

    def test_post_http_500_non_json_is_not_retried(self) -> None:
        session = Mock()
        session.request.return_value = self._response(500, None)
        client = self._client(session)

        with self.assertRaisesRegex(KISOrderError, "주문 결과가 불명확"):
            client.post(
                path="/uapi/domestic-stock/v1/trading/order-cash",
                tr_id="VTTC0012U",
                body={"CANO": "12345678"},
            )

        self.assertEqual(session.request.call_count, 1)

    def test_get_http_500_non_json_retries_then_succeeds(self) -> None:
        session = Mock()
        session.request.side_effect = [
            self._response(500, None),
            self._response(200, {"rt_cd": "0", "msg_cd": "", "msg1": "", "output": {"ok": 1}}),
        ]
        client = self._client(session)

        result = client.get(path="/test", tr_id="TEST", params={})

        self.assertEqual(result.output, {"ok": 1})
        self.assertEqual(session.request.call_count, 2)

    def test_get_expired_token_refreshes_once_then_succeeds(self) -> None:
        session = Mock()
        session.request.side_effect = [
            self._response(
                500,
                {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."},
            ),
            self._response(200, {"rt_cd": "0", "msg_cd": "", "msg1": "", "output": {"ok": 1}}),
        ]
        self.token_manager.get_token.side_effect = ["expired", "fresh"]
        client = self._client(session)

        result = client.get(path="/test", tr_id="TEST", params={})

        self.assertEqual(result.output, {"ok": 1})
        self.assertEqual(session.request.call_count, 2)
        self.token_manager.invalidate.assert_called_once_with(remove_cache=True)
        self.assertEqual(
            self.token_manager.get_token.call_args_list[1].kwargs,
            {"force_refresh": True},
        )

    def test_post_expired_token_is_not_retried(self) -> None:
        session = Mock()
        session.request.return_value = self._response(
            500,
            {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."},
        )
        client = self._client(session)

        with self.assertRaisesRegex(KISOrderError, "재시도하지 않습니다"):
            client.post(
                path="/uapi/domestic-stock/v1/trading/order-cash",
                tr_id="VTTC0012U",
                body={},
            )

        self.assertEqual(session.request.call_count, 1)
        self.token_manager.invalidate.assert_not_called()
    def test_consecutive_requests_are_spaced_globally(self) -> None:
        clock = FakeClock()
        session = Mock()
        request_times: list[float] = []

        def respond(**_: object) -> Mock:
            request_times.append(clock.now)
            return self._response(
                200,
                {"rt_cd": "0", "msg_cd": "", "msg1": "", "output": {}},
            )

        session.request.side_effect = respond
        client = self._client(session, clock=clock)

        client.get(path="/one", tr_id="ONE", params={})
        client.get(path="/two", tr_id="TWO", params={})

        self.assertEqual(session.request.call_count, 2)
        self.assertGreaterEqual(request_times[1] - request_times[0], 1.1)

    def test_get_rate_limit_retries_after_wait_then_succeeds(self) -> None:
        clock = FakeClock()
        session = Mock()
        session.request.side_effect = [
            self._response(
                500,
                {
                    "rt_cd": "1",
                    "msg_cd": "EGW00201",
                    "msg1": "초당 거래건수를 초과하였습니다.",
                },
            ),
            self._response(
                200,
                {"rt_cd": "0", "msg_cd": "", "msg1": "", "output": {"ok": 1}},
            ),
        ]
        client = self._client(session, clock=clock)

        result = client.get(path="/test", tr_id="TEST", params={})

        self.assertEqual(result.output, {"ok": 1})
        self.assertEqual(session.request.call_count, 2)
        self.assertGreaterEqual(sum(clock.sleeps), 1.1)

    def test_post_rate_limit_is_not_retried(self) -> None:
        session = Mock()
        session.request.return_value = self._response(
            500,
            {
                "rt_cd": "1",
                "msg_cd": "EGW00201",
                "msg1": "초당 거래건수를 초과하였습니다.",
            },
        )
        client = self._client(session)

        with self.assertRaisesRegex(KISOrderError, "재시도하지 않습니다"):
            client.post(
                path="/uapi/domestic-stock/v1/trading/order-cash",
                tr_id="VTTC0011U",
                body={},
            )

        self.assertEqual(session.request.call_count, 1)
