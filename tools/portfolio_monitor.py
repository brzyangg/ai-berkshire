#!/usr/bin/env python3
"""Monitor investment price ladders and trailing drawdowns.

The monitor uses public Tencent quotes, with Yahoo and the last good snapshot
as fallbacks. It stores the last active band so a scheduled run only reports a
new, deeper trigger. It never places orders.

Examples:
  python3 tools/portfolio_monitor.py check --show-all
  python3 tools/portfolio_monitor.py check --repeat-active
  python3 tools/portfolio_monitor.py check --prices-file /tmp/prices.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "data", "portfolio", "watchlist.json")
DEFAULT_STATE = os.path.join(ROOT, "data", "portfolio", "monitor_state.json")


@dataclass
class Snapshot:
    price: Decimal
    high_52w: Decimal
    currency: str
    as_of: str
    source: str

    @property
    def drawdown_pct(self) -> Decimal:
        if self.high_52w <= 0:
            return Decimal("0")
        return (self.high_52w - self.price) / self.high_52w * Decimal("100")


def decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def load_json(path: str, default: Any | None = None) -> Any:
    if not os.path.exists(path):
        if default is not None:
            return default
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(temp_path, path)


def request_json(url: str, timeout: int = 15) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def request_text(url: str, timeout: int = 15) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def parse_tencent_line(
    line: str,
    quote_code: str,
    currency: str,
) -> Snapshot:
    start = line.find('"')
    end = line.rfind('"')
    if start < 0 or end <= start:
        raise RuntimeError("empty Tencent quote")
    fields = line[start + 1 : end].split("~")
    if len(fields) < 50:
        raise RuntimeError(f"short Tencent quote: {len(fields)} fields")

    price = decimal(fields[3])
    if quote_code.startswith(("sh", "sz")):
        high_52w = decimal(fields[47])
    else:
        high_52w = decimal(fields[48])
    if high_52w < price:
        high_52w = price
    source_currency = fields[35] if quote_code.startswith("us") else ""
    return Snapshot(
        price=price,
        high_52w=high_52w,
        currency=source_currency or currency,
        as_of=fields[30],
        source="Tencent quote",
    )


def fetch_tencent_snapshots(
    watchlist: list[dict[str, Any]],
) -> tuple[dict[str, Snapshot], dict[str, str]]:
    code_to_item = {
        item["quote_code"]: item for item in watchlist if item.get("quote_code")
    }
    if not code_to_item:
        return {}, {}
    query = urllib.parse.quote(",".join(code_to_item), safe=",.")
    raw = request_text(f"https://qt.gtimg.cn/q={query}")
    lines_by_key = {}
    for line in raw.splitlines():
        key, separator, _ = line.partition("=")
        if separator and key.startswith("v_"):
            lines_by_key[key[2:]] = line

    snapshots: dict[str, Snapshot] = {}
    errors: dict[str, str] = {}
    for quote_code, item in code_to_item.items():
        symbol = item["symbol"]
        line = lines_by_key.get(quote_code)
        if not line:
            errors[symbol] = "Tencent response missing quote"
            continue
        try:
            snapshots[symbol] = parse_tencent_line(
                line,
                quote_code=quote_code,
                currency=item.get("currency", ""),
            )
        except Exception as exc:
            errors[symbol] = str(exc)
    return snapshots, errors


def fetch_yahoo_snapshot(symbol: str) -> Snapshot:
    encoded = urllib.parse.quote(symbol, safe="")
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{encoded}?range=1y&interval=1d"
    )
    data = request_json(url)
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        error = data.get("chart", {}).get("error")
        raise RuntimeError(f"empty Yahoo response: {error}")

    meta = result.get("meta", {})
    quote = ((result.get("indicators", {}).get("quote") or [{}])[0])
    closes = [decimal(value) for value in quote.get("close", []) if value is not None]
    highs = [decimal(value) for value in quote.get("high", []) if value is not None]
    if not closes:
        raise RuntimeError("Yahoo response has no close prices")

    raw_price = meta.get("regularMarketPrice")
    price = decimal(raw_price) if raw_price is not None else closes[-1]
    high_52w = max(highs or closes)
    timestamp = meta.get("regularMarketTime")
    as_of = (
        datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
        if timestamp
        else ""
    )
    return Snapshot(
        price=price,
        high_52w=high_52w,
        currency=meta.get("currency") or "",
        as_of=as_of,
        source="Yahoo Finance chart",
    )


def load_price_overrides(path: str) -> dict[str, Snapshot]:
    raw = load_json(path)
    snapshots: dict[str, Snapshot] = {}
    for symbol, item in raw.items():
        snapshots[symbol] = Snapshot(
            price=decimal(item["price"]),
            high_52w=decimal(item.get("high_52w", item["price"])),
            currency=item.get("currency", ""),
            as_of=item.get("as_of", ""),
            source=item.get("source", f"file:{path}"),
        )
    return snapshots


def snapshots_from_state(state: dict[str, Any]) -> dict[str, Snapshot]:
    snapshots: dict[str, Snapshot] = {}
    for symbol, item in state.get("symbols", {}).items():
        if item.get("price") is None or item.get("high_52w") is None:
            continue
        snapshots[symbol] = Snapshot(
            price=decimal(item["price"]),
            high_52w=decimal(item["high_52w"]),
            currency=item.get("currency", ""),
            as_of=item.get("as_of", ""),
            source="last successful snapshot",
        )
    return snapshots


def active_price_level(item: dict[str, Any], price: Decimal) -> dict[str, Any] | None:
    levels = sorted(
        item.get("price_levels", []),
        key=lambda level: decimal(level["at_or_below"]),
        reverse=True,
    )
    active = None
    for severity, level in enumerate(levels, start=1):
        if price <= decimal(level["at_or_below"]):
            active = dict(level, severity=severity)
    return active


def active_drawdown_level(
    item: dict[str, Any], drawdown_pct: Decimal
) -> dict[str, Any] | None:
    levels = sorted(
        item.get("drawdown_levels", []),
        key=lambda level: decimal(level["at_or_above_pct"]),
    )
    active = None
    for severity, level in enumerate(levels, start=1):
        if drawdown_pct >= decimal(level["at_or_above_pct"]):
            active = dict(level, severity=severity)
    return active


def is_new_or_deeper(
    current: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    repeat_active: bool,
) -> bool:
    if current is None:
        return False
    if repeat_active:
        return True
    if previous is None:
        return True
    return int(current["severity"]) > int(previous.get("severity", 0))


def compact_level(level: dict[str, Any] | None) -> dict[str, Any] | None:
    if level is None:
        return None
    return {"id": level["id"], "severity": level["severity"]}


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):,.2f}"


def evaluate(
    config: dict[str, Any],
    snapshots: dict[str, Snapshot],
    previous_state: dict[str, Any],
    repeat_active: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    next_state = {
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        "symbols": {},
    }
    old_symbols = previous_state.get("symbols", {})

    for item in config.get("watchlist", []):
        symbol = item["symbol"]
        snapshot = snapshots[symbol]
        price_level = active_price_level(item, snapshot.price)
        drawdown_level = active_drawdown_level(item, snapshot.drawdown_pct)
        old = old_symbols.get(symbol, {})

        if is_new_or_deeper(price_level, old.get("price_level"), repeat_active):
            alerts.append(
                {
                    "kind": "price",
                    "name": item["name"],
                    "symbol": symbol,
                    "snapshot": snapshot,
                    "level": price_level,
                    "gate": item.get("gate", ""),
                }
            )
        if is_new_or_deeper(drawdown_level, old.get("drawdown_level"), repeat_active):
            alerts.append(
                {
                    "kind": "drawdown",
                    "name": item["name"],
                    "symbol": symbol,
                    "snapshot": snapshot,
                    "level": drawdown_level,
                    "gate": item.get("gate", ""),
                }
            )

        next_state["symbols"][symbol] = {
            "price_level": compact_level(price_level),
            "drawdown_level": compact_level(drawdown_level),
            "price": str(snapshot.price),
            "high_52w": str(snapshot.high_52w),
            "currency": snapshot.currency,
            "as_of": snapshot.as_of,
            "source": snapshot.source,
        }
        rows.append(
            {
                "name": item["name"],
                "symbol": symbol,
                "snapshot": snapshot,
                "price_level": price_level,
                "drawdown_level": drawdown_level,
            }
        )
    return alerts, rows, next_state


def render_alert(alert: dict[str, Any]) -> str:
    snapshot = alert["snapshot"]
    level = alert["level"]
    if alert["kind"] == "price":
        condition = f"价格 <= {money(decimal(level['at_or_below']))}"
    else:
        condition = f"52周回撤 >= {decimal(level['at_or_above_pct'])}%"
    line = (
        f"- {alert['name']} ({alert['symbol']}): 当前 {money(snapshot.price)} "
        f"{snapshot.currency}, 52周回撤 {snapshot.drawdown_pct:.1f}%；"
        f"触发“{level['label']}”（{condition}）。{level['action']}"
    )
    if alert["gate"]:
        line += f" 组合约束：{alert['gate']}"
    return line


def render_dashboard(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "",
        "| 标的 | 当前价 | 52周高点 | 回撤 | 当前价格档 | 当前回撤档 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in rows:
        snapshot = row["snapshot"]
        price_level = row["price_level"]
        drawdown_level = row["drawdown_level"]
        lines.append(
            "| {name} | {price} {currency} | {high} | {drawdown:.1f}% | "
            "{price_band} | {drawdown_band} |".format(
                name=row["name"],
                price=money(snapshot.price),
                currency=snapshot.currency,
                high=money(snapshot.high_52w),
                drawdown=snapshot.drawdown_pct,
                price_band=price_level["label"] if price_level else "-",
                drawdown_band=drawdown_level["label"] if drawdown_level else "-",
            )
        )
    return lines


def command_check(args: argparse.Namespace) -> int:
    config = load_json(args.config)
    previous_state = load_json(args.state, default={"symbols": {}})
    overrides = load_price_overrides(args.prices_file) if args.prices_file else {}
    cached = snapshots_from_state(previous_state)
    snapshots: dict[str, Snapshot] = dict(overrides)
    errors: list[str] = []
    warnings: list[str] = []

    watchlist = config.get("watchlist", [])
    if not args.prices_file:
        try:
            tencent_snapshots, tencent_errors = fetch_tencent_snapshots(watchlist)
            snapshots.update(tencent_snapshots)
        except Exception as exc:
            tencent_errors = {
                item["symbol"]: f"Tencent batch request failed: {exc}"
                for item in watchlist
            }
    else:
        tencent_errors = {}

    for item in watchlist:
        symbol = item["symbol"]
        if symbol in snapshots:
            continue
        try:
            snapshots[symbol] = fetch_yahoo_snapshot(
                item.get("quote_symbol", symbol)
            )
        except Exception as exc:
            if symbol in cached:
                snapshots[symbol] = cached[symbol]
                warnings.append(
                    f"{item['name']} ({symbol}) 使用上次快照；"
                    f"腾讯: {tencent_errors.get(symbol, '无数据')}；Yahoo: {exc}"
                )
            else:
                errors.append(
                    f"{item['name']} ({symbol})；"
                    f"腾讯: {tencent_errors.get(symbol, '无数据')}；Yahoo: {exc}"
                )

    evaluable = {
        item["symbol"]
        for item in config.get("watchlist", [])
        if item["symbol"] in snapshots
    }
    evaluation_config = dict(config)
    evaluation_config["watchlist"] = [
        item for item in config.get("watchlist", []) if item["symbol"] in evaluable
    ]
    alerts, rows, next_state = evaluate(
        evaluation_config,
        snapshots,
        previous_state,
        repeat_active=args.repeat_active,
    )

    if not args.no_state and rows:
        write_json(args.state, next_state)

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    if not rows:
        print(f"DATA_UNAVAILABLE | {now} | checked=0 | errors={len(errors)}")
    elif alerts:
        print(f"ALERTS={len(alerts)} | {now}")
        for alert in alerts:
            print(render_alert(alert))
        print("")
        print("执行前统一检查：")
        for gate in config.get("portfolio_gates", []):
            print(f"- {gate}")
    else:
        print(
            f"NO_ALERTS | {now} | checked={len(rows)} | "
            f"warnings={len(warnings)} | errors={len(errors)}"
        )

    if args.show_all:
        print("\n".join(render_dashboard(rows)))
    if errors:
        print("\n数据错误：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
    if warnings and args.show_all:
        print("\n数据警告：", file=sys.stderr)
        for warning in warnings:
            print(f"- {warning}", file=sys.stderr)
    return 1 if not rows else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor investment trigger levels.")
    subparsers = parser.add_subparsers(dest="command")
    check = subparsers.add_parser("check", help="Fetch prices and evaluate triggers.")
    check.add_argument("--config", default=DEFAULT_CONFIG)
    check.add_argument("--state", default=DEFAULT_STATE)
    check.add_argument("--prices-file", help="Optional offline snapshot JSON.")
    check.add_argument("--show-all", action="store_true")
    check.add_argument("--repeat-active", action="store_true")
    check.add_argument("--no-state", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in {None, "check"}:
        if args.command is None:
            args = parser.parse_args(["check"])
        return command_check(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
