import unittest
from decimal import Decimal

from tools.portfolio_monitor import (
    Snapshot,
    active_drawdown_level,
    active_price_level,
    evaluate,
    parse_tencent_line,
)


ITEM = {
    "name": "Example",
    "symbol": "EX",
    "price_levels": [
        {"id": "p1", "label": "first", "at_or_below": 100, "action": "review"},
        {"id": "p2", "label": "second", "at_or_below": 80, "action": "review"},
    ],
    "drawdown_levels": [
        {"id": "d1", "label": "first", "at_or_above_pct": 10, "action": "review"},
        {"id": "d2", "label": "second", "at_or_above_pct": 20, "action": "review"},
    ],
}


class PortfolioMonitorTest(unittest.TestCase):
    def test_parse_tencent_us_quote(self):
        fields = [""] * 60
        fields[3] = "326.56"
        fields[30] = "2026-07-27 16:00:01"
        fields[35] = "USD"
        fields[48] = "408.36"
        fields[49] = "187.28"
        snapshot = parse_tencent_line(
            'v_usGOOGL="' + "~".join(fields) + '";',
            quote_code="usGOOGL",
            currency="USD",
        )
        self.assertEqual(snapshot.price, Decimal("326.56"))
        self.assertEqual(snapshot.high_52w, Decimal("408.36"))
        self.assertEqual(snapshot.currency, "USD")

    def test_parse_tencent_ashare_quote(self):
        fields = [""] * 60
        fields[3] = "1320.00"
        fields[30] = "20260728161459"
        fields[47] = "1418.45"
        fields[48] = "1160.55"
        snapshot = parse_tencent_line(
            'v_sh600519="' + "~".join(fields) + '";',
            quote_code="sh600519",
            currency="CNY",
        )
        self.assertEqual(snapshot.price, Decimal("1320.00"))
        self.assertEqual(snapshot.high_52w, Decimal("1418.45"))
        self.assertEqual(snapshot.currency, "CNY")

    def test_deepest_price_level_wins(self):
        level = active_price_level(ITEM, Decimal("75"))
        self.assertEqual(level["id"], "p2")
        self.assertEqual(level["severity"], 2)

    def test_deepest_drawdown_level_wins(self):
        level = active_drawdown_level(ITEM, Decimal("25"))
        self.assertEqual(level["id"], "d2")
        self.assertEqual(level["severity"], 2)

    def test_same_level_does_not_repeat(self):
        snapshot = Snapshot(
            price=Decimal("90"),
            high_52w=Decimal("100"),
            currency="USD",
            as_of="2026-07-28",
            source="test",
        )
        config = {"watchlist": [ITEM]}
        first_alerts, _, state = evaluate(config, {"EX": snapshot}, {}, False)
        second_alerts, _, _ = evaluate(config, {"EX": snapshot}, state, False)
        self.assertEqual(len(first_alerts), 2)
        self.assertEqual(second_alerts, [])

    def test_deeper_level_creates_new_alert(self):
        first = Snapshot(
            price=Decimal("90"),
            high_52w=Decimal("100"),
            currency="USD",
            as_of="2026-07-28",
            source="test",
        )
        deeper = Snapshot(
            price=Decimal("75"),
            high_52w=Decimal("100"),
            currency="USD",
            as_of="2026-07-29",
            source="test",
        )
        config = {"watchlist": [ITEM]}
        _, _, state = evaluate(config, {"EX": first}, {}, False)
        alerts, _, _ = evaluate(config, {"EX": deeper}, state, False)
        self.assertEqual({alert["level"]["id"] for alert in alerts}, {"p2", "d2"})


if __name__ == "__main__":
    unittest.main()
