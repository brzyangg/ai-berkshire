import http.client
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from tools.portfolio_monitor import (
    Snapshot,
    active_drawdown_level,
    active_price_level,
    evaluate,
    fetch_auto_metrics,
    fetch_item_metrics,
    merge_metrics,
    metric_gate_status,
    parse_eastmoney_kline_high,
    parse_eastmoney_kline_snapshot,
    parse_fund_nav_html,
    parse_index_valuation_html,
    parse_tencent_kline_high,
    parse_tencent_line,
    render_dashboard,
    request_bytes,
    snapshots_from_state,
    verify_ashare_snapshot,
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
    @patch("tools.portfolio_monitor.time.sleep")
    @patch("tools.portfolio_monitor.urllib.request.urlopen")
    def test_request_retries_transient_disconnect(self, urlopen, sleep):
        response = MagicMock()
        response.read.return_value = b"ok"
        response.headers.get_content_charset.return_value = "utf-8"
        response.__enter__.return_value = response
        urlopen.side_effect = [
            http.client.RemoteDisconnected("temporary"),
            response,
        ]

        raw, encoding = request_bytes("https://example.test")

        self.assertEqual(raw, b"ok")
        self.assertEqual(encoding, "utf-8")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)

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
        fields = [""] * 90
        fields[3] = "1320.00"
        fields[30] = "20260728161459"
        fields[47] = "1453.10"
        fields[48] = "1188.90"
        fields[67] = "1539.98"
        fields[68] = "1151.01"
        snapshot = parse_tencent_line(
            'v_sh600519="' + "~".join(fields) + '";',
            quote_code="sh600519",
            currency="CNY",
        )
        self.assertEqual(snapshot.price, Decimal("1320.00"))
        self.assertEqual(snapshot.high_52w, Decimal("1539.98"))
        self.assertEqual(snapshot.currency, "CNY")

    def test_parse_tencent_ashare_etf_uses_52w_high_not_limit_up(self):
        fields = [""] * 90
        fields[3] = "7.294"
        fields[30] = "20260730111423"
        fields[47] = "8.294"
        fields[48] = "6.786"
        fields[61] = "ETF"
        fields[67] = "9.156"
        fields[68] = "6.039"
        snapshot = parse_tencent_line(
            'v_sh510500="' + "~".join(fields) + '";',
            quote_code="sh510500",
            currency="CNY",
        )
        self.assertEqual(snapshot.price, Decimal("7.294"))
        self.assertEqual(snapshot.high_52w, Decimal("9.156"))

    def test_parse_tencent_kline_high_uses_unadjusted_intraday_high(self):
        data = {
            "data": {
                "sh510500": {
                    "day": [
                        ["2025-07-30", "9.900", "9.900", "10.000", "9.800"],
                        ["2025-07-31", "8.900", "8.950", "9.000", "8.800"],
                        ["2026-07-01", "9.201", "9.180", "9.305", "9.100"],
                        ["2026-07-30", "7.490", "7.290", "7.560", "7.290"],
                    ]
                }
            }
        }
        high, as_of = parse_tencent_kline_high(data, "sh510500")
        self.assertEqual(high, Decimal("9.305"))
        self.assertEqual(as_of, "2026-07-30")

    def test_parse_eastmoney_kline_exposes_latest_close_and_precision(self):
        data = {
            "data": {
                "klines": [
                    "2026-07-29,7.300,7.310,7.350,7.280,1,1",
                    "2026-07-30,7.490,7.263,7.560,7.250,1,1",
                ]
            }
        }
        price, tick, high, as_of = parse_eastmoney_kline_snapshot(data)
        self.assertEqual(price, Decimal("7.263"))
        self.assertEqual(tick, Decimal("0.001"))
        self.assertEqual(high, Decimal("7.560"))
        self.assertEqual(as_of, "2026-07-30")

    def test_parse_eastmoney_kline_high_uses_unadjusted_intraday_high(self):
        data = {
            "data": {
                "klines": [
                    "2025-07-30,9.900,9.900,10.000,9.800,1,1",
                    "2025-07-31,8.900,8.950,9.000,8.800,1,1",
                    "2026-07-01,9.201,9.180,9.305,9.100,1,1",
                    "2026-07-30,7.490,7.290,7.560,7.290,1,1",
                ]
            }
        }
        high, as_of = parse_eastmoney_kline_high(data)
        self.assertEqual(high, Decimal("9.305"))
        self.assertEqual(as_of, "2026-07-30")

    def test_ashare_high_requires_matching_independent_source(self):
        snapshot = Snapshot(
            price=Decimal("7.263"),
            high_52w=Decimal("9.156"),
            currency="CNY",
            as_of="20260730150000",
            source="Tencent quote",
        )
        verified = verify_ashare_snapshot(
            snapshot,
            "sh510500",
            (Decimal("9.305"), "2026-07-30"),
            (
                Decimal("7.264"),
                Decimal("0.001"),
                Decimal("9.305"),
                "2026-07-30",
            ),
        )
        self.assertEqual(verified.high_52w, Decimal("9.305"))
        self.assertIn(
            "verified by Tencent+Eastmoney",
            verified.high_52w_basis,
        )

    def test_ashare_high_conflict_fails_closed(self):
        snapshot = Snapshot(
            price=Decimal("7.263"),
            high_52w=Decimal("9.156"),
            currency="CNY",
            as_of="20260730150000",
            source="Tencent quote",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "52-week unadjusted intraday high conflict",
        ):
            verify_ashare_snapshot(
                snapshot,
                "sh510500",
                (Decimal("9.156"), "2026-07-30"),
                (
                    Decimal("7.263"),
                    Decimal("0.001"),
                    Decimal("9.305"),
                    "2026-07-30",
                ),
            )

    def test_unverified_ashare_cache_is_rejected(self):
        state = {
            "symbols": {
                "510500.SS": {
                    "price": "7.263",
                    "high_52w": "9.305",
                    "currency": "CNY",
                    "as_of": "2026-07-30",
                    "high_52w_basis": (
                        "unadjusted intraday high through 2026-07-30"
                    ),
                }
            }
        }
        self.assertNotIn("510500.SS", snapshots_from_state(state))

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

    def test_all_metric_gate_passes_and_blocks(self):
        item = {
            "metric_gate_groups": [
                {
                    "mode": "all",
                    "checks": [
                        {
                            "metric": "premium_pct",
                            "operator": "<=",
                            "value": 1,
                            "label": "premium",
                        },
                        {
                            "metric": "subscription_open",
                            "operator": "==",
                            "value": True,
                            "label": "subscription",
                        },
                    ],
                }
            ]
        }
        passed = metric_gate_status(
            item,
            {"premium_pct": 0.5, "subscription_open": True},
        )
        blocked = metric_gate_status(
            item,
            {"premium_pct": 2.5, "subscription_open": True},
        )
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(blocked["status"], "blocked")

    def test_any_metric_gate_handles_missing_values(self):
        item = {
            "metric_gate_groups": [
                {
                    "mode": "any",
                    "checks": [
                        {"metric": "pe", "operator": "<=", "value": 12},
                        {
                            "metric": "valuation_percentile",
                            "operator": "<=",
                            "value": 30,
                        },
                    ],
                }
            ]
        }
        passed = metric_gate_status(item, {"pe": 11.5})
        unknown = metric_gate_status(item, {})
        blocked = metric_gate_status(
            item,
            {"pe": 14, "valuation_percentile": 80},
        )
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(blocked["status"], "blocked")

    def test_evaluate_attaches_metric_gate_to_alert(self):
        item = dict(
            ITEM,
            metric_gate_groups=[
                {
                    "mode": "all",
                    "checks": [
                        {"metric": "premium_pct", "operator": "<=", "value": 1}
                    ],
                }
            ],
        )
        snapshot = Snapshot(
            price=Decimal("90"),
            high_52w=Decimal("100"),
            currency="USD",
            as_of="2026-07-28",
            source="test",
        )
        config = {"watchlist": [item]}
        alerts, _, _ = evaluate(
            config,
            {"EX": snapshot},
            {},
            False,
            metrics={"EX": {"premium_pct": 2}},
        )
        self.assertTrue(alerts)
        self.assertTrue(
            all(alert["metric_gate"]["status"] == "blocked" for alert in alerts)
        )

    def test_separate_benchmark_drives_drawdown_level(self):
        item = dict(
            ITEM,
            benchmark={"name": "SPY benchmark", "symbol": "SPY"},
        )
        instrument = Snapshot(
            price=Decimal("95"),
            high_52w=Decimal("100"),
            currency="CNY",
            as_of="2026-07-30",
            source="test",
        )
        benchmark = Snapshot(
            price=Decimal("75"),
            high_52w=Decimal("100"),
            currency="USD",
            as_of="2026-07-29",
            source="test",
        )
        alerts, rows, _ = evaluate(
            {"watchlist": [item]},
            {"EX": instrument, "SPY": benchmark},
            {},
            False,
        )
        self.assertEqual(rows[0]["drawdown_level"]["id"], "d2")
        self.assertEqual(rows[0]["drawdown_name"], "SPY benchmark")
        self.assertEqual(
            {alert["level"]["id"] for alert in alerts},
            {"p1", "d2"},
        )

    def test_dashboard_explains_missing_metrics_without_wait_to_buy_wording(self):
        item = dict(
            ITEM,
            metric_gate_groups=[
                {
                    "mode": "all",
                    "checks": [
                        {
                            "metric": "premium_pct",
                            "operator": "<=",
                            "value": 1,
                            "label": "premium <= 1%",
                        }
                    ],
                }
            ],
        )
        snapshot = Snapshot(
            price=Decimal("90"),
            high_52w=Decimal("100"),
            currency="USD",
            as_of="2026-07-30",
            source="test",
        )
        _, rows, _ = evaluate(
            {"watchlist": [item]},
            {"EX": snapshot},
            {},
            False,
        )
        rendered = "\n".join(render_dashboard(rows))
        self.assertIn("[01] Example (EX)", rendered)
        self.assertIn("当前价格：90.00 USD", rendered)
        self.assertIn("指标核验：数据不可用", rendered)
        self.assertIn("行情来源：test；时间=2026-07-30", rendered)
        self.assertIn("premium <= 1%: 缺少数据", rendered)
        self.assertIn("已触发，但缺指标", rendered)
        self.assertNotIn("| 标的 |", rendered)
        self.assertNotIn("待补", rendered)

    def test_parse_index_valuation_html(self):
        raw = """
        <html><body>
          <p>最新交易日：2026-07-29</p>
          <section><h2><span>PE · 市盈率</span><em>偏热</em></h2>
          <strong>14.34</strong>
          <span>近 10 年百分位85.1%</span></section>
          <section><h2>PB · 市净率正常</h2><strong>1.46</strong></section>
        </body></html>
        """
        values, as_of = parse_index_valuation_html(raw)
        self.assertEqual(values["pe"], "14.34")
        self.assertEqual(values["valuation_percentile"], "85.1")
        self.assertEqual(as_of, "2026-07-29")

    def test_parse_fund_nav_html(self):
        raw = """
        <html><body>
          <div>单位净值：2.3848(0.235%)</div>
          <div>数据日期：2026-7-28</div>
          <div>开放申购：是</div>
        </body></html>
        """
        values, as_of = parse_fund_nav_html(raw)
        self.assertEqual(values["nav"], "2.3848")
        self.assertTrue(values["subscription_open"])
        self.assertEqual(as_of, "2026-07-28")

    @patch("tools.portfolio_monitor.request_text")
    def test_fund_nav_source_calculates_premium(self, request_text):
        request_text.return_value = """
        <div>单位净值：2.3848</div>
        <div>数据日期：2026-7-28</div>
        <div>开放申购：是</div>
        """
        item = {
            "metric_source": {
                "type": "fund_nav",
                "name": "test source",
                "url": "https://example.test/fund",
                "max_age_days": 5,
            }
        }
        snapshot = Snapshot(
            price=Decimal("2.48"),
            high_52w=Decimal("2.76"),
            currency="CNY",
            as_of="2026-07-30",
            source="test",
        )
        values = fetch_item_metrics(
            item,
            snapshot,
            today=datetime(2026, 7, 30),
        )
        self.assertEqual(values["premium_pct"], "3.99")
        self.assertTrue(values["subscription_open"])
        self.assertEqual(values["_meta"]["as_of"], "2026-07-28")
        self.assertEqual(values["_meta"]["context"], "净值 2.3848")

    @patch("tools.portfolio_monitor.request_text")
    def test_stale_auto_metric_is_not_exposed(self, request_text):
        request_text.return_value = """
        <p>最新交易日：2026-07-20</p>
        <h2>PE · 市盈率正常</h2><strong>12.3</strong>
        <span>近 10 年百分位30.0%</span>
        <h2>PB · 市净率正常</h2>
        """
        item = {
            "metric_source": {
                "type": "index_valuation",
                "name": "test source",
                "url": "https://example.test/index",
                "max_age_days": 5,
            }
        }
        snapshot = Snapshot(
            price=Decimal("1"),
            high_52w=Decimal("1"),
            currency="CNY",
            as_of="2026-07-30",
            source="test",
        )
        values = fetch_item_metrics(
            item,
            snapshot,
            today=datetime(2026, 7, 30),
        )
        self.assertNotIn("pe", values)
        self.assertIn("超过 5 天", values["_meta"]["issue"])

    def test_manual_metrics_override_auto_source_issue(self):
        item = {
            "symbol": "EX",
            "metric_gate_groups": [
                {
                    "mode": "all",
                    "checks": [
                        {"metric": "pe", "operator": "<=", "value": 12}
                    ],
                }
            ],
        }
        merged = merge_metrics(
            {"EX": {"_meta": {"source": "auto", "issue": "failed"}}},
            {"EX": {"pe": 11}},
            [item],
            override_source="/tmp/metrics.json",
        )
        self.assertEqual(merged["EX"]["pe"], 11)
        self.assertNotIn("issue", merged["EX"]["_meta"])
        self.assertEqual(
            merged["EX"]["_meta"]["override"],
            "/tmp/metrics.json",
        )

    @patch("tools.portfolio_monitor.request_text")
    def test_auto_metrics_use_fresh_cache_after_source_failure(self, request_text):
        request_text.side_effect = RuntimeError("source down")
        item = {
            "name": "Example ETF",
            "symbol": "EX",
            "metric_source": {
                "type": "index_valuation",
                "name": "test source",
                "url": "https://example.test/index",
                "max_age_days": 5,
            },
        }
        snapshot = Snapshot(
            price=Decimal("1"),
            high_52w=Decimal("1"),
            currency="CNY",
            as_of="2026-07-30",
            source="test",
        )
        cached = {
            "EX": {
                "pe": "12.3",
                "valuation_percentile": "30",
                "_meta": {
                    "source": "test source",
                    "as_of": "2026-07-29",
                },
            }
        }
        values, warnings = fetch_auto_metrics(
            [item],
            {"EX": snapshot},
            cached_metrics=cached,
            today=datetime(2026, 7, 30),
        )
        self.assertEqual(values["EX"]["pe"], "12.3")
        self.assertTrue(values["EX"]["_meta"]["cached"])
        self.assertIn("使用 2026-07-29 缓存", warnings[0])


if __name__ == "__main__":
    unittest.main()
