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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
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


@dataclass
class Quote:
    symbol: str
    price: Decimal | None
    currency: str
    as_of: str
    source: str
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
        row = dict(raw)
        row["名称"] = raw.get("名称") or raw.get("name") or ""
        row["股数"] = raw.get("股数") or raw.get("number") or raw.get("shares") or ""
        row["总价"] = (
            raw.get("总价")
            or raw.get("total")
            or raw.get("amount_cny")
            or ""
        )
        row["代码"] = raw.get("代码") or raw.get("code") or ""
        raw_market = raw.get("市场") or raw.get("market") or ""
        row["市场"] = MARKET_NAMES.get(raw_market.strip(), raw_market.strip())
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


def request_text(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


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


def fetch_quote(row: dict[str, str], alias: dict[str, Any]) -> Quote:
    symbol = alias.get("symbol") or ""
    market = alias.get("market") or row.get("市场") or ""
    currency = infer_currency(market)
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
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    fx = {k: Decimal(str(v)) for k, v in aliases.get("fx_to_cny", {}).items()}
    enriched = []
    summary = {
        "total_original": Decimal("0"),
        "total_latest_cny": Decimal("0"),
        "missing_total_rows": [],
        "missing_symbol_rows": [],
        "priced_rows": 0,
    }

    for row in rows:
        item = dict(row)
        alias = alias_for(row, aliases)
        name = normalize_name(row.get("名称", ""))
        shares = dec(row.get("股数")) or Decimal("0")
        original_total = dec(row.get("总价"))
        market = alias.get("market") or row.get("市场") or ""
        category = alias.get("category") or row.get("类别") or ""
        symbol = alias.get("symbol") or ""
        tags = alias.get("tags", [])

        item["代码"] = symbol
        item["类别"] = row.get("类别") or category
        item["市场"] = row.get("市场") or market
        item["标签"] = "、".join(tags)
        item["候选代码"] = "、".join(alias.get("candidate_symbols", []))

        if original_total is not None:
            summary["total_original"] += original_total
        elif shares > 0:
            summary["missing_total_rows"].append(name)

        quote = Quote(symbol, None, infer_currency(item["市场"]), "", "not_requested")
        if fetch_prices:
            quote = fetch_quote(row, alias)
            if quote.price is not None:
                summary["priced_rows"] += 1

        item["最新价"] = money(quote.price, "0.0001")
        item["币种"] = quote.currency or infer_currency(item["市场"])
        fx_rate = fx.get(item["币种"], Decimal("1"))
        item["汇率(CNY)"] = money(fx_rate, "0.0001")
        latest_cny = None
        if quote.price is not None and shares > 0:
            latest_cny = quote.price * shares * fx_rate
            summary["total_latest_cny"] += latest_cny
        item["最新市值(CNY)"] = money(latest_cny)
        item["原总价(CNY估计)"] = money(original_total)
        pnl = None
        pnl_pct = None
        if latest_cny is not None and original_total and original_total != 0:
            pnl = latest_cny - original_total
            pnl_pct = pnl / original_total * Decimal("100")
        item["浮动盈亏(CNY)"] = money(pnl)
        item["浮动盈亏率"] = pct(pnl_pct)
        item["更新时间"] = quote.as_of
        item["数据源"] = quote.source
        note_parts = []
        if quote.note:
            note_parts.append(quote.note)
        if not symbol:
            summary["missing_symbol_rows"].append(name)
            note_parts.append("需补代码后才能自动更新价格")
        if row.get("类别") and row.get("类别") != category:
            note_parts.append(f"保留原类别：{row.get('类别')}")
        item["备注"] = "；".join(note_parts)
        enriched.append(item)
    return enriched, summary


def write_csv(path: str, rows: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def group_totals(rows: list[dict[str, str]], column: str) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        amount = dec(row.get("最新市值(CNY)")) or dec(row.get("原总价(CNY估计)"))
        if amount is None:
            continue
        key = row.get(column) or "未分类"
        totals[key] += amount
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def render_report(rows: list[dict[str, str]], summary: dict[str, Any], fetch_prices: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_known = summary["total_latest_cny"] if fetch_prices and summary["total_latest_cny"] else summary["total_original"]
    market_totals = group_totals(rows, "市场")
    category_totals = group_totals(rows, "类别")
    lines = [
        "# 持仓分析快照",
        "",
        f"**生成时间**：{now}",
        f"**价格模式**：{'已尝试联网更新' if fetch_prices else '仅使用表内总价，不联网'}",
        f"**组合已识别金额**：{money(total_known)} CNY",
        "",
        "> 本报告仅用于学习和持仓管理，不构成投资建议。",
        "",
        "## 先说结论",
        "",
        "- 当前统计只能覆盖有 `总价` 或已成功取价的持仓；没有总价、也没有成功取价的持仓不会进入权重分母。",
        "- 单一最大暴露是茅台/消费；其次是腾讯/互联网、医疗医药、伯克希尔、红利资产。",
        "- 表格需要先统一三个口径：`总价` 到底是成本还是最新市值；跨市场金额是否都已折成人民币；ETF/基金需要唯一代码。",
        "- 标签应拆成 `市场`、`资产类别`、`行业`、`策略标签`、`风险标签`，否则后续很难做自动归因。",
        "",
        "## 组合结构",
        "",
        "| 维度 | 金额(CNY) | 占比 |",
        "|------|-----------|------|",
    ]
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
        "| 名称 | 股数 | 原总价(CNY估计) | 代码 | 市场 | 类别 | 最新市值(CNY) | 浮动盈亏率 | 标签 | 备注 |",
        "|------|------|----------------|------|------|------|---------------|------------|------|------|",
    ])
    for row in rows:
        lines.append(
            "| {名称} | {股数} | {原总价} | {代码} | {市场} | {类别} | {最新市值} | {盈亏率} | {标签} | {备注} |".format(
                名称=row.get("名称", ""),
                股数=row.get("股数", ""),
                原总价=row.get("原总价(CNY估计)", ""),
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
            "- 以下资产有股数但缺少 `总价`，在离线模式下没有进入组合权重：" + "、".join(missing_totals),
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
        "- 表内 `总价` 被视为人民币成本或上次市值快照；若实际是本币金额，请先统一口径。",
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
    enriched, summary = enrich_rows(rows, aliases, fetch_prices=(command == "update"))
    output = args.input if args.in_place else args.output
    write_csv(output, enriched)
    if args.report:
        write_report(args.report, render_report(enriched, summary, fetch_prices=(command == "update")))

    print(f"已读取: {args.input}")
    print(f"已输出: {output}")
    print(f"表内总价合计: {money(summary['total_original'])} CNY")
    if command == "update":
        print(f"成功获取价格: {summary['priced_rows']} / {len(rows)}")
        print(f"最新市值合计(仅已取价): {money(summary['total_latest_cny'])} CNY")
    if summary["missing_symbol_rows"]:
        print("待补代码: " + "、".join(summary["missing_symbol_rows"]))
    if args.report:
        print(f"分析报告: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
