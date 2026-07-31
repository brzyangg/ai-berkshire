import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tools.portfolio_tracker import (
    FxRate,
    Quote,
    ecb_cny_cross_rates,
    enrich_rows,
    fetch_live_fx_rates,
    infer_symbol_and_quote,
    read_holdings,
    resolve_asset,
    write_csv,
)


class PortfolioTrackerTest(unittest.TestCase):
    def test_ecb_cross_rates_convert_euro_quotes_to_cny_pairs(self):
        raw = """
        <Envelope>
          <Cube>
            <Cube time="2026-07-29">
              <Cube currency="USD" rate="1.1380"/>
              <Cube currency="HKD" rate="8.9246"/>
              <Cube currency="CNY" rate="7.7000"/>
            </Cube>
          </Cube>
        </Envelope>
        """

        rates, as_of = ecb_cny_cross_rates(raw)

        self.assertEqual(as_of, "2026-07-29")
        self.assertEqual(rates["USD"], Decimal("7.7000") / Decimal("1.1380"))
        self.assertEqual(rates["HKD"], Decimal("7.7000") / Decimal("8.9246"))

    @patch("tools.portfolio_tracker.fetch_yahoo_chart")
    @patch("tools.portfolio_tracker.request_text")
    def test_live_fx_requires_yahoo_and_ecb_to_agree(
        self,
        request_text,
        fetch_yahoo_chart,
    ):
        request_text.return_value = """
        <Envelope><Cube><Cube time="2026-07-29">
          <Cube currency="USD" rate="1.1380"/>
          <Cube currency="HKD" rate="8.9246"/>
          <Cube currency="CNY" rate="7.7000"/>
        </Cube></Cube></Envelope>
        """
        quotes = {
            "USDCNY=X": Quote(
                "USDCNY=X",
                Decimal("6.7596"),
                "CNY",
                "2026-07-30 13:55:27",
                "yahoo",
            ),
            "HKDCNY=X": Quote(
                "HKDCNY=X",
                Decimal("0.8614"),
                "CNY",
                "2026-07-30 13:55:22",
                "yahoo",
            ),
        }
        fetch_yahoo_chart.side_effect = lambda symbol: quotes[symbol]

        rates, warnings = fetch_live_fx_rates(
            {"CNY", "USD", "HKD"},
            today=date(2026, 7, 30),
        )

        self.assertEqual(warnings, [])
        self.assertEqual(rates["USD"].status, "VERIFIED")
        self.assertEqual(rates["USD"].cny_rate, Decimal("6.7596"))
        self.assertEqual(rates["HKD"].status, "VERIFIED")
        self.assertEqual(rates["HKD"].cny_rate, Decimal("0.8614"))

    @patch("tools.portfolio_tracker.fetch_yahoo_chart")
    @patch("tools.portfolio_tracker.request_text")
    def test_live_fx_conflict_fails_closed(
        self,
        request_text,
        fetch_yahoo_chart,
    ):
        request_text.return_value = """
        <Envelope><Cube><Cube time="2026-07-29">
          <Cube currency="USD" rate="1.1380"/>
          <Cube currency="HKD" rate="8.9246"/>
          <Cube currency="CNY" rate="7.7000"/>
        </Cube></Cube></Envelope>
        """
        fetch_yahoo_chart.return_value = Quote(
            "USDCNY=X",
            Decimal("7.20"),
            "CNY",
            "2026-07-30 13:55:27",
            "yahoo",
        )

        rates, warnings = fetch_live_fx_rates(
            {"USD"},
            today=date(2026, 7, 30),
        )

        self.assertEqual(rates["USD"].status, "CONFLICT")
        self.assertIsNone(rates["USD"].cny_rate)
        self.assertTrue(warnings)

    def test_read_holdings_accepts_latest_average_cost_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "holdings.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["name", "number", "code", "market", "平均成本"])
                writer.writerow(["腾讯", "200", "00700", "HK", "436.274"])

            rows = read_holdings(str(path))

        self.assertEqual(rows[0]["名称"], "腾讯")
        self.assertEqual(rows[0]["股数"], "200")
        self.assertEqual(rows[0]["代码"], "00700")
        self.assertEqual(rows[0]["市场"], "港股")
        self.assertEqual(rows[0]["平均成本"], "436.274")
        self.assertEqual(rows[0]["总价"], "")

    def test_average_cost_and_cash_are_converted_to_cny_cost_basis(self):
        rows = [
            {
                "名称": "腾讯",
                "股数": "200",
                "代码": "00700",
                "市场": "港股",
                "平均成本": "436.274",
                "总价": "",
                "币种": "",
            },
            {
                "名称": "美金",
                "股数": "100",
                "代码": "",
                "市场": "",
                "平均成本": "",
                "总价": "",
                "币种": "",
            },
        ]
        aliases = {
            "fx_to_cny": {"CNY": 1, "HKD": 0.916, "USD": 7.15},
            "aliases": {
                "腾讯": {
                    "symbol": "0700.HK",
                    "market": "港股",
                    "category": "科技互联网",
                },
                "美金": {
                    "currency": "USD",
                    "market": "现金",
                    "category": "现金",
                },
            },
        }

        enriched, summary = enrich_rows(rows, aliases, fetch_prices=False)

        expected_tencent = (
            Decimal("200") * Decimal("436.274") * Decimal("0.916")
        )
        self.assertEqual(
            Decimal(enriched[0]["成本基础(CNY估计)"]),
            expected_tencent.quantize(Decimal("0.01")),
        )
        self.assertEqual(
            Decimal(enriched[1]["成本基础(CNY估计)"]),
            Decimal("715.00"),
        )
        self.assertEqual(enriched[1]["最新市值(CNY)"], "715.00")
        self.assertEqual(summary["derived_cost_rows"], 1)
        self.assertEqual(summary["cash_rows"], 1)
        self.assertEqual(summary["missing_symbol_rows"], [])

    @patch("tools.portfolio_tracker.fetch_quote")
    def test_update_uses_live_fx_for_current_value_not_cost_basis(
        self,
        fetch_quote,
    ):
        fetch_quote.return_value = Quote(
            "TEST",
            Decimal("110"),
            "USD",
            "2026-07-30 14:00:00",
            "test",
        )
        rows = [
            {
                "名称": "TEST",
                "股数": "1",
                "代码": "TEST",
                "市场": "美股",
                "平均成本": "100",
                "总价": "",
                "币种": "",
            }
        ]
        aliases = {
            "fx_to_cny": {"USD": 7.15},
            "aliases": {},
        }
        live_fx = {
            "USD": FxRate(
                "USD",
                Decimal("6.75"),
                "2026-07-30 14:00:00",
                "test",
                "VERIFIED",
            )
        }

        enriched, summary = enrich_rows(
            rows,
            aliases,
            fetch_prices=True,
            current_fx_rates=live_fx,
        )

        self.assertEqual(enriched[0]["成本基础(CNY估计)"], "715.00")
        self.assertEqual(enriched[0]["最新市值(CNY)"], "742.50")
        self.assertEqual(enriched[0]["浮动盈亏(CNY)"], "27.50")
        self.assertEqual(summary["comparable_cost_cny"], Decimal("715.00"))
        self.assertEqual(summary["comparable_value_cny"], Decimal("742.50"))
        self.assertEqual(summary["total_pnl_cny"], Decimal("27.50"))
        self.assertEqual(summary["pnl_coverage_pct"], Decimal("100"))

    def test_cash_rows_are_sorted_after_securities(self):
        rows = [
            {
                "名称": "人民币",
                "股数": "100",
                "代码": "",
                "市场": "",
                "平均成本": "",
                "总价": "",
                "币种": "",
            },
            {
                "名称": "SGOV",
                "股数": "1",
                "代码": "SGOV",
                "市场": "美股",
                "平均成本": "100",
                "总价": "",
                "币种": "",
            },
        ]
        aliases = {
            "fx_to_cny": {"CNY": 1, "USD": 7},
            "aliases": {},
        }

        enriched, _ = enrich_rows(rows, aliases, fetch_prices=False)

        self.assertEqual([row["名称"] for row in enriched], ["SGOV", "人民币"])

    def test_compact_csv_omits_internal_source_and_basis_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "output.csv"
            write_csv(
                str(path),
                [
                    {
                        "名称": "Example",
                        "代码": "EX",
                        "成本基础口径": "internal",
                        "汇率来源": "internal",
                        "数据源": "internal",
                    }
                ],
            )
            with path.open("r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)

        self.assertNotIn("成本基础口径", reader.fieldnames)
        self.assertNotIn("汇率来源", reader.fieldnames)
        self.assertNotIn("数据源", reader.fieldnames)
        self.assertEqual(rows[0]["名称"], "Example")

    def test_missing_alias_uses_code_to_infer_market_and_quote(self):
        aliases = {"fx_to_cny": {"CNY": 1, "USD": 7.15}, "aliases": {}}
        rows = [
            {
                "名称": "货币ETF",
                "股数": "100",
                "代码": "511950",
                "市场": "",
                "平均成本": "",
                "总价": "",
                "币种": "",
            },
            {
                "名称": "SGOV",
                "股数": "50",
                "代码": "SGOV",
                "市场": "",
                "平均成本": "",
                "总价": "",
                "币种": "",
            },
        ]

        enriched, summary = enrich_rows(rows, aliases, fetch_prices=False)

        self.assertEqual(enriched[0]["代码"], "511950.SH")
        self.assertEqual(enriched[0]["市场"], "A股")
        self.assertEqual(enriched[1]["代码"], "SGOV")
        self.assertEqual(enriched[1]["市场"], "美股")
        self.assertEqual(summary["missing_symbol_rows"], [])
        self.assertEqual(summary["missing_total_rows"], ["货币ETF", "SGOV"])

    def test_old_total_schema_remains_supported(self):
        rows = [
            {
                "名称": "旧持仓",
                "股数": "10",
                "代码": "600000",
                "市场": "A股",
                "平均成本": "",
                "总价": "1234.56",
                "币种": "",
            }
        ]
        aliases = {"fx_to_cny": {"CNY": 1}, "aliases": {}}

        enriched, summary = enrich_rows(rows, aliases, fetch_prices=False)

        self.assertEqual(enriched[0]["成本基础(CNY估计)"], "1234.56")
        self.assertEqual(summary["total_original"], Decimal("1234.56"))

    def test_hong_kong_code_normalization_preserves_leading_zero(self):
        symbol, quote = infer_symbol_and_quote("00700", "港股", "腾讯")
        self.assertEqual(symbol, "0700.HK")
        self.assertEqual(quote, "hk00700")

    def test_cash_resolution_does_not_create_fake_symbol(self):
        row = {
            "名称": "人民币",
            "代码": "",
            "市场": "",
            "币种": "",
        }
        asset = resolve_asset(row, {})
        self.assertTrue(asset["is_cash"])
        self.assertEqual(asset["currency"], "CNY")
        self.assertEqual(asset["symbol"], "")


if __name__ == "__main__":
    unittest.main()
