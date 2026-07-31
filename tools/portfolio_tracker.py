#!/usr/bin/env python3
"""Portfolio CSV tag enrichment, analysis, and price updates.

零外部依赖，默认不覆盖原始持仓表。

Examples:
  python3 tools/portfolio_tracker.py analyze \
    --input "$HOME/Documents/投资表_数据表_表格.csv"

  python3 tools/portfolio_tracker.py update \
    --input "$HOME/Documents/投资表_数据表_表格.csv" \
    --output data/portfolio/holdings_latest.csv \
    --report reports/持仓分析-$(date +%Y%m%d).md

Privacy note:
  The update command sends configured symbols to public quote endpoints.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ALIASES = os.path.join(ROOT, "data", "portfolio_aliases.json")
DEFAULT_OUTPUT = os.path.join(ROOT, "data", "portfolio", "holdings_latest.csv")
MARKET_NAMES = {
    "CN": "A股",
    "HK": "港股",
    "US": "美股",
    "A股": "A股",
    "港股": "港股",
    "美股": "美股",
}
CASH_CURRENCIES = {
    "人民币": "CNY",
    "港币": "HKD",
    "美金": "USD",
    "美元": "USD",
}
FX_TOLERANCE_PCT = Decimal("0.5")
FX_MAX_REFERENCE_AGE_DAYS = 4
OUTPUT_FIELDS = [
    "名称",
    "代码",
    "市场",
    "类别",
    "股数",
    "平均成本",
    "成本币种",
    "成本金额(本币)",
    "成本基础(CNY估计)",
    "最新价",
    "币种",
    "汇率(CNY)",
    "最新市值(CNY)",
    "浮动盈亏(CNY)",
    "浮动盈亏率",
    "更新时间",
    "标签",
    "备注",
]


@dataclass
class Quote:
    symbol: str
    price: Decimal | None
    currency: str
    as_of: str
    source: str
    note: str = ""


@dataclass
class FxRate:
    currency: str
    cny_rate: Decimal | None
    as_of: str
    source: str
    status: str
    reference_rate: Decimal | None = None
    reference_as_of: str = ""
    note: str = ""


def dec(value: Any) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def money(value: Decimal | None, places: str = "0.01") -> str:
    if value is None:
        return ""
    return str(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def pct(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def load_aliases(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_holdings(path: str) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        raw_rows = list(csv.DictReader(f))

    rows = []
    for raw in raw_rows:
        normalized = {
            str(key or "").strip(): value
            for key, value in raw.items()
        }
        row = dict(normalized)
        row["名称"] = (
            normalized.get("名称")
            or normalized.get("name")
            or normalized.get("标的")
            or ""
        )
        row["股数"] = (
            normalized.get("股数")
            or normalized.get("number")
            or normalized.get("shares")
            or normalized.get("数量")
            or ""
        )
        row["总价"] = (
            normalized.get("总价")
            or normalized.get("total")
            or normalized.get("amount_cny")
            or normalized.get("成本基础")
            or ""
        )
        row["平均成本"] = (
            normalized.get("平均成本")
            or normalized.get("average_cost")
            or normalized.get("cost_price")
            or normalized.get("成本价")
            or ""
        )
        row["代码"] = normalized.get("代码") or normalized.get("code") or ""
        raw_market = normalized.get("市场") or normalized.get("market") or ""
        row["市场"] = MARKET_NAMES.get(raw_market.strip(), raw_market.strip())
        row["币种"] = (
            normalized.get("币种")
            or normalized.get("currency")
            or ""
        )
        rows.append(row)
    return rows


def normalize_name(name: str) -> str:
    return (name or "").strip()


def alias_for(row: dict[str, str], aliases: dict[str, Any]) -> dict[str, Any]:
    name = normalize_name(row.get("名称", ""))
    return aliases.get("aliases", {}).get(name, {})


def infer_currency(market: str) -> str:
    if market == "港股":
        return "HKD"
    if market == "美股":
        return "USD"
    return "CNY"


def infer_market(code: str, name: str = "") -> str:
    code = code.strip().upper()
    if name in CASH_CURRENCIES:
        return "现金"
    if re.fullmatch(r"\d{6}", code):
        return "A股"
    if re.fullmatch(r"\d{4,5}", code):
        return "港股"
    if re.fullmatch(r"[A-Z][A-Z0-9.-]*", code):
        return "美股"
    return ""


def ashare_exchange(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("0", "1", "2", "3")):
        return "SZ"
    return ""


def infer_symbol_and_quote(
    code: str,
    market: str,
    name: str,
) -> tuple[str, str]:
    code = code.strip().upper()
    if not code or name in CASH_CURRENCIES:
        return "", ""
    if market == "A股" and re.fullmatch(r"\d{6}", code):
        if "基金" in name and "ETF" not in name.upper():
            return code, ""
        exchange = ashare_exchange(code)
        if exchange:
            return f"{code}.{exchange}", f"{exchange.lower()}{code}"
    if market == "港股" and code.isdigit():
        numeric_code = str(int(code)).zfill(4)
        return f"{numeric_code}.HK", f"hk{code.zfill(5)}"
    if market == "美股":
        yahoo_symbol = code.replace(".", "-")
        return yahoo_symbol, f"us{code}"
    return code, ""


def resolve_asset(
    row: dict[str, str],
    alias: dict[str, Any],
) -> dict[str, Any]:
    item = dict(alias)
    name = normalize_name(row.get("名称", ""))
    raw_code = (row.get("代码") or "").strip()
    cash_currency = CASH_CURRENCIES.get(name)
    raw_market = row.get("市场") or ""
    market = item.get("market") or raw_market or infer_market(raw_code, name)
    market = MARKET_NAMES.get(market, market)
    inferred_symbol, inferred_quote = infer_symbol_and_quote(
        raw_code,
        market,
        name,
    )
    item["market"] = market
    item["symbol"] = item.get("symbol") or inferred_symbol
    item["quote"] = item.get("quote") or inferred_quote
    item["currency"] = (
        item.get("currency")
        or row.get("币种")
        or cash_currency
        or infer_currency(market)
    )
    item["is_cash"] = bool(cash_currency)
    if item["is_cash"]:
        item["category"] = item.get("category") or "现金"
        item["tags"] = item.get("tags") or ["现金", item["currency"]]
    return item


def request_text(url: str, timeout: int = 12, attempts: int = 3) -> str:
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"
                )
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("gbk", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable_statuses or attempt == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
        time.sleep(0.5 * (2**attempt))
    raise RuntimeError("request retry loop ended unexpectedly")


def fetch_tencent_quote(quote_code: str, symbol: str, currency: str) -> Quote:
    url = f"https://qt.gtimg.cn/q={urllib.parse.quote(quote_code)}"
    raw = request_text(url)
    start = raw.find('"')
    end = raw.rfind('"')
    if start < 0 or end <= start:
        return Quote(symbol, None, currency, "", "tencent", "empty quote")
    fields = raw[start + 1 : end].split("~")
    price = None
    # A股常见字段3；港股腾讯接口常见字段3或6，择第一个正数。
    for idx in (3, 6, 4):
        if idx < len(fields):
            candidate = dec(fields[idx])
            if candidate and candidate > 0:
                price = candidate
                break
    as_of = fields[30] if len(fields) > 30 else ""
    return Quote(symbol, price, currency, as_of, "tencent")


def fetch_yahoo_chart(symbol: str) -> Quote:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d"
    raw = request_text(url)
    data = json.loads(raw)
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return Quote(symbol, None, "", "", "yahoo", "empty chart")
    meta = result.get("meta", {})
    price = dec(meta.get("regularMarketPrice") or meta.get("previousClose"))
    currency = meta.get("currency") or ""
    ts = meta.get("regularMarketTime")
    as_of = ""
    if ts:
        as_of = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    return Quote(symbol, price, currency, as_of, "yahoo")


def parse_ecb_reference_rates(raw: str) -> tuple[dict[str, Decimal], str]:
    root = ET.fromstring(raw)
    reference_date = ""
    rates: dict[str, Decimal] = {}
    for element in root.iter():
        attrs = element.attrib
        if attrs.get("time"):
            reference_date = attrs["time"]
        currency = attrs.get("currency")
        rate = attrs.get("rate")
        if currency and rate:
            rates[currency] = Decimal(rate)
    required = {"USD", "HKD", "CNY"}
    if not reference_date or not required.issubset(rates):
        missing = ", ".join(sorted(required - rates.keys()))
        raise RuntimeError(
            "ECB reference response missing date or currencies"
            + (f": {missing}" if missing else "")
        )
    return rates, reference_date


def ecb_cny_cross_rates(
    raw: str,
) -> tuple[dict[str, Decimal], str]:
    rates, as_of = parse_ecb_reference_rates(raw)
    cny_per_eur = rates["CNY"]
    return {
        "CNY": Decimal("1"),
        "USD": cny_per_eur / rates["USD"],
        "HKD": cny_per_eur / rates["HKD"],
    }, as_of


def fx_difference_pct(primary: Decimal, reference: Decimal) -> Decimal:
    if reference == 0:
        raise ValueError("reference FX rate is zero")
    return abs(primary - reference) / reference * Decimal("100")


def fetch_live_fx_rates(
    currencies: set[str],
    today: date | None = None,
) -> tuple[dict[str, FxRate], list[str]]:
    current_date = today or datetime.now().astimezone().date()
    results = {
        "CNY": FxRate(
            currency="CNY",
            cny_rate=Decimal("1"),
            as_of=current_date.isoformat(),
            source="人民币本币",
            status="VERIFIED",
            reference_rate=Decimal("1"),
            reference_as_of=current_date.isoformat(),
        )
    }
    warnings: list[str] = []
    foreign = sorted((currencies - {"CNY"}) & {"USD", "HKD"})
    if not foreign:
        return results, warnings

    try:
        ecb_raw = request_text(
            "https://www.ecb.europa.eu/stats/eurofxref/"
            "eurofxref-daily.xml"
        )
        reference_rates, reference_as_of = ecb_cny_cross_rates(ecb_raw)
        reference_age = (
            current_date - date.fromisoformat(reference_as_of)
        ).days
        if reference_age < 0 or reference_age > FX_MAX_REFERENCE_AGE_DAYS:
            raise RuntimeError(
                f"ECB reference rate is stale: {reference_as_of}"
            )
    except Exception as exc:
        for currency in foreign:
            results[currency] = FxRate(
                currency=currency,
                cny_rate=None,
                as_of="",
                source="Yahoo Finance + ECB",
                status="MISSING",
                note=f"ECB reference unavailable: {exc}",
            )
        warnings.append(f"官方汇率复核不可用：{exc}")
        return results, warnings

    for currency in foreign:
        reference = reference_rates[currency]
        try:
            market_quote = fetch_yahoo_chart(f"{currency}CNY=X")
            if market_quote.price is None:
                raise RuntimeError("Yahoo FX quote has no price")
            if market_quote.currency and market_quote.currency != "CNY":
                raise RuntimeError(
                    "Yahoo FX quote currency mismatch: "
                    f"{market_quote.currency}"
                )
            if not market_quote.as_of:
                raise RuntimeError("Yahoo FX quote has no timestamp")
            market_date = datetime.strptime(
                market_quote.as_of[:10],
                "%Y-%m-%d",
            ).date()
            market_age = (current_date - market_date).days
            if market_age < 0 or market_age > 3:
                raise RuntimeError(
                    f"Yahoo FX quote is stale: {market_quote.as_of}"
                )
            difference = fx_difference_pct(
                market_quote.price,
                reference,
            )
            if difference > FX_TOLERANCE_PCT:
                results[currency] = FxRate(
                    currency=currency,
                    cny_rate=None,
                    as_of=market_quote.as_of,
                    source="Yahoo Finance + ECB",
                    status="CONFLICT",
                    reference_rate=reference,
                    reference_as_of=reference_as_of,
                    note=(
                        f"Yahoo={market_quote.price}, ECB={reference}; "
                        f"差异={difference.quantize(Decimal('0.01'))}%"
                    ),
                )
                warnings.append(
                    f"{currency}/CNY 双源差异 "
                    f"{difference.quantize(Decimal('0.01'))}% "
                    f"超过 {FX_TOLERANCE_PCT}%"
                )
                continue
            results[currency] = FxRate(
                currency=currency,
                cny_rate=market_quote.price,
                as_of=market_quote.as_of,
                source="Yahoo Finance（ECB每日参考汇率复核）",
                status="VERIFIED",
                reference_rate=reference,
                reference_as_of=reference_as_of,
                note=(
                    f"ECB={reference.quantize(Decimal('0.0001'))}; "
                    f"差异={difference.quantize(Decimal('0.01'))}%"
                ),
            )
        except Exception as exc:
            results[currency] = FxRate(
                currency=currency,
                cny_rate=None,
                as_of="",
                source="Yahoo Finance + ECB",
                status="MISSING",
                reference_rate=reference,
                reference_as_of=reference_as_of,
                note=str(exc),
            )
            warnings.append(f"{currency}/CNY 实时汇率不可用：{exc}")
    return results, warnings


def fetch_quote(row: dict[str, str], alias: dict[str, Any]) -> Quote:
    symbol = alias.get("symbol") or ""
    market = alias.get("market") or row.get("市场") or ""
    currency = alias.get("currency") or infer_currency(market)
    if not symbol:
        candidates = ", ".join(alias.get("candidate_symbols", []))
        note = f"missing symbol; candidates: {candidates}" if candidates else "missing symbol"
        return Quote("", None, currency, "", "manual", note)
    if alias.get("quote"):
        try:
            quote = fetch_tencent_quote(alias["quote"], symbol, currency)
            if quote.price is not None:
                return quote
        except Exception as exc:
            tencent_error = str(exc)
        else:
            tencent_error = quote.note or "no price from tencent"
        try:
            fallback = fetch_yahoo_chart(symbol)
            if fallback.price is not None:
                fallback.note = f"tencent fallback: {tencent_error}"
                return fallback
        except Exception as exc:
            return Quote(symbol, None, currency, "", "tencent/yahoo", f"{tencent_error}; {exc}")
        return Quote(symbol, None, currency, "", "tencent/yahoo", tencent_error)
    try:
        return fetch_yahoo_chart(symbol)
    except Exception as exc:
        return Quote(symbol, None, currency, "", "yahoo", str(exc))


def enrich_rows(
    rows: list[dict[str, str]],
    aliases: dict[str, Any],
    fetch_prices: bool,
    current_fx_rates: dict[str, FxRate] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    cost_fx = {
        k: Decimal(str(v))
        for k, v in aliases.get("fx_to_cny", {}).items()
    }
    if current_fx_rates is None:
        current_fx_rates = {
            currency: FxRate(
                currency=currency,
                cny_rate=rate,
                as_of="配置文件",
                source="portfolio_aliases.json",
                status="CONFIGURED",
            )
            for currency, rate in cost_fx.items()
        }
    enriched = []
    summary = {
        "total_original": Decimal("0"),
        "total_latest_cny": Decimal("0"),
        "missing_total_rows": [],
        "missing_symbol_rows": [],
        "priced_rows": 0,
        "valued_rows": 0,
        "derived_cost_rows": 0,
        "cash_rows": 0,
        "fx_rates": current_fx_rates,
        "comparable_cost_cny": Decimal("0"),
        "comparable_value_cny": Decimal("0"),
    }

    for row in rows:
        item = dict(row)
        alias = resolve_asset(row, alias_for(row, aliases))
        name = normalize_name(row.get("名称", ""))
        shares = dec(row.get("股数")) or Decimal("0")
        average_cost = dec(row.get("平均成本"))
        original_total = dec(row.get("总价"))
        market = alias.get("market") or row.get("市场") or ""
        category = alias.get("category") or row.get("类别") or ""
        symbol = alias.get("symbol") or ""
        tags = alias.get("tags", [])
        currency = alias.get("currency") or infer_currency(market)
        cost_fx_rate = cost_fx.get(currency)
        is_cash = bool(alias.get("is_cash"))
        native_cost = None
        cost_basis = "缺失"

        if (
            original_total is None
            and is_cash
            and shares > 0
            and cost_fx_rate
        ):
            native_cost = shares
            original_total = shares * cost_fx_rate
            cost_basis = "现金余额×配置汇率"
            summary["cash_rows"] += 1
        elif (
            original_total is None
            and average_cost is not None
            and shares > 0
            and cost_fx_rate
        ):
            native_cost = average_cost * shares
            original_total = native_cost * cost_fx_rate
            cost_basis = "平均成本×数量×配置汇率"
            summary["derived_cost_rows"] += 1
        elif original_total is not None:
            cost_basis = "表内总价(CNY)"

        item["代码"] = symbol
        item["类别"] = row.get("类别") or category
        item["市场"] = row.get("市场") or market
        item["标签"] = "、".join(tags)
        item["候选代码"] = "、".join(alias.get("candidate_symbols", []))
        item["成本币种"] = currency
        item["成本金额(本币)"] = money(native_cost)
        item["成本汇率(CNY)"] = money(cost_fx_rate, "0.0001")
        item["成本基础口径"] = cost_basis

        if original_total is not None:
            summary["total_original"] += original_total
        elif shares > 0 and not is_cash:
            summary["missing_total_rows"].append(name)

        quote = Quote(symbol, None, infer_currency(item["市场"]), "", "not_requested")
        if is_cash:
            quote = Quote(symbol, Decimal("1"), currency, "", "cash_balance")
        elif fetch_prices:
            quote = fetch_quote(row, alias)
            if quote.price is not None:
                summary["priced_rows"] += 1

        item["最新价"] = money(quote.price, "0.0001")
        item["币种"] = quote.currency or currency
        fx_quote = current_fx_rates.get(item["币种"])
        quote_fx_rate = fx_quote.cny_rate if fx_quote else None
        item["汇率(CNY)"] = money(quote_fx_rate, "0.0001")
        item["汇率来源"] = fx_quote.source if fx_quote else ""
        item["汇率时间"] = fx_quote.as_of if fx_quote else ""
        item["汇率核验"] = fx_quote.status if fx_quote else "MISSING"
        latest_cny = None
        if quote.price is not None and shares > 0 and quote_fx_rate:
            latest_cny = quote.price * shares * quote_fx_rate
            summary["total_latest_cny"] += latest_cny
            summary["valued_rows"] += 1
        item["最新市值(CNY)"] = money(latest_cny)
        item["成本基础(CNY估计)"] = money(original_total)
        item["原总价(CNY估计)"] = money(original_total)
        pnl = None
        pnl_pct = None
        if latest_cny is not None and original_total and original_total != 0:
            pnl = latest_cny - original_total
            pnl_pct = pnl / original_total * Decimal("100")
            summary["comparable_cost_cny"] += original_total
            summary["comparable_value_cny"] += latest_cny
        item["浮动盈亏(CNY)"] = money(pnl)
        item["浮动盈亏率"] = pct(pnl_pct)
        item["更新时间"] = quote.as_of
        item["数据源"] = quote.source
        note_parts = []
        if quote.note:
            note_parts.append(quote.note)
        if not symbol and not is_cash:
            summary["missing_symbol_rows"].append(name)
            note_parts.append("需补代码后才能自动更新价格")
        if original_total is None and shares > 0:
            note_parts.append("缺总价和平均成本，无法计算成本基础")
        if cost_fx_rate is None:
            note_parts.append(f"缺少{currency}兑人民币配置汇率")
        if fetch_prices and (not fx_quote or fx_quote.cny_rate is None):
            note_parts.append(
                f"{item['币种']}当前汇率未通过核验，"
                "不计算人民币当前市值"
            )
        elif fetch_prices and fx_quote.status != "VERIFIED":
            note_parts.append(
                f"使用{fx_quote.status}汇率计算当前市值"
            )
        if row.get("类别") and row.get("类别") != category:
            note_parts.append(f"保留原类别：{row.get('类别')}")
        item["备注"] = "；".join(note_parts)
        enriched.append(item)
    comparable_cost = summary["comparable_cost_cny"]
    comparable_value = summary["comparable_value_cny"]
    summary["total_pnl_cny"] = comparable_value - comparable_cost
    summary["total_pnl_pct"] = (
        summary["total_pnl_cny"] / comparable_cost * Decimal("100")
        if comparable_cost
        else None
    )
    summary["pnl_coverage_pct"] = (
        comparable_cost / summary["total_original"] * Decimal("100")
        if summary["total_original"]
        else None
    )
    enriched.sort(key=lambda item: item.get("市场") == "现金")
    return enriched, summary


def write_csv(path: str, rows: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=OUTPUT_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def group_totals(
    rows: list[dict[str, str]],
    column: str,
    current_only: bool = False,
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        if current_only:
            amount = dec(row.get("最新市值(CNY)"))
        else:
            amount = (
                dec(row.get("成本基础(CNY估计)"))
                or dec(row.get("原总价(CNY估计)"))
            )
        if amount is None:
            continue
        key = row.get(column) or "未分类"
        totals[key] += amount
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def render_report(rows: list[dict[str, str]], summary: dict[str, Any], fetch_prices: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_known = summary["total_latest_cny"] if fetch_prices and summary["total_latest_cny"] else summary["total_original"]
    market_totals = group_totals(rows, "市场", current_only=fetch_prices)
    category_totals = group_totals(
        rows,
        "类别",
        current_only=fetch_prices,
    )
    pnl_lines = []
    if fetch_prices and summary.get("total_pnl_pct") is not None:
        pnl_lines = [
            f"**可比口径成本**：{money(summary['comparable_cost_cny'])} CNY",
            f"**可比口径当前市值**：{money(summary['comparable_value_cny'])} CNY",
            f"**总浮动盈亏**：{money(summary['total_pnl_cny'])} CNY",
            f"**总盈亏率**：{pct(summary['total_pnl_pct'])}",
            f"**盈亏成本覆盖率**：{pct(summary['pnl_coverage_pct'])}",
        ]
    lines = [
        "# 持仓分析快照",
        "",
        f"**生成时间**：{now}",
        f"**价格模式**：{'已尝试联网更新价格与双源核验汇率' if fetch_prices else '仅使用表内成本与配置汇率，不联网'}",
        f"**组合已识别金额**：{money(total_known)} CNY",
        *pnl_lines,
        "",
        "> 本报告仅用于学习和持仓管理，不构成投资建议。",
        "",
        "## 汇率口径",
        "",
    ]
    for currency, rate in summary.get("fx_rates", {}).items():
        reference = (
            money(rate.reference_rate, "0.0001")
            if rate.reference_rate is not None
            else "-"
        )
        lines.append(
            f"- {currency}/CNY：{money(rate.cny_rate, '0.0001') or '不可用'}；"
            f"状态={rate.status}；来源={rate.source}；时间={rate.as_of or '-'}；"
            f"ECB复核={reference} ({rate.reference_as_of or '-'})"
        )
    lines.extend([
        "",
        "## 先说结论",
        "",
        "- 当前统计覆盖有 `总价`、可由 `平均成本×数量` 推导成本基础、现金余额或已成功取价的持仓。",
        "- 单一最大暴露是茅台/消费；其次是腾讯/互联网、医疗医药、伯克希尔、红利资产。",
        "- 表格需要先统一三个口径：`总价` 到底是成本还是最新市值；跨市场金额是否都已折成人民币；ETF/基金需要唯一代码。",
        "- 标签应拆成 `市场`、`资产类别`、`行业`、`策略标签`、`风险标签`，否则后续很难做自动归因。",
        "",
        "## 组合结构",
        "",
        "| 维度 | 金额(CNY) | 占比 |",
        "|------|-----------|------|",
    ])
    for key, amount in market_totals.items():
        weight = amount / total_known * Decimal("100") if total_known else None
        lines.append(f"| 市场：{key} | {money(amount)} | {pct(weight)} |")
    for key, amount in category_totals.items():
        weight = amount / total_known * Decimal("100") if total_known else None
        lines.append(f"| 类别：{key} | {money(amount)} | {pct(weight)} |")

    lines.extend([
        "",
        "## 持仓明细",
        "",
        "| 名称 | 股数 | 平均成本 | 成本基础(CNY估计) | 代码 | 市场 | 类别 | 最新市值(CNY) | 浮动盈亏率 | 标签 | 备注 |",
        "|------|------|----------|-------------------|------|------|------|---------------|------------|------|------|",
    ])
    for row in rows:
        lines.append(
            "| {名称} | {股数} | {平均成本} | {成本基础} | {代码} | {市场} | {类别} | {最新市值} | {盈亏率} | {标签} | {备注} |".format(
                名称=row.get("名称", ""),
                股数=row.get("股数", ""),
                平均成本=row.get("平均成本", ""),
                成本基础=row.get("成本基础(CNY估计)", ""),
                代码=row.get("代码", ""),
                市场=row.get("市场", ""),
                类别=row.get("类别", ""),
                最新市值=row.get("最新市值(CNY)", ""),
                盈亏率=row.get("浮动盈亏率", ""),
                标签=row.get("标签", ""),
                备注=row.get("备注", ""),
            )
        )

    missing_symbols = summary.get("missing_symbol_rows", [])
    missing_totals = summary.get("missing_total_rows", [])
    if missing_totals:
        lines.extend([
            "",
            "## 未计入权重的持仓",
            "",
            "- 以下资产有数量但同时缺少 `总价` 和 `平均成本`，在离线模式下没有进入组合权重：" + "、".join(missing_totals),
            "- 这些资产包含美股/港股成长股和主题ETF，补价后组合风险画像可能明显变化。",
        ])
    if missing_symbols:
        lines.extend([
            "",
            "## 待补信息",
            "",
            "- 以下资产缺少唯一代码，暂不能自动更新价格：" + "、".join(missing_symbols),
            "- 对ETF/基金，建议在别名表里补交易所代码或基金代码，避免把同名产品认错。",
        ])
    lines.extend([
        "",
        "## 风险提示",
        "",
        "- 表内 `总价` 仍被视为人民币金额；新版 `平均成本` 按标的本币计算，并使用别名配置中的汇率折算。",
        "- 美股、港股、A股混合持仓必须同时看汇率风险。",
        "- 标签是研究辅助，不是买卖建议。",
    ])
    return "\n".join(lines) + "\n"


def write_report(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich and update portfolio CSV.")
    parser.add_argument("--input", required=True, help="Input CSV path.")
    parser.add_argument("--aliases", default=DEFAULT_ALIASES, help="Alias/tag config JSON.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output enriched CSV path.")
    parser.add_argument("--report", default="", help="Optional Markdown report path.")
    parser.add_argument("--in-place", action="store_true", help="Overwrite input CSV with enriched columns.")
    parser.add_argument(
        "--use-config-fx",
        action="store_true",
        help="Use configured FX rates even in update mode; disables live FX.",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("analyze", help="Analyze and enrich without fetching prices.")
    subparsers.add_parser("update", help="Fetch prices, enrich CSV, and optionally write report.")

    # Support command before options and after options by normalizing simple usage.
    argv = sys.argv[1:]
    if argv and argv[0] in {"analyze", "update"}:
        argv = argv[1:] + [argv[0]]
    args = parser.parse_args(argv)
    command = args.command or "analyze"

    aliases = load_aliases(args.aliases)
    rows = read_holdings(args.input)
    fetch_prices = command == "update"
    fx_warnings: list[str] = []
    current_fx_rates = None
    if fetch_prices and not args.use_config_fx:
        currencies = {
            resolve_asset(row, alias_for(row, aliases)).get("currency", "CNY")
            for row in rows
        }
        current_fx_rates, fx_warnings = fetch_live_fx_rates(currencies)
    enriched, summary = enrich_rows(
        rows,
        aliases,
        fetch_prices=fetch_prices,
        current_fx_rates=current_fx_rates,
    )
    summary["fx_warnings"] = fx_warnings
    output = args.input if args.in_place else args.output
    write_csv(output, enriched)
    if args.report:
        write_report(
            args.report,
            render_report(enriched, summary, fetch_prices=fetch_prices),
        )

    print(f"已读取: {args.input}")
    print(f"已输出: {output}")
    print(f"成本基础合计(CNY估计): {money(summary['total_original'])} CNY")
    if summary["derived_cost_rows"]:
        print(
            "由平均成本推导: "
            f"{summary['derived_cost_rows']} 项"
        )
    if summary["cash_rows"]:
        print(f"已识别现金余额: {summary['cash_rows']} 项")
    if command == "update":
        print(f"成功获取证券价格: {summary['priced_rows']} 项")
        print(
            "成功计算当前人民币市值: "
            f"{summary['valued_rows']} / {len(rows)} 项"
        )
        print(
            "当前市值合计(仅核验通过且已取价): "
            f"{money(summary['total_latest_cny'])} CNY"
        )
        if summary["total_pnl_pct"] is not None:
            print(
                "可比口径成本: "
                f"{money(summary['comparable_cost_cny'])} CNY"
            )
            print(
                "可比口径当前市值: "
                f"{money(summary['comparable_value_cny'])} CNY"
            )
            print(
                "总浮动盈亏: "
                f"{money(summary['total_pnl_cny'])} CNY"
            )
            print(f"总盈亏率: {pct(summary['total_pnl_pct'])}")
            print(
                "盈亏成本覆盖率: "
                f"{pct(summary['pnl_coverage_pct'])}"
            )
        print("当前汇率：")
        for currency, rate in summary["fx_rates"].items():
            reference = (
                money(rate.reference_rate, "0.0001")
                if rate.reference_rate is not None
                else "-"
            )
            print(
                f"- {currency}/CNY: "
                f"{money(rate.cny_rate, '0.0001') or '不可用'} "
                f"[{rate.status}] | {rate.source} | {rate.as_of or '-'} "
                f"| ECB={reference} ({rate.reference_as_of or '-'})"
            )
        for warning in fx_warnings:
            print(f"汇率警告: {warning}", file=sys.stderr)
    if summary["missing_total_rows"]:
        print(
            "缺少总价/平均成本: "
            + "、".join(summary["missing_total_rows"])
        )
    if summary["missing_symbol_rows"]:
        print("待补代码: " + "、".join(summary["missing_symbol_rows"]))
    if args.report:
        print(f"分析报告: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
