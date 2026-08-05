import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path

from tools.portfolio_book import (
    BUY,
    DEPOSIT,
    DIVIDEND,
    FEE,
    FX,
    INTEREST,
    OPEN_CASH,
    OPEN_POSITION,
    POSITION_TRANSFER,
    SELL,
    TRANSFER,
    WITHDRAW,
    apply_events,
    assert_cash_balances,
    empty_event,
    initialize_from_holdings,
    read_events,
    print_dashboard,
    resolve_event_fx,
    update_nav_history,
    value_book,
    write_compat_holdings,
)
from tools.portfolio_tracker import FxRate


class PortfolioBookTest(unittest.TestCase):
    def event(self, kind, **values):
        defaults = {
            "datetime": "2026-08-05",
            "type": kind,
            "account": "US",
            "currency": "USD",
            "fx_to_cny": "7",
        }
        defaults.update(values)
        return empty_event(**defaults)

    def test_average_cost_sale_updates_cash_and_realized_pnl(self):
        events = [
            self.event(OPEN_CASH, cash_amount="1000"),
            self.event(
                OPEN_POSITION,
                name="TEST",
                code="TEST",
                market="美股",
                quantity="10",
                price="50",
            ),
            self.event(
                SELL,
                name="TEST",
                code="TEST",
                market="美股",
                quantity="4",
                price="60",
            ),
        ]

        state = apply_events(events)
        position = next(iter(state.positions.values()))

        self.assertEqual(position.quantity, Decimal("6"))
        self.assertEqual(position.cost_native, Decimal("300"))
        self.assertEqual(position.cost_cny, Decimal("2100"))
        self.assertEqual(state.cash[("US", "USD")], Decimal("1240"))
        self.assertEqual(state.realized_pnl_cny, Decimal("280"))
        self.assertEqual(state.external_capital_cny, Decimal("10500"))

    def test_buy_fee_is_part_of_average_cost_and_requires_cash(self):
        events = [
            self.event(OPEN_CASH, cash_amount="1000"),
            self.event(
                BUY,
                name="TEST",
                code="TEST",
                market="美股",
                quantity="5",
                price="100",
                fee="1",
            ),
        ]

        state = apply_events(events)
        position = next(iter(state.positions.values()))

        self.assertEqual(state.cash[("US", "USD")], Decimal("499"))
        self.assertEqual(position.average_cost, Decimal("100.2"))
        self.assertEqual(state.fees_cny, Decimal("7"))
        assert_cash_balances(state, allow_negative=False)

    def test_cash_events_separate_external_flows_from_income_and_fees(self):
        events = [
            self.event(OPEN_CASH, cash_amount="100"),
            self.event(DEPOSIT, cash_amount="20"),
            self.event(WITHDRAW, cash_amount="5"),
            self.event(DIVIDEND, cash_amount="3"),
            self.event(INTEREST, cash_amount="2"),
            self.event(FEE, cash_amount="1"),
        ]

        state = apply_events(events)

        self.assertEqual(state.cash[("US", "USD")], Decimal("119"))
        self.assertEqual(state.external_capital_cny, Decimal("805"))
        self.assertEqual(state.income_cny, Decimal("35"))
        self.assertEqual(state.fees_cny, Decimal("7"))

    def test_transfer_and_fx_preserve_external_capital(self):
        events = [
            self.event(OPEN_CASH, account="CN", currency="CNY", fx_to_cny="1", cash_amount="1000"),
            self.event(
                TRANSFER,
                account="CN",
                counter_account="CN2",
                currency="CNY",
                fx_to_cny="1",
                cash_amount="400",
            ),
            self.event(
                FX,
                account="CN2",
                currency="CNY",
                counter_currency="USD",
                cash_amount="350",
                counter_amount="50",
                fx_to_cny="1",
                counter_fx_to_cny="7",
            ),
        ]

        state = apply_events(events)

        self.assertEqual(state.cash[("CN", "CNY")], Decimal("600"))
        self.assertEqual(state.cash[("CN2", "CNY")], Decimal("50"))
        self.assertEqual(state.cash[("CN2", "USD")], Decimal("50"))
        self.assertEqual(state.external_capital_cny, Decimal("1000"))

    def test_position_transfer_preserves_quantity_and_cost_basis(self):
        events = [
            self.event(OPEN_CASH, account="TIGER", cash_amount="100"),
            self.event(
                OPEN_POSITION,
                account="TIGER",
                name="TEST",
                code="TEST",
                market="美股",
                quantity="10",
                price="50",
            ),
            self.event(
                POSITION_TRANSFER,
                account="TIGER",
                counter_account="IBKR",
                name="TEST",
                code="TEST",
                market="美股",
                quantity="6",
                fee="1",
            ),
        ]

        state = apply_events(events)
        positions = {position.account: position for position in state.positions.values()}

        self.assertEqual(positions["TIGER"].quantity, Decimal("4"))
        self.assertEqual(positions["TIGER"].cost_native, Decimal("200"))
        self.assertEqual(positions["IBKR"].quantity, Decimal("6"))
        self.assertEqual(positions["IBKR"].cost_native, Decimal("300"))
        self.assertEqual(state.cash[("TIGER", "USD")], Decimal("99"))
        self.assertEqual(state.realized_pnl_cny, Decimal("0"))

    def test_manual_valuation_calculates_nav_pnl_and_cash_pool(self):
        events = [
            self.event(OPEN_CASH, cash_amount="1000"),
            self.event(
                OPEN_POSITION,
                name="QQQ",
                code="QQQ",
                market="美股",
                quantity="2",
                price="100",
            ),
        ]
        state = apply_events(events)
        aliases = {
            "fx_to_cny": {"USD": 7},
            "aliases": {
                "QQQ": {
                    "symbol": "QQQ",
                    "market": "美股",
                    "currency": "USD",
                    "category": "指数ETF",
                }
            },
        }
        fx_rates = {
            "USD": FxRate("USD", Decimal("7"), "2026-08-05", "test", "VERIFIED")
        }
        prices = {"QQQ": {"price": "120", "currency": "USD", "as_of": "test"}}

        rows, summary = value_book(
            state,
            aliases,
            fx_rates,
            manual_prices=prices,
            fetch_prices=False,
        )

        self.assertTrue(summary["complete"])
        self.assertEqual(summary["total_nav_cny"], Decimal("8680"))
        self.assertEqual(summary["external_capital_cny"], Decimal("8400"))
        self.assertEqual(summary["total_pnl_cny"], Decimal("280"))
        self.assertEqual(summary["unrealized_pnl_cny"], Decimal("280"))
        self.assertEqual(summary["cash_cny"], Decimal("7000"))
        self.assertEqual(summary["cash_like_by_currency"], {})
        self.assertEqual(rows[0]["浮动盈亏率"], "20.00%")

        output = StringIO()
        with redirect_stdout(output):
            print_dashboard(rows, summary)
        self.assertIn("成本价", output.getvalue())
        self.assertIn("100 USD", output.getvalue())

    def test_event_fx_defaults_cny_and_requires_rate_for_history(self):
        self.assertEqual(resolve_event_fx("CNY", "", "2026-01-01"), Decimal("1"))
        self.assertEqual(resolve_event_fx("USD", "7.2", "2026-01-01"), Decimal("7.2"))
        with self.assertRaisesRegex(ValueError, "历史交易必须提供"):
            resolve_event_fx("USD", "", "2026-01-01")

    def test_initialize_and_export_preserve_compact_schema_and_codes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.csv"
            ledger = Path(temp_dir) / "ledger.csv"
            output = Path(temp_dir) / "current.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["name", "number", "code", "market", "平均成本"])
                writer.writerow(["腾讯", "200", "00700", "HK", "436.274"])
                writer.writerow(["美金", "100", "", "", ""])
            aliases = {
                "fx_to_cny": {"HKD": 0.9, "USD": 7},
                "aliases": {
                    "腾讯": {
                        "symbol": "0700.HK",
                        "market": "港股",
                        "currency": "HKD",
                    },
                    "美金": {"market": "现金", "currency": "USD"},
                },
            }

            state = initialize_from_holdings(
                str(source),
                str(ledger),
                aliases,
                "2026-08-05",
            )
            write_compat_holdings(str(output), state)

            with output.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))

            self.assertEqual(rows[0]["code"], "00700")
            self.assertEqual(rows[0]["market"], "HK")
            self.assertEqual(rows[-1]["name"], "美金")
            self.assertEqual(len(read_events(str(ledger))), 2)

    def test_nav_history_replaces_same_day_and_adjusts_external_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "nav.csv")
            first = {
                "as_of": "2026-08-05T10:00:00+08:00",
                "total_nav_cny": Decimal("1000"),
                "external_capital_cny": Decimal("1000"),
                "total_pnl_cny": Decimal("0"),
                "total_pnl_pct": Decimal("0"),
                "cash_cny": Decimal("1000"),
                "cash_like_cny": Decimal("0"),
                "funds_pool_cny": Decimal("1000"),
                "price_coverage_pct": Decimal("100"),
            }
            update_nav_history(path, first)
            same_day = dict(first, total_nav_cny=Decimal("1010"), total_pnl_cny=Decimal("10"))
            update_nav_history(path, same_day)
            next_day = dict(
                first,
                as_of="2026-08-06T10:00:00+08:00",
                total_nav_cny=Decimal("1121"),
                external_capital_cny=Decimal("1100"),
                total_pnl_cny=Decimal("21"),
            )
            update_nav_history(path, next_day)

            with open(path, "r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))

            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["组合净资产(CNY)"], "1010.00")
            self.assertEqual(rows[1]["净值指数"], "1.010891")


if __name__ == "__main__":
    unittest.main()
