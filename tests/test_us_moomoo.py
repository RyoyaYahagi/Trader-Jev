from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from trader_jev.moomoo import MoomooClientConfig
from trader_jev.us_moomoo import (
    MoomooUSMarketAdapter,
    USMoomooConfig,
    USMoomooFailureKind,
    parse_screen_rows,
)


def test_parse_screen_rows_normalizes_factor_fields() -> None:
    rows = parse_screen_rows(
        [
            {
                "results": [
                    _result(1101, "US.AAPL"),
                    _result(1102, "Apple Inc."),
                    _result(2201, 212.5),
                    _result(2301, 3000000000000),
                    _result(2307, 12000),
                    _result(2217, 1.4),
                    _result(3102, 0.02, days=1),
                    _result(3102, 0.08, days=5),
                    _result(3103, 0.03, days=1),
                    _result(3104, 1000000, days=20),
                    _result(3105, 100000000, days=20),
                    _result(3107, -0.01, days=20),
                    _result(3108, 0.12, days=20),
                ]
            }
        ]
    )

    assert len(rows) == 1
    assert rows[0].code == "US.AAPL"
    assert rows[0].price == 212.5
    assert rows[0].avg_turnover_20d_usd == 100000000
    assert rows[0].price_change_5d == Decimal("0.08")


def test_minute_bar_subscription_obeys_remaining_quota_and_reuses_active_symbols(
    monkeypatch: Any,
) -> None:
    context = _FakeQuoteContext()
    sdk = SimpleNamespace(
        SubType=SimpleNamespace(K_1M="K_1M"),
        KLType=SimpleNamespace(K_1M="K_1M"),
        AuType=SimpleNamespace(QFQ="QFQ"),
    )
    monkeypatch.setattr(MoomooUSMarketAdapter, "_sdk", staticmethod(lambda: sdk))
    adapter = MoomooUSMarketAdapter(
        connection=MoomooClientConfig(),
        config=USMoomooConfig(retry_attempts=0),
        context_factory=lambda _config: context,
        sleep=lambda _seconds: None,
    )

    with adapter:
        first = adapter.fetch_minute_bars(("US.AAPL", "US.MSFT"), count=2)
        second = adapter.fetch_minute_bars(("US.AAPL",), count=2)

    assert tuple(first) == ("US.AAPL",)
    assert tuple(second) == ("US.AAPL",)
    assert len(context.subscriptions) == 1
    assert context.subscriptions[0][0] == ["US.AAPL"]
    assert not context.unsubscriptions
    assert context.closed
    assert any(error.kind == USMoomooFailureKind.SUBSCRIPTION_QUOTA for error in adapter.errors)


def _result(property_id: int, value: object, *, days: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "property": {"name": property_id},
        "sval": value,
    }
    if days is not None:
        result["property"]["days"] = days
    return result


class _FakeQuoteContext:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[list[str], list[object]]] = []
        self.unsubscriptions: list[tuple[list[str], list[object]]] = []
        self.closed = False

    def get_stock_basicinfo(self, market: object, stock_type: object) -> tuple[int, list[object]]:
        del market, stock_type
        return 0, []

    def get_stock_screen(self, request: object) -> tuple[int, list[object]]:
        del request
        return 0, []

    def get_market_snapshot(self, code_list: list[str]) -> tuple[int, list[object]]:
        del code_list
        return 0, []

    def query_subscription(self, is_all_conn: bool = True) -> tuple[int, dict[str, int]]:
        assert is_all_conn
        return 0, {"remain": 1}

    def subscribe(
        self,
        code_list: list[str],
        subtype_list: list[object],
        **kwargs: object,
    ) -> tuple[int, dict[str, object]]:
        assert kwargs == {"is_first_push": False, "subscribe_push": False}
        self.subscriptions.append((code_list, subtype_list))
        return 0, {}

    def unsubscribe(
        self, code_list: list[str], subtype_list: list[object]
    ) -> tuple[int, dict[str, object]]:
        self.unsubscriptions.append((code_list, subtype_list))
        return 0, {}

    def get_cur_kline(
        self,
        code: str,
        num: int,
        ktype: object,
        autype: object,
    ) -> tuple[int, list[dict[str, object]]]:
        assert code == "US.AAPL"
        assert num == 2
        assert ktype == "K_1M"
        assert autype == "QFQ"
        return 0, [
            {
                "time_key": datetime(2026, 9, 24, 17, 30, tzinfo=UTC).isoformat(),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 1000,
                "turnover": 100500,
            }
        ]

    def close(self) -> None:
        self.closed = True

    def get_history_kl_quota(self, get_detail: bool = False) -> tuple[int, dict[str, int]]:
        del get_detail
        return 0, {"used_quota": 0, "remain_quota": 0}
