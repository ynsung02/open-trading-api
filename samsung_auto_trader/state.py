from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import csv
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SEOUL_TZ = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class RuntimeState:
    state: str
    symbol: str
    buy_order_no: str = ""
    buy_order_date: str = ""
    sell_order_no: str = ""
    sell_order_date: str = ""
    requested_qty: int = 0
    requested_buy_price: int = 0
    requested_sell_price: int = 0
    buy_filled_qty: int = 0
    buy_average_price: float = 0.0
    sell_filled_qty: int = 0
    sell_average_price: float = 0.0
    completed_round_trips: int = 0
    max_round_trips: int = 1
    round_trip_id: int = 1
    last_error: str = ""
    updated_at: str = ""


class RuntimeStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RuntimeState | None:
        if not self._path.exists():
            return None

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"runtime_state.json을 읽을 수 없습니다: {exc}") from exc

        # Backward-compatible migration from the earlier draft schema.
        legacy_quantity = self._to_int(data.get("quantity"))
        legacy_price = self._to_int(data.get("price"))
        state = str(data.get("state", "IDLE")).strip() or "IDLE"

        requested_buy_price = self._to_int(data.get("requested_buy_price"))
        requested_sell_price = self._to_int(data.get("requested_sell_price"))
        if legacy_price and not requested_buy_price and state in {"BUY_PENDING", "HOLDING"}:
            requested_buy_price = legacy_price
        if legacy_price and not requested_sell_price and state == "SELL_PENDING":
            requested_sell_price = legacy_price

        completed = self._to_int(data.get("completed_round_trips"))
        max_round_trips = max(1, self._to_int(data.get("max_round_trips")) or 1)

        return RuntimeState(
            state=state,
            symbol=str(data.get("symbol", "")).strip(),
            buy_order_no=str(data.get("buy_order_no", "")).strip(),
            buy_order_date=str(data.get("buy_order_date", "")).strip(),
            sell_order_no=str(data.get("sell_order_no", "")).strip(),
            sell_order_date=str(data.get("sell_order_date", "")).strip(),
            requested_qty=self._to_int(data.get("requested_qty")) or legacy_quantity,
            requested_buy_price=requested_buy_price,
            requested_sell_price=requested_sell_price,
            buy_filled_qty=self._to_int(data.get("buy_filled_qty")),
            buy_average_price=self._to_float(data.get("buy_average_price")),
            sell_filled_qty=self._to_int(data.get("sell_filled_qty")),
            sell_average_price=self._to_float(data.get("sell_average_price")),
            completed_round_trips=completed,
            max_round_trips=max_round_trips,
            round_trip_id=max(1, self._to_int(data.get("round_trip_id")) or completed + 1),
            last_error=str(data.get("last_error", "")).strip(),
            updated_at=str(data.get("updated_at", "")).strip(),
        )

    def save(self, runtime_state: RuntimeState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(runtime_state)
        payload["updated_at"] = datetime.now(SEOUL_TZ).isoformat(timespec="seconds")
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self._path)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)
        self._path.with_suffix(self._path.suffix + ".tmp").unlink(missing_ok=True)

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(str(value).replace(",", "").strip() or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(str(value).replace(",", "").strip() or 0)
        except (TypeError, ValueError):
            return 0.0


class TradeRecordWriter:
    FIELDNAMES = [
        "round_trip_id",
        "timestamp",
        "symbol",
        "side",
        "order_no",
        "requested_price",
        "requested_qty",
        "filled_qty",
        "average_filled_price",
        "remaining_qty",
        "rejected_qty",
        "cancelled",
        "status",
        "buy_average_price",
        "sell_average_price",
        "realized_profit_krw",
    ]

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def record(
        self,
        *,
        round_trip_id: int,
        timestamp: datetime,
        symbol: str,
        side: str,
        order_no: str,
        requested_price: int,
        requested_qty: int,
        filled_qty: int,
        average_filled_price: float,
        remaining_qty: int,
        rejected_qty: int,
        cancelled: str,
        status: str,
        buy_average_price: float = 0.0,
        sell_average_price: float = 0.0,
        realized_profit_krw: float | None = None,
    ) -> Path:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"trades_{timestamp.strftime('%Y%m%d')}.csv"

        # A completed/rejected/cancelled order is written once, even after restart.
        if order_no and self._already_recorded(path, order_no, side):
            return path

        file_exists = path.exists()
        with path.open("a", encoding="utf-8", newline="") as file_handle:
            writer = csv.DictWriter(file_handle, fieldnames=self.FIELDNAMES)
            if not file_exists:
                writer.writeheader()
            writer.writerow(
                {
                    "round_trip_id": round_trip_id,
                    "timestamp": timestamp.isoformat(timespec="seconds"),
                    "symbol": symbol,
                    "side": side,
                    "order_no": order_no,
                    "requested_price": requested_price,
                    "requested_qty": requested_qty,
                    "filled_qty": filled_qty,
                    "average_filled_price": average_filled_price,
                    "remaining_qty": remaining_qty,
                    "rejected_qty": rejected_qty,
                    "cancelled": cancelled,
                    "status": status,
                    "buy_average_price": buy_average_price or "",
                    "sell_average_price": sell_average_price or "",
                    "realized_profit_krw": "" if realized_profit_krw is None else realized_profit_krw,
                }
            )
        return path

    @staticmethod
    def _already_recorded(path: Path, order_no: str, side: str) -> bool:
        if not path.exists():
            return False
        try:
            with path.open("r", encoding="utf-8", newline="") as file_handle:
                for row in csv.DictReader(file_handle):
                    if row.get("order_no") == order_no and row.get("side") == side:
                        return True
        except (OSError, csv.Error):
            return False
        return False
