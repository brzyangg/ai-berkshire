#!/usr/bin/env python3
"""Ledger-backed portfolio accounting and valuation.

The append-only ledger is the source of truth. Current holdings, cash pools,
valuation snapshots, and the legacy compact holdings CSV are derived outputs.

Examples:
  python3 tools/portfolio_book.py init \
    --input "$HOME/Documents/投资表_数据表_表格.csv" \
    --compat-output "$HOME/Documents/投资表_数据表_表格.csv"
  python3 tools/portfolio_book.py trade --side buy --account US \
    --name QQQM --code QQQM --market US --currency USD \
    --quantity 5 --price 300
  python3 tools/portfolio_book.py cash --type deposit --account CN \
    --currency CNY --amount 10000
  python3 tools/portfolio_book.py configure \
    --compat-output "$HOME/Documents/投资表_数据表_表格.csv"
  python3 tools/portfolio_book.py update
  python3 tools/portfolio_book.py show
  python3 tools/portfolio_book.py history --limit 20

Privacy note:
  The default book directory is local/portfolio, which is ignored by Git.
  The update command sends open-position symbols to public quote endpoints.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.portfolio_tracker import (
    CASH_CURRENCIES,
    FxRate,
    alias_for,
    dec,
    fetch_live_fx_rates,
    fetch_quote,
    infer_currency,
    load_aliases,
    money,
    pct,
    read_holdings,
    resolve_asset,
)


DEFAULT_BOOK_DIR = os.path.join(ROOT, "local", "portfolio")
DEFAULT_ALIASES = os.path.join(ROOT, "data", "portfolio_aliases.json")

LEDGER_FIELDS = [
    "event_id",
    "datetime",
    "type",
    "account",
    "counter_account",
    "name",
    "code",
    "market",
    "currency",
    "counter_currency",
    "quantity",
    "price",
    "fee",
    "cash_amount",
    "counter_amount",
    "fx_to_cny",
    "counter_fx_to_cny",
    "reference_id",
    "note",
]

COMPAT_FIELDS = ["name", "number", "code", "market", "平均成本"]

VALUATION_FIELDS = [
    "名称",
    "代码",
    "账户",
    "市场",
    "类别",
    "数量",
    "平均成本",
    "成本币种",
    "剩余成本(CNY)",
    "最新价",
    "币种",
    "汇率(CNY)",
    "最新市值(CNY)",
    "浮动盈亏(CNY)",
    "浮动盈亏率",
    "权重",
    "更新时间",
    "状态",
    "备注",
]

NAV_HISTORY_FIELDS = [
    "日期",
    "组合净资产(CNY)",
    "累计净投入(CNY)",
    "总盈亏(CNY)",
    "总盈亏率",
    "净值指数",
    "现金(CNY)",
    "现金管理资产(CNY)",
    "资金池(CNY)",
    "价格覆盖率",
    "更新时间",
]

OPEN_POSITION = "OPEN_POSITION"
OPEN_CASH = "OPEN_CASH"
BUY = "BUY"
SELL = "SELL"
DEPOSIT = "DEPOSIT"
WITHDRAW = "WITHDRAW"
DIVIDEND = "DIVIDEND"
INTEREST = "INTEREST"
FEE = "FEE"
TRANSFER = "TRANSFER"
POSITION_TRANSFER = "POSITION_TRANSFER"
FX = "FX"
VOID = "VOID"

VALID_TYPES = {
    OPEN_POSITION,
    OPEN_CASH,
    BUY,
    SELL,
    DEPOSIT,
    WITHDRAW,
    DIVIDEND,
    INTEREST,
    FEE,
    TRANSFER,
    POSITION_TRANSFER,
    FX,
    VOID,
}

CURRENCY_CASH_NAMES = {
    "CNY": "人民币",
    "HKD": "港币",
    "USD": "美金",
}

COMPACT_MARKETS = {
    "A股": "CN",
    "港股": "HK",
    "美股": "US",
    "CN": "CN",
    "HK": "HK",
    "US": "US",
}

DEFAULT_ACCOUNTS = {"CNY": "CN", "HKD": "HK", "USD": "US"}
CENT = Decimal("0.01")
ZERO = Decimal("0")


@dataclass
class Position:
    account: str
    name: str
    code: str
    market: str
    currency: str
    quantity: Decimal = ZERO
    cost_native: Decimal = ZERO
    cost_cny: Decimal = ZERO

    @property
    def average_cost(self) -> Decimal | None:
        if not self.quantity:
            return None
        return self.cost_native / self.quantity


@dataclass
class BookState:
    positions: dict[str, Position] = field(default_factory=dict)
    cash: dict[tuple[str, str], Decimal] = field(
        default_factory=lambda: defaultdict(lambda: ZERO)
    )
    external_capital_cny: Decimal = ZERO
    realized_pnl_cny: Decimal = ZERO
    income_cny: Decimal = ZERO
    fees_cny: Decimal = ZERO
    activities: list[dict[str, str]] = field(default_factory=list)


def required_decimal(value: Any, label: str, positive: bool = False) -> Decimal:
    parsed = dec(value)
    if parsed is None:
        raise ValueError(f"{label}不能为空或不是有效数字")
    if positive and parsed <= 0:
        raise ValueError(f"{label}必须大于0")
    return parsed


def decimal_or_zero(value: Any) -> Decimal:
    return dec(value) or ZERO


def decimal_text(value: Decimal | None, places: str = "0.########") -> str:
    if value is None:
        return ""
    if "#" in places:
        return format(value.normalize(), "f")
    quantized = value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return format(quantized, "f").rstrip("0").rstrip(".") or "0"


def event_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def event_time(value: str | None = None) -> str:
    if not value:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("日期应为 YYYY-MM-DD 或 ISO 日期时间") from exc
    if "T" not in text and " " not in text:
        return parsed.date().isoformat()
    return parsed.isoformat(timespec="seconds")


def empty_event(**values: Any) -> dict[str, str]:
    row = {field_name: "" for field_name in LEDGER_FIELDS}
    for key, value in values.items():
        if key not in row:
            raise KeyError(f"unknown ledger field: {key}")
        row[key] = str(value) if value is not None else ""
    row["event_id"] = row["event_id"] or event_id()
    row["datetime"] = event_time(row["datetime"] or None)
    row["type"] = row["type"].upper()
    return row


def position_key(account: str, code: str, name: str, currency: str) -> str:
    identity = (code or name).strip().upper()
    return "|".join([account.strip(), identity, currency.strip().upper()])


def atomic_write_csv(
    path: str,
    fieldnames: list[str],
    rows: Iterable[dict[str, Any]],
) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


def atomic_write_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = f"{path}.tmp"

    def serialize(item: Any) -> Any:
        if isinstance(item, Decimal):
            return decimal_text(item)
        if isinstance(item, dict):
            return {key: serialize(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [serialize(value) for value in item]
        return item

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(serialize(value), file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temp_path, path)


def read_events(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = set(LEDGER_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError("账本缺少字段: " + ", ".join(sorted(missing)))
        rows = [
            {field_name: (row.get(field_name) or "").strip() for field_name in LEDGER_FIELDS}
            for row in reader
        ]
    ids = [row["event_id"] for row in rows]
    if any(not value for value in ids):
        raise ValueError("账本存在空 event_id")
    if len(ids) != len(set(ids)):
        raise ValueError("账本存在重复 event_id")
    invalid = sorted({row["type"] for row in rows} - VALID_TYPES)
    if invalid:
        raise ValueError("账本存在未知类型: " + ", ".join(invalid))
    return rows


def active_events(events: list[dict[str, str]]) -> list[dict[str, str]]:
    voided = {
        row["reference_id"]
        for row in events
        if row["type"] == VOID and row["reference_id"]
    }
    return [
        row
        for row in events
        if row["type"] != VOID and row["event_id"] not in voided
    ]


def apply_events(events: list[dict[str, str]]) -> BookState:
    state = BookState()
    for row in active_events(events):
        kind = row["type"]
        account = row["account"]
        currency = row["currency"].upper()
        fx_rate = required_decimal(row["fx_to_cny"] or "1", "人民币汇率", positive=True)
        fee = decimal_or_zero(row["fee"])
        quantity = decimal_or_zero(row["quantity"])
        price = dec(row["price"])
        amount = dec(row["cash_amount"])
        realized = ZERO
        activity_cash = ZERO

        if kind in {OPEN_POSITION, BUY, SELL, POSITION_TRANSFER}:
            if not all([account, row["name"], currency]):
                raise ValueError(f"{row['event_id']}: 证券事件缺少账户、名称或币种")
            key = position_key(account, row["code"], row["name"], currency)
            position = state.positions.get(key)

            if kind in {OPEN_POSITION, BUY}:
                if quantity <= 0 or price is None or price < 0:
                    raise ValueError(f"{row['event_id']}: 买入/期初数量和价格无效")
                if position is None:
                    position = Position(
                        account=account,
                        name=row["name"],
                        code=row["code"],
                        market=row["market"],
                        currency=currency,
                    )
                    state.positions[key] = position
                native_cost = quantity * price + fee
                cny_cost = native_cost * fx_rate
                position.quantity += quantity
                position.cost_native += native_cost
                position.cost_cny += cny_cost
                activity_cash = native_cost
                if kind == OPEN_POSITION:
                    state.external_capital_cny += cny_cost
                else:
                    state.cash[(account, currency)] -= native_cost
                    state.fees_cny += fee * fx_rate

            elif kind == SELL:
                if position is None or quantity <= 0 or quantity > position.quantity:
                    available = position.quantity if position else ZERO
                    raise ValueError(
                        f"{row['event_id']}: 卖出数量 {quantity} 超过持仓 {available}"
                    )
                if amount is not None:
                    if amount <= 0:
                        raise ValueError(f"{row['event_id']}: 卖出净回款必须大于0")
                    net_proceeds = amount
                    if price is None:
                        price = amount / quantity
                else:
                    if price is None or price <= 0:
                        raise ValueError(f"{row['event_id']}: 卖出需要价格或净回款")
                    net_proceeds = quantity * price - fee
                cost_native_sold = position.cost_native * quantity / position.quantity
                cost_cny_sold = position.cost_cny * quantity / position.quantity
                position.quantity -= quantity
                position.cost_native -= cost_native_sold
                position.cost_cny -= cost_cny_sold
                if abs(position.quantity) < Decimal("0.00000001"):
                    position.quantity = ZERO
                    position.cost_native = ZERO
                    position.cost_cny = ZERO
                state.cash[(account, currency)] += net_proceeds
                realized = net_proceeds * fx_rate - cost_cny_sold
                state.realized_pnl_cny += realized
                state.fees_cny += fee * fx_rate
                activity_cash = net_proceeds

            else:
                if position is None or quantity <= 0 or quantity > position.quantity:
                    available = position.quantity if position else ZERO
                    raise ValueError(
                        f"{row['event_id']}: 转仓数量 {quantity} 超过持仓 {available}"
                    )
                if not row["counter_account"]:
                    raise ValueError(f"{row['event_id']}: 转仓缺少目标账户")
                cost_native_moved = position.cost_native * quantity / position.quantity
                cost_cny_moved = position.cost_cny * quantity / position.quantity
                position.quantity -= quantity
                position.cost_native -= cost_native_moved
                position.cost_cny -= cost_cny_moved
                if abs(position.quantity) < Decimal("0.00000001"):
                    position.quantity = ZERO
                    position.cost_native = ZERO
                    position.cost_cny = ZERO
                target_key = position_key(
                    row["counter_account"],
                    row["code"],
                    row["name"],
                    currency,
                )
                target = state.positions.get(target_key)
                if target is None:
                    target = Position(
                        account=row["counter_account"],
                        name=row["name"],
                        code=row["code"],
                        market=row["market"],
                        currency=currency,
                    )
                    state.positions[target_key] = target
                target.quantity += quantity
                target.cost_native += cost_native_moved
                target.cost_cny += cost_cny_moved
                if fee:
                    state.cash[(account, currency)] -= fee
                    state.fees_cny += fee * fx_rate
                activity_cash = -fee

        elif kind == OPEN_CASH:
            amount = required_decimal(row["cash_amount"], "期初现金", positive=True)
            state.cash[(account, currency)] += amount
            state.external_capital_cny += amount * fx_rate
            activity_cash = amount

        elif kind in {DEPOSIT, WITHDRAW, DIVIDEND, INTEREST, FEE}:
            amount = required_decimal(row["cash_amount"], "现金金额", positive=True)
            sign = Decimal("1")
            if kind in {WITHDRAW, FEE}:
                sign = Decimal("-1")
            state.cash[(account, currency)] += sign * amount
            activity_cash = sign * amount
            if kind == DEPOSIT:
                state.external_capital_cny += amount * fx_rate
            elif kind == WITHDRAW:
                state.external_capital_cny -= amount * fx_rate
            elif kind in {DIVIDEND, INTEREST}:
                state.income_cny += amount * fx_rate
            elif kind == FEE:
                state.fees_cny += amount * fx_rate

        elif kind == TRANSFER:
            amount = required_decimal(row["cash_amount"], "转账金额", positive=True)
            if not row["counter_account"]:
                raise ValueError(f"{row['event_id']}: 转账缺少目标账户")
            state.cash[(account, currency)] -= amount
            state.cash[(row["counter_account"], currency)] += amount - fee
            state.fees_cny += fee * fx_rate
            activity_cash = amount

        elif kind == FX:
            source_amount = required_decimal(row["cash_amount"], "换出金额", positive=True)
            target_amount = required_decimal(row["counter_amount"], "换入金额", positive=True)
            target_currency = row["counter_currency"].upper()
            target_account = row["counter_account"] or account
            if not target_currency:
                raise ValueError(f"{row['event_id']}: 换汇缺少目标币种")
            state.cash[(account, currency)] -= source_amount + fee
            state.cash[(target_account, target_currency)] += target_amount
            state.fees_cny += fee * fx_rate
            activity_cash = source_amount

        state.activities.append(
            {
                "event_id": row["event_id"],
                "datetime": row["datetime"],
                "type": kind,
                "account": account,
                "name": row["name"],
                "currency": currency,
                "quantity": decimal_text(quantity),
                "price": decimal_text(price, "0.000001"),
                "cash_amount": decimal_text(activity_cash),
                "realized_pnl_cny": money(realized),
                "note": row["note"],
            }
        )

    state.positions = {
        key: position
        for key, position in state.positions.items()
        if position.quantity > 0
    }
    return state


def assert_cash_balances(state: BookState, allow_negative: bool) -> None:
    if allow_negative:
        return
    negative = [
        f"{account}/{currency}={money(balance)}"
        for (account, currency), balance in state.cash.items()
        if balance < -CENT
    ]
    if negative:
        raise ValueError(
            "现金不足，先录入入金/转账，或明确使用 --allow-negative-cash: "
            + ", ".join(negative)
        )


def append_event(path: str, row: dict[str, str], allow_negative: bool = False) -> BookState:
    events = read_events(path)
    if row["event_id"] in {event["event_id"] for event in events}:
        raise ValueError(f"event_id 已存在: {row['event_id']}")
    candidate = events + [row]
    state = apply_events(candidate)
    assert_cash_balances(state, allow_negative)
    atomic_write_csv(path, LEDGER_FIELDS, candidate)
    return state


def paths(book_dir: str) -> dict[str, str]:
    return {
        "ledger": os.path.join(book_dir, "ledger.csv"),
        "settings": os.path.join(book_dir, "settings.json"),
        "holdings": os.path.join(book_dir, "current_holdings.csv"),
        "valuation": os.path.join(book_dir, "valuation_latest.csv"),
        "summary": os.path.join(book_dir, "summary_latest.json"),
        "nav_history": os.path.join(book_dir, "nav_history.csv"),
    }


def load_settings(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as file:
        value = json.load(file)
    return value if isinstance(value, dict) else {}


def write_compat_holdings(path: str, state: BookState) -> None:
    rows: list[dict[str, str]] = []
    positions = sorted(
        state.positions.values(),
        key=lambda item: (item.market, item.name, item.account),
    )
    for position in positions:
        rows.append(
            {
                "name": position.name,
                "number": decimal_text(position.quantity),
                "code": position.code,
                "market": COMPACT_MARKETS.get(position.market, position.market),
                "平均成本": decimal_text(position.average_cost),
            }
        )

    balances: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for (_, currency), balance in state.cash.items():
        balances[currency] += balance
    for currency in ("USD", "HKD", "CNY"):
        balance = balances.get(currency, ZERO)
        if abs(balance) < CENT:
            continue
        rows.append(
            {
                "name": CURRENCY_CASH_NAMES.get(currency, currency),
                "number": decimal_text(balance, "0.01"),
                "code": "",
                "market": "",
                "平均成本": "",
            }
        )
    atomic_write_csv(path, COMPAT_FIELDS, rows)


def refresh_compat_outputs(book_paths: dict[str, str], state: BookState) -> list[str]:
    outputs = [book_paths["holdings"]]
    write_compat_holdings(book_paths["holdings"], state)
    settings = load_settings(book_paths["settings"])
    external = settings.get("compat_holdings_output")
    if external:
        write_compat_holdings(os.path.expanduser(external), state)
        outputs.append(os.path.expanduser(external))
    return outputs


def initialize_from_holdings(
    input_path: str,
    ledger_path: str,
    aliases: dict[str, Any],
    as_of: str,
    force: bool = False,
) -> BookState:
    if os.path.exists(ledger_path) and not force:
        raise FileExistsError("账本已存在；如需重建，请显式使用 --force")
    rows = read_holdings(input_path)
    configured_fx = {
        currency: Decimal(str(rate))
        for currency, rate in aliases.get("fx_to_cny", {}).items()
    }
    events: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        asset = resolve_asset(row, alias_for(row, aliases))
        name = row.get("名称", "").strip()
        quantity = required_decimal(row.get("股数"), f"{name} 数量", positive=True)
        currency = str(asset.get("currency") or "CNY").upper()
        fx_rate = configured_fx.get(currency)
        if fx_rate is None:
            raise ValueError(f"{name}: 配置中缺少 {currency}/CNY 汇率")
        account = DEFAULT_ACCOUNTS.get(currency, currency)
        common = {
            "event_id": f"OPEN-{as_of.replace('-', '')}-{index:03d}",
            "datetime": as_of,
            "account": account,
            "name": name,
            "code": row.get("代码", "").strip(),
            "market": asset.get("market") or row.get("市场", ""),
            "currency": currency,
            "fx_to_cny": decimal_text(fx_rate),
            "note": f"从 {os.path.basename(input_path)} 导入期初余额",
        }
        if asset.get("is_cash") or name in CASH_CURRENCIES:
            events.append(empty_event(type=OPEN_CASH, cash_amount=quantity, **common))
            continue
        average_cost = dec(row.get("平均成本"))
        if average_cost is None:
            total_cny = dec(row.get("总价"))
            if total_cny is not None:
                average_cost = total_cny / quantity / fx_rate
        if average_cost is None:
            raise ValueError(f"{name}: 缺少平均成本，无法建立期初账本")
        events.append(
            empty_event(
                type=OPEN_POSITION,
                quantity=decimal_text(quantity),
                price=decimal_text(average_cost),
                **common,
            )
        )
    state = apply_events(events)
    atomic_write_csv(ledger_path, LEDGER_FIELDS, events)
    return state


def lookup_asset_defaults(
    name: str,
    code: str,
    market: str,
    currency: str,
    aliases: dict[str, Any],
) -> dict[str, str]:
    row = {"名称": name, "代码": code, "市场": market, "币种": currency}
    asset = resolve_asset(row, alias_for(row, aliases))
    resolved_currency = str(asset.get("currency") or currency or infer_currency(market)).upper()
    return {
        "code": code or str(asset.get("symbol") or ""),
        "market": market or str(asset.get("market") or ""),
        "currency": resolved_currency,
        "account": DEFAULT_ACCOUNTS.get(resolved_currency, resolved_currency),
    }


def load_manual_prices(path: str) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as file:
        raw = json.load(file)
    prices: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            price_value = value.get("price")
            currency = value.get("currency", "")
            as_of = value.get("as_of", "manual")
        else:
            price_value = value
            currency = ""
            as_of = "manual"
        price = required_decimal(price_value, f"{key} price", positive=True)
        prices[str(key).upper()] = {
            "price": decimal_text(price),
            "currency": str(currency).upper(),
            "as_of": str(as_of),
        }
    return prices


def configured_fx_rates(aliases: dict[str, Any]) -> dict[str, FxRate]:
    return {
        currency: FxRate(
            currency=currency,
            cny_rate=Decimal(str(rate)),
            as_of="portfolio_aliases.json",
            source="配置汇率",
            status="CONFIGURED",
        )
        for currency, rate in aliases.get("fx_to_cny", {}).items()
    }


def resolve_event_fx(
    currency: str,
    explicit_rate: str,
    event_datetime: str,
) -> Decimal:
    currency = currency.upper()
    if explicit_rate:
        return required_decimal(explicit_rate, "人民币汇率", positive=True)
    if currency == "CNY":
        return Decimal("1")
    trade_date = event_time(event_datetime or None)[:10]
    today = datetime.now().astimezone().date().isoformat()
    if trade_date != today:
        raise ValueError(
            f"{currency} 历史交易必须提供交易日汇率 --fx-to-cny"
        )
    rates, warnings = fetch_live_fx_rates({currency})
    rate = rates.get(currency)
    if warnings or rate is None or rate.cny_rate is None:
        detail = "；".join(warnings) or "汇率未通过核验"
        raise ValueError(f"无法自动取得 {currency}/CNY 汇率: {detail}")
    return rate.cny_rate


def value_book(
    state: BookState,
    aliases: dict[str, Any],
    fx_rates: dict[str, FxRate],
    manual_prices: dict[str, dict[str, str]] | None = None,
    fetch_prices: bool = True,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    manual_prices = manual_prices or {}
    rows: list[dict[str, str]] = []
    position_values: dict[str, Decimal] = {}
    known_nav = ZERO
    cash_cny = ZERO
    cash_like_cny = ZERO
    cash_like_by_currency: dict[str, Decimal] = defaultdict(lambda: ZERO)
    unrealized_cny = ZERO
    missing: list[str] = []

    for key, position in state.positions.items():
        source_row = {
            "名称": position.name,
            "代码": position.code,
            "市场": position.market,
            "币种": position.currency,
        }
        asset = resolve_asset(source_row, alias_for(source_row, aliases))
        symbol = str(asset.get("symbol") or position.code)
        candidates = [symbol.upper(), position.code.upper(), position.name.upper()]
        manual = next((manual_prices[value] for value in candidates if value in manual_prices), None)
        quote_price: Decimal | None = None
        quote_currency = position.currency
        quote_as_of = ""
        note = ""
        status = "MISSING"
        if manual:
            quote_price = dec(manual["price"])
            quote_currency = manual["currency"] or position.currency
            quote_as_of = manual["as_of"]
            status = "MANUAL"
        elif fetch_prices:
            quote = fetch_quote(source_row, asset)
            quote_price = quote.price
            quote_currency = quote.currency or position.currency
            quote_as_of = quote.as_of
            note = quote.note
            status = "LIVE" if quote.price is not None else "MISSING"
        fx = fx_rates.get(quote_currency)
        rate = fx.cny_rate if fx else None
        value_cny = None
        pnl_cny = None
        pnl_pct_value = None
        if quote_price is not None and rate is not None:
            value_cny = position.quantity * quote_price * rate
            pnl_cny = value_cny - position.cost_cny
            pnl_pct_value = pnl_cny / position.cost_cny * Decimal("100") if position.cost_cny else None
            known_nav += value_cny
            unrealized_cny += pnl_cny
            position_values[key] = value_cny
            if asset.get("category") == "现金管理":
                cash_like_cny += value_cny
                cash_like_by_currency[quote_currency] += value_cny
        else:
            reasons = []
            if quote_price is None:
                reasons.append("缺价格")
            if rate is None:
                reasons.append(f"缺{quote_currency}/CNY汇率")
            note = "；".join(filter(None, [note, *reasons]))
            missing.append(position.name)
        rows.append(
            {
                "名称": position.name,
                "代码": symbol,
                "账户": position.account,
                "市场": asset.get("market") or position.market,
                "类别": asset.get("category") or "未分类",
                "数量": decimal_text(position.quantity),
                "平均成本": decimal_text(position.average_cost),
                "成本币种": position.currency,
                "剩余成本(CNY)": money(position.cost_cny),
                "最新价": decimal_text(quote_price, "0.0001"),
                "币种": quote_currency,
                "汇率(CNY)": money(rate, "0.0001"),
                "最新市值(CNY)": money(value_cny),
                "浮动盈亏(CNY)": money(pnl_cny),
                "浮动盈亏率": pct(pnl_pct_value),
                "权重": "",
                "更新时间": quote_as_of,
                "状态": status,
                "备注": note,
            }
        )

    cash_by_currency: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"balance": ZERO, "value_cny": ZERO}
    )
    for (account, currency), balance in sorted(state.cash.items()):
        fx = fx_rates.get(currency)
        rate = fx.cny_rate if fx else None
        value_cny = balance * rate if rate is not None else None
        if value_cny is None:
            missing.append(f"{account}/{currency}现金")
        else:
            known_nav += value_cny
            cash_cny += value_cny
            cash_by_currency[currency]["balance"] += balance
            cash_by_currency[currency]["value_cny"] += value_cny
        rows.append(
            {
                "名称": CURRENCY_CASH_NAMES.get(currency, currency),
                "代码": "",
                "账户": account,
                "市场": "现金",
                "类别": "现金",
                "数量": decimal_text(balance, "0.01"),
                "平均成本": "",
                "成本币种": currency,
                "剩余成本(CNY)": "",
                "最新价": "1",
                "币种": currency,
                "汇率(CNY)": money(rate, "0.0001"),
                "最新市值(CNY)": money(value_cny),
                "浮动盈亏(CNY)": "",
                "浮动盈亏率": "",
                "权重": "",
                "更新时间": fx.as_of if fx else "",
                "状态": fx.status if fx else "MISSING",
                "备注": fx.note if fx else f"缺{currency}/CNY汇率",
            }
        )

    complete = not missing
    total_nav = known_nav if complete else None
    total_pnl = total_nav - state.external_capital_cny if total_nav is not None else None
    total_pnl_pct = (
        total_pnl / state.external_capital_cny * Decimal("100")
        if total_pnl is not None and state.external_capital_cny
        else None
    )
    residual = (
        total_pnl - state.realized_pnl_cny - unrealized_cny - state.income_cny
        if total_pnl is not None
        else None
    )
    if total_nav:
        for row in rows:
            value = dec(row.get("最新市值(CNY)"))
            row["权重"] = pct(value / total_nav * Decimal("100")) if value is not None else ""
    rows.sort(key=lambda row: (row["市场"] == "现金", -(dec(row["最新市值(CNY)"]) or ZERO)))

    summary = {
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "complete": complete,
        "missing": missing,
        "position_count": len(state.positions),
        "priced_positions": len(position_values),
        "price_coverage_pct": (
            Decimal(len(position_values)) / Decimal(len(state.positions)) * Decimal("100")
            if state.positions
            else Decimal("100")
        ),
        "known_nav_cny": known_nav,
        "total_nav_cny": total_nav,
        "external_capital_cny": state.external_capital_cny,
        "total_pnl_cny": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "realized_pnl_cny": state.realized_pnl_cny,
        "unrealized_pnl_cny": unrealized_cny,
        "income_cny": state.income_cny,
        "fees_cny": state.fees_cny,
        "fx_and_other_pnl_cny": residual,
        "cash_cny": cash_cny,
        "cash_like_cny": cash_like_cny,
        "funds_pool_cny": cash_cny + cash_like_cny,
        "invested_cny": known_nav - cash_cny - cash_like_cny,
        "cash_by_currency": cash_by_currency,
        "cash_like_by_currency": cash_like_by_currency,
        "fx_rates": {
            currency: {
                "rate": rate.cny_rate,
                "status": rate.status,
                "source": rate.source,
                "as_of": rate.as_of,
            }
            for currency, rate in fx_rates.items()
        },
    }
    return rows, summary


def write_valuation(path: str, rows: list[dict[str, str]]) -> None:
    atomic_write_csv(path, VALUATION_FIELDS, rows)


def read_nav_history(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def update_nav_history(path: str, summary: dict[str, Any]) -> None:
    nav = summary.get("total_nav_cny")
    if nav is None:
        return
    history = read_nav_history(path)
    today = str(summary["as_of"])[:10]
    prior_rows = [row for row in history if row.get("日期", "") < today]
    if prior_rows:
        previous = max(prior_rows, key=lambda row: row["日期"])
        previous_nav = required_decimal(previous["组合净资产(CNY)"], "上一期净资产", positive=True)
        previous_capital = required_decimal(previous["累计净投入(CNY)"], "上一期累计投入")
        previous_index = required_decimal(previous["净值指数"], "上一期净值指数", positive=True)
        external_flow = summary["external_capital_cny"] - previous_capital
        period_return = (nav - previous_nav - external_flow) / previous_nav
        nav_index = previous_index * (Decimal("1") + period_return)
    else:
        nav_index = Decimal("1")
    current = {
        "日期": today,
        "组合净资产(CNY)": money(nav),
        "累计净投入(CNY)": money(summary["external_capital_cny"]),
        "总盈亏(CNY)": money(summary["total_pnl_cny"]),
        "总盈亏率": pct(summary["total_pnl_pct"]),
        "净值指数": money(nav_index, "0.000001"),
        "现金(CNY)": money(summary["cash_cny"]),
        "现金管理资产(CNY)": money(summary["cash_like_cny"]),
        "资金池(CNY)": money(summary["funds_pool_cny"]),
        "价格覆盖率": pct(summary["price_coverage_pct"]),
        "更新时间": summary["as_of"],
    }
    retained = [row for row in history if row.get("日期") != today]
    retained.append(current)
    retained.sort(key=lambda row: row["日期"])
    atomic_write_csv(path, NAV_HISTORY_FIELDS, retained)


def display_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1
        for char in value
    )


def pad(value: Any, width: int, right: bool = False) -> str:
    text = str(value)
    spaces = " " * max(0, width - display_width(text))
    return spaces + text if right else text + spaces


def format_table(headers: list[str], rows: list[list[Any]], right_columns: set[int] | None = None) -> str:
    right_columns = right_columns or set()
    text_rows = [[str(value) for value in row] for row in rows]
    widths = [display_width(header) for header in headers]
    for row in text_rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], display_width(value))
    lines = [
        "  ".join(pad(header, widths[index], index in right_columns) for index, header in enumerate(headers)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(
            pad(value, widths[index], index in right_columns)
            for index, value in enumerate(row)
        )
        for row in text_rows
    )
    return "\n".join(lines)


def print_dashboard(rows: list[dict[str, str]], summary: dict[str, Any]) -> None:
    complete_label = "完整" if summary.get("complete") else "不完整"
    print(f"组合账本 | {summary.get('as_of', '-')} | 估值{complete_label}")
    metrics = [
        ["组合净资产", money(summary.get("total_nav_cny")) or "不可用"],
        ["累计净投入", money(summary.get("external_capital_cny"))],
        ["总盈亏", money(summary.get("total_pnl_cny")) or "不可用"],
        ["总盈亏率", pct(summary.get("total_pnl_pct")) or "不可用"],
        ["已实现盈亏", money(summary.get("realized_pnl_cny"))],
        ["未实现盈亏", money(summary.get("unrealized_pnl_cny"))],
        ["分红/利息", money(summary.get("income_cny"))],
        ["现金", money(summary.get("cash_cny"))],
        ["现金管理资产", money(summary.get("cash_like_cny"))],
        ["可调配资金池", money(summary.get("funds_pool_cny"))],
    ]
    print(format_table(["指标", "CNY/比例"], metrics, {1}))
    print()
    holdings = []
    for row in rows:
        if row.get("市场") == "现金":
            continue
        holdings.append(
            [
                row.get("名称", ""),
                row.get("代码", ""),
                row.get("数量", ""),
                " ".join(
                    filter(
                        None,
                        [
                            row.get("平均成本", ""),
                            row.get("成本币种", ""),
                        ],
                    )
                ),
                row.get("最新价", "") or "-",
                row.get("最新市值(CNY)", "") or "-",
                row.get("权重", "") or "-",
                row.get("浮动盈亏率", "") or "-",
            ]
        )
    print("持仓")
    print(
        format_table(
            ["名称", "代码", "数量", "成本价", "现价", "市值CNY", "权重", "浮盈亏"],
            holdings,
            {2, 3, 4, 5, 6, 7},
        )
    )
    print()
    cash_rows = []
    cash_by_currency = summary.get("cash_by_currency", {})
    cash_like_by_currency = summary.get("cash_like_by_currency", {})
    currencies = sorted(set(cash_by_currency) | set(cash_like_by_currency))
    for currency in currencies:
        values = cash_by_currency.get(
            currency,
            {"balance": ZERO, "value_cny": ZERO},
        )
        rate = summary.get("fx_rates", {}).get(currency, {}).get("rate")
        managed = cash_like_by_currency.get(currency, ZERO)
        cash_rows.append(
            [
                currency,
                decimal_text(values.get("balance"), "0.01"),
                money(rate, "0.0001"),
                money(values.get("value_cny")),
                money(managed),
                money(values.get("value_cny") + managed),
            ]
        )
    print("现金池")
    print(
        format_table(
            ["币种", "现金余额", "汇率", "现金CNY", "现金管理CNY", "资金池CNY"],
            cash_rows,
            {1, 2, 3, 4, 5},
        )
    )
    if summary.get("missing"):
        print("\n数据缺口: " + "、".join(summary["missing"]))


def load_latest(book_paths: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not os.path.exists(book_paths["summary"]) or not os.path.exists(book_paths["valuation"]):
        raise FileNotFoundError("还没有估值快照，请先运行 update")
    with open(book_paths["summary"], "r", encoding="utf-8") as file:
        raw_summary = json.load(file)

    decimal_keys = {
        "known_nav_cny",
        "total_nav_cny",
        "external_capital_cny",
        "total_pnl_cny",
        "total_pnl_pct",
        "realized_pnl_cny",
        "unrealized_pnl_cny",
        "income_cny",
        "fees_cny",
        "fx_and_other_pnl_cny",
        "cash_cny",
        "cash_like_cny",
        "funds_pool_cny",
        "invested_cny",
        "price_coverage_pct",
    }
    summary = dict(raw_summary)
    for key in decimal_keys:
        summary[key] = dec(raw_summary.get(key))
    for currency, values in summary.get("cash_by_currency", {}).items():
        values["balance"] = dec(values.get("balance")) or ZERO
        values["value_cny"] = dec(values.get("value_cny")) or ZERO
    for currency, value in summary.get("cash_like_by_currency", {}).items():
        summary["cash_like_by_currency"][currency] = dec(value) or ZERO
    for currency, values in summary.get("fx_rates", {}).items():
        values["rate"] = dec(values.get("rate"))
    with open(book_paths["valuation"], "r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    return rows, summary


def save_settings(path: str, compat_output: str) -> None:
    settings = load_settings(path)
    if compat_output:
        settings["compat_holdings_output"] = os.path.abspath(os.path.expanduser(compat_output))
    atomic_write_json(path, settings)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append-only portfolio ledger, cash pools, and NAV snapshots."
    )
    parser.add_argument("--book-dir", default=DEFAULT_BOOK_DIR, help="Private portfolio book directory.")
    parser.add_argument("--aliases", default=DEFAULT_ALIASES, help="Alias/tag configuration JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create an opening ledger from a holdings CSV.")
    init_parser.add_argument("--input", required=True)
    init_parser.add_argument("--as-of", default=datetime.now().date().isoformat())
    init_parser.add_argument("--compat-output", default="")
    init_parser.add_argument("--force", action="store_true")

    trade_parser = subparsers.add_parser("trade", help="Append a BUY or SELL event.")
    trade_parser.add_argument("--side", required=True, choices=["buy", "sell"])
    trade_parser.add_argument("--account", default="")
    trade_parser.add_argument("--name", required=True)
    trade_parser.add_argument("--code", default="")
    trade_parser.add_argument("--market", default="")
    trade_parser.add_argument("--currency", default="")
    trade_parser.add_argument("--quantity", required=True)
    trade_parser.add_argument("--price", default="")
    trade_parser.add_argument("--net-amount", default="", help="Net settlement amount; mainly for redemptions.")
    trade_parser.add_argument("--fee", default="0")
    trade_parser.add_argument(
        "--fx-to-cny",
        default="",
        help="Trade-date CNY rate; same-day CNY/USD/HKD trades can fetch it automatically.",
    )
    trade_parser.add_argument("--date", default="")
    trade_parser.add_argument("--note", default="")
    trade_parser.add_argument("--allow-negative-cash", action="store_true")

    cash_parser = subparsers.add_parser("cash", help="Append deposit, withdrawal, income, or fee.")
    cash_parser.add_argument("--type", required=True, choices=["deposit", "withdraw", "dividend", "interest", "fee"])
    cash_parser.add_argument("--account", required=True)
    cash_parser.add_argument("--currency", required=True)
    cash_parser.add_argument("--amount", required=True)
    cash_parser.add_argument(
        "--fx-to-cny",
        default="",
        help="Event-date CNY rate; same-day events can fetch it automatically.",
    )
    cash_parser.add_argument("--name", default="")
    cash_parser.add_argument("--date", default="")
    cash_parser.add_argument("--note", default="")
    cash_parser.add_argument("--allow-negative-cash", action="store_true")

    transfer_parser = subparsers.add_parser("transfer", help="Move cash between accounts in one currency.")
    transfer_parser.add_argument("--from-account", required=True)
    transfer_parser.add_argument("--to-account", required=True)
    transfer_parser.add_argument("--currency", required=True)
    transfer_parser.add_argument("--amount", required=True)
    transfer_parser.add_argument("--fee", default="0")
    transfer_parser.add_argument("--fx-to-cny", required=True)
    transfer_parser.add_argument("--date", default="")
    transfer_parser.add_argument("--note", default="")
    transfer_parser.add_argument("--allow-negative-cash", action="store_true")

    position_transfer_parser = subparsers.add_parser(
        "position-transfer",
        help="Move a security between accounts without realizing a sale.",
    )
    position_transfer_parser.add_argument("--from-account", required=True)
    position_transfer_parser.add_argument("--to-account", required=True)
    position_transfer_parser.add_argument("--name", required=True)
    position_transfer_parser.add_argument("--code", default="")
    position_transfer_parser.add_argument("--market", default="")
    position_transfer_parser.add_argument("--currency", default="")
    position_transfer_parser.add_argument("--quantity", required=True)
    position_transfer_parser.add_argument("--fee", default="0")
    position_transfer_parser.add_argument("--fx-to-cny", required=True)
    position_transfer_parser.add_argument("--date", default="")
    position_transfer_parser.add_argument("--note", default="")
    position_transfer_parser.add_argument("--allow-negative-cash", action="store_true")

    fx_parser = subparsers.add_parser("fx", help="Record a currency conversion.")
    fx_parser.add_argument("--account", required=True)
    fx_parser.add_argument("--to-account", default="")
    fx_parser.add_argument("--from-currency", required=True)
    fx_parser.add_argument("--to-currency", required=True)
    fx_parser.add_argument("--from-amount", required=True)
    fx_parser.add_argument("--to-amount", required=True)
    fx_parser.add_argument("--fee", default="0")
    fx_parser.add_argument("--fx-to-cny", required=True)
    fx_parser.add_argument("--counter-fx-to-cny", required=True)
    fx_parser.add_argument("--date", default="")
    fx_parser.add_argument("--note", default="")
    fx_parser.add_argument("--allow-negative-cash", action="store_true")

    void_parser = subparsers.add_parser("void", help="Void an event by appending a reversal marker.")
    void_parser.add_argument("--event-id", required=True)
    void_parser.add_argument("--date", default="")
    void_parser.add_argument("--note", required=True)

    update_parser = subparsers.add_parser("update", help="Fetch quotes/FX and save a NAV snapshot.")
    update_parser.add_argument("--prices-file", default="")
    update_parser.add_argument("--use-config-fx", action="store_true")

    subparsers.add_parser("show", help="Show the latest saved dashboard without network access.")

    history_parser = subparsers.add_parser("history", help="Show ledger activity history.")
    history_parser.add_argument("--limit", type=int, default=20)
    history_parser.add_argument("--trades-only", action="store_true")

    export_parser = subparsers.add_parser("export", help="Export current holdings in legacy compact CSV format.")
    export_parser.add_argument("--output", required=True)

    configure_parser = subparsers.add_parser("configure", help="Configure automatic legacy CSV output.")
    configure_parser.add_argument("--compat-output", required=True)

    args = parser.parse_args()
    book_paths = paths(os.path.abspath(os.path.expanduser(args.book_dir)))
    aliases = load_aliases(args.aliases)

    if args.command == "init":
        state = initialize_from_holdings(
            os.path.abspath(os.path.expanduser(args.input)),
            book_paths["ledger"],
            aliases,
            args.as_of,
            force=args.force,
        )
        if args.compat_output:
            save_settings(book_paths["settings"], args.compat_output)
        outputs = refresh_compat_outputs(book_paths, state)
        print(f"账本已建立: {book_paths['ledger']}")
        print(f"期初证券: {len(state.positions)} 项")
        print(f"期初净投入(CNY): {money(state.external_capital_cny)}")
        for output in outputs:
            print(f"持仓快照: {output}")
        return 0

    events = read_events(book_paths["ledger"])
    if not events:
        raise FileNotFoundError("账本不存在或为空，请先运行 init")

    if args.command == "trade":
        defaults = lookup_asset_defaults(args.name, args.code, args.market, args.currency, aliases)
        currency = (args.currency or defaults["currency"]).upper()
        account = args.account or defaults["account"]
        fx_rate = resolve_event_fx(currency, args.fx_to_cny, args.date)
        row = empty_event(
            datetime=args.date,
            type=BUY if args.side == "buy" else SELL,
            account=account,
            name=args.name,
            code=args.code or defaults["code"],
            market=args.market or defaults["market"],
            currency=currency,
            quantity=args.quantity,
            price=args.price,
            fee=args.fee,
            cash_amount=args.net_amount,
            fx_to_cny=decimal_text(fx_rate),
            note=args.note,
        )
        state = append_event(book_paths["ledger"], row, args.allow_negative_cash)
        outputs = refresh_compat_outputs(book_paths, state)
        activity = state.activities[-1]
        print(f"已记录 {row['type']}: {args.name} {args.quantity} @ {activity['price'] or '-'} {currency}")
        if row["type"] == SELL:
            print(f"净回款: {activity['cash_amount']} {currency}")
            print(f"已实现盈亏(CNY): {activity['realized_pnl_cny']}")
        print(f"event_id: {row['event_id']}")
        for output in outputs:
            print(f"持仓快照: {output}")
        return 0

    if args.command == "cash":
        type_map = {
            "deposit": DEPOSIT,
            "withdraw": WITHDRAW,
            "dividend": DIVIDEND,
            "interest": INTEREST,
            "fee": FEE,
        }
        currency = args.currency.upper()
        fx_rate = resolve_event_fx(currency, args.fx_to_cny, args.date)
        row = empty_event(
            datetime=args.date,
            type=type_map[args.type],
            account=args.account,
            name=args.name,
            currency=currency,
            cash_amount=args.amount,
            fx_to_cny=decimal_text(fx_rate),
            note=args.note,
        )
        state = append_event(book_paths["ledger"], row, args.allow_negative_cash)
        refresh_compat_outputs(book_paths, state)
        print(f"已记录 {row['type']}: {args.amount} {args.currency.upper()}")
        print(f"event_id: {row['event_id']}")
        return 0

    if args.command == "transfer":
        row = empty_event(
            datetime=args.date,
            type=TRANSFER,
            account=args.from_account,
            counter_account=args.to_account,
            currency=args.currency.upper(),
            cash_amount=args.amount,
            fee=args.fee,
            fx_to_cny=args.fx_to_cny,
            note=args.note,
        )
        state = append_event(book_paths["ledger"], row, args.allow_negative_cash)
        refresh_compat_outputs(book_paths, state)
        print(f"已记录账户转账: {args.from_account} -> {args.to_account} {args.amount} {args.currency.upper()}")
        print(f"event_id: {row['event_id']}")
        return 0

    if args.command == "position-transfer":
        defaults = lookup_asset_defaults(
            args.name,
            args.code,
            args.market,
            args.currency,
            aliases,
        )
        row = empty_event(
            datetime=args.date,
            type=POSITION_TRANSFER,
            account=args.from_account,
            counter_account=args.to_account,
            name=args.name,
            code=args.code or defaults["code"],
            market=args.market or defaults["market"],
            currency=(args.currency or defaults["currency"]).upper(),
            quantity=args.quantity,
            fee=args.fee,
            fx_to_cny=args.fx_to_cny,
            note=args.note,
        )
        state = append_event(
            book_paths["ledger"],
            row,
            args.allow_negative_cash,
        )
        refresh_compat_outputs(book_paths, state)
        print(
            f"已记录证券转仓: {args.name} {args.quantity} "
            f"{args.from_account} -> {args.to_account}"
        )
        print(f"event_id: {row['event_id']}")
        return 0

    if args.command == "fx":
        row = empty_event(
            datetime=args.date,
            type=FX,
            account=args.account,
            counter_account=args.to_account,
            currency=args.from_currency.upper(),
            counter_currency=args.to_currency.upper(),
            cash_amount=args.from_amount,
            counter_amount=args.to_amount,
            fee=args.fee,
            fx_to_cny=args.fx_to_cny,
            counter_fx_to_cny=args.counter_fx_to_cny,
            note=args.note,
        )
        state = append_event(book_paths["ledger"], row, args.allow_negative_cash)
        refresh_compat_outputs(book_paths, state)
        print(f"已记录换汇: {args.from_amount} {args.from_currency.upper()} -> {args.to_amount} {args.to_currency.upper()}")
        print(f"event_id: {row['event_id']}")
        return 0

    if args.command == "void":
        target = next((event for event in events if event["event_id"] == args.event_id), None)
        if target is None:
            raise ValueError(f"找不到 event_id: {args.event_id}")
        if target["type"] == VOID:
            raise ValueError("不能冲销 VOID 事件")
        row = empty_event(
            datetime=args.date,
            type=VOID,
            reference_id=args.event_id,
            fx_to_cny="1",
            note=args.note,
        )
        state = append_event(book_paths["ledger"], row)
        refresh_compat_outputs(book_paths, state)
        print(f"已冲销: {args.event_id}")
        print(f"void event_id: {row['event_id']}")
        return 0

    if args.command == "export":
        state = apply_events(events)
        output = os.path.abspath(os.path.expanduser(args.output))
        write_compat_holdings(output, state)
        print(f"已输出: {output}")
        return 0

    if args.command == "configure":
        save_settings(book_paths["settings"], args.compat_output)
        state = apply_events(events)
        outputs = refresh_compat_outputs(book_paths, state)
        print(f"已配置兼容持仓输出: {os.path.abspath(os.path.expanduser(args.compat_output))}")
        for output in outputs:
            print(f"持仓快照: {output}")
        return 0

    if args.command == "history":
        state = apply_events(events)
        activities = state.activities
        if args.trades_only:
            activities = [row for row in activities if row["type"] in {BUY, SELL}]
        activities = activities[-max(args.limit, 0):]
        table_rows = [
            [
                row["datetime"][:19],
                row["type"],
                row["name"] or "-",
                row["quantity"] or "-",
                row["price"] or "-",
                f"{row['cash_amount']} {row['currency']}" if row["cash_amount"] else "-",
                row["realized_pnl_cny"] or "-",
                row["event_id"],
            ]
            for row in activities
        ]
        print(format_table(["时间", "类型", "标的", "数量", "价格", "现金变动", "已实现CNY", "event_id"], table_rows, {3, 4, 5, 6}))
        return 0

    if args.command == "show":
        rows, summary = load_latest(book_paths)
        print_dashboard(rows, summary)
        return 0

    if args.command == "update":
        state = apply_events(events)
        manual_prices = load_manual_prices(args.prices_file)
        currencies = {currency for _, currency in state.cash}
        currencies.update(position.currency for position in state.positions.values())
        if args.use_config_fx:
            fx_rates = configured_fx_rates(aliases)
            fx_warnings: list[str] = []
        else:
            fx_rates, fx_warnings = fetch_live_fx_rates(currencies)
        rows, summary = value_book(
            state,
            aliases,
            fx_rates,
            manual_prices=manual_prices,
            fetch_prices=True,
        )
        write_valuation(book_paths["valuation"], rows)
        atomic_write_json(book_paths["summary"], summary)
        update_nav_history(book_paths["nav_history"], summary)
        refresh_compat_outputs(book_paths, state)
        print_dashboard(rows, summary)
        for warning in fx_warnings:
            print(f"汇率警告: {warning}", file=sys.stderr)
        print(f"\n估值明细: {book_paths['valuation']}")
        print(f"净值历史: {book_paths['nav_history']}")
        return 0

    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, InvalidOperation) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
