from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo

from state import RuntimeState, RuntimeStateStore, TradeRecordWriter


class StateTests(TestCase):
    def test_runtime_state_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "runtime_state.json")
            expected = RuntimeState(
                state="BUY_PENDING",
                symbol="005930",
                buy_order_no="0000012345",
                requested_qty=1,
                completed_round_trips=1,
                max_round_trips=3,
                round_trip_id=2,
            )

            store.save(expected)
            loaded = store.load()

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.buy_order_no, expected.buy_order_no)
            self.assertEqual(loaded.completed_round_trips, 1)
            self.assertEqual(loaded.max_round_trips, 3)

    def test_trade_record_is_deduplicated_and_contains_no_secrets(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = TradeRecordWriter(Path(tmp) / "records")
            timestamp = datetime(2026, 6, 22, 13, 0, tzinfo=ZoneInfo("Asia/Seoul"))
            kwargs = dict(
                round_trip_id=1,
                timestamp=timestamp,
                symbol="005930",
                side="BUY",
                order_no="0000012345",
                requested_price=350000,
                requested_qty=1,
                filled_qty=1,
                average_filled_price=350000.0,
                remaining_qty=0,
                rejected_qty=0,
                cancelled="N",
                status="FILLED",
                buy_average_price=350000.0,
            )

            path = writer.record(**kwargs)
            writer.record(**kwargs)

            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("app-secret", text)
            self.assertNotIn("authorization", text.lower())
            self.assertNotIn("12345678", text)
