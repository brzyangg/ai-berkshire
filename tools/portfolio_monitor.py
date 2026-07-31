#!/usr/bin/env python3
"""Monitor investment price ladders and trailing drawdowns.

The monitor uses public Tencent quotes. A-share prices and unadjusted 52-week
highs must also pass an independent Eastmoney check before they can trigger a
signal. Yahoo and the last good snapshot remain fallbacks for other markets.
It stores the last active band so a scheduled run only reports a new, deeper
trigger. It never places orders.

Examples:
  python3 tools/portfolio_monitor.py check --show-all
  python3 tools/portfolio_monitor.py check --repeat-active
  python3 tools/portfolio_monitor.py check --prices-file /tmp/prices.json
  python3 tools/portfolio_monitor.py check --metrics-file /tmp/metrics.json
  python3 tools/portfolio_monitor.py list
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "data", "portfolio", "watchlist.json")
DEFAULT_STATE = os.path.join(ROOT, "data", "portfolio", "monitor_state.json")
EASTMONEY_UT = "fa5fd1943c7b386f172d6893dbfba10b"
ASHARE_PREFIXES = ("sh", "sz")
ASHARE_HIGH_TOLERANCE = Decimal("0.001")


@dataclass
class Snapshot:
    price: Decimal
    high_52w: Decimal
    currency: str
    as_of: str
    source: str
    high_52w_basis: str = ""

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


def request_bytes(
    url: str,
    timeout: int = 15,
    attempts: int = 3,
) -> tuple[bytes, str | None]:
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                )
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers.get_content_charset()
        except urllib.error.HTTPError as exc:
            retryable = exc.code in retryable_statuses
            if not retryable or attempt == attempts - 1:
                raise
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.RemoteDisconnected,
        ):
            if attempt == attempts - 1:
                raise
        time.sleep(0.5 * (2**attempt))
    raise RuntimeError("request retry loop ended unexpectedly")


def request_json(url: str, timeout: int = 15) -> dict[str, Any]:
    raw, _ = request_bytes(url, timeout=timeout)
    return json.loads(raw.decode("utf-8"))


def request_text(
    url: str,
    timeout: int = 15,
    default_encoding: str = "utf-8",
) -> str:
    raw, response_encoding = request_bytes(url, timeout=timeout)
    encodings = [response_encoding, default_encoding, "utf-8", "gbk"]
    for encoding in dict.fromkeys(value for value in encodings if value):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode(default_encoding, errors="replace")


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
    high_index = 67 if quote_code.startswith(("sh", "sz")) else 48
    if len(fields) <= high_index:
        raise RuntimeError(
            f"short Tencent quote: {len(fields)} fields, "
            f"need index {high_index}"
        )

    price = decimal(fields[3])
    high_52w = decimal(fields[high_index])
    if high_52w < price:
        high_52w = price
    source_currency = fields[35] if quote_code.startswith("us") else ""
    return Snapshot(
        price=price,
        high_52w=high_52w,
        currency=source_currency or currency,
        as_of=fields[30],
        source="Tencent quote",
        high_52w_basis=(
            "forward-adjusted quote"
            if quote_code.startswith(("sh", "sz"))
            else "quote 52-week intraday high"
        ),
    )


def parse_tencent_kline_high(
    data: dict[str, Any],
    quote_code: str,
) -> tuple[Decimal, str]:
    item = data.get("data", {}).get(quote_code, {})
    rows = item.get("day") or item.get("bfqday")
    if not rows:
        raise RuntimeError("Tencent kline response has no unadjusted daily data")

    parsed_rows: list[tuple[date, Decimal]] = []
    for row in rows:
        if len(row) < 4:
            continue
        try:
            parsed_rows.append(
                (date.fromisoformat(str(row[0])), decimal(row[3]))
            )
        except (TypeError, ValueError):
            continue
    return rolling_52w_high(parsed_rows, "Tencent")


def fetch_tencent_kline_high(quote_code: str) -> tuple[Decimal, str]:
    query = urllib.parse.urlencode(
        {"param": f"{quote_code},day,,,320,bfq"}
    )
    data = request_json(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        f"{query}"
    )
    return parse_tencent_kline_high(data, quote_code)


def rolling_52w_high(
    rows: list[tuple[date, Decimal]],
    source_name: str,
) -> tuple[Decimal, str]:
    if not rows:
        raise RuntimeError(f"{source_name} kline response has no valid daily highs")
    latest_date = max(row_date for row_date, _ in rows)
    cutoff = latest_date - timedelta(weeks=52)
    highs = [
        high
        for row_date, high in rows
        if cutoff <= row_date <= latest_date
    ]
    if not highs:
        raise RuntimeError(
            f"{source_name} kline response has no data in last 52 weeks"
        )
    return max(highs), latest_date.isoformat()


def eastmoney_secid(quote_code: str) -> str:
    if quote_code.startswith("sh"):
        market = "1"
    elif quote_code.startswith("sz"):
        market = "0"
    else:
        raise ValueError(f"not an A-share quote code: {quote_code}")
    code = quote_code[2:]
    if not code.isdigit():
        raise ValueError(f"invalid A-share quote code: {quote_code}")
    return f"{market}.{code}"


def parse_eastmoney_kline_snapshot(
    data: dict[str, Any],
) -> tuple[Decimal, Decimal, Decimal, str]:
    item = data.get("data")
    rows = item.get("klines") if isinstance(item, dict) else None
    if not rows:
        raise RuntimeError("Eastmoney kline response has no unadjusted daily data")

    parsed_rows: list[tuple[date, Decimal, Decimal, Decimal]] = []
    for row in rows:
        fields = str(row).split(",")
        if len(fields) < 4:
            continue
        try:
            close = decimal(fields[2])
            high = decimal(fields[3])
            price_tick = Decimal(1).scaleb(
                min(close.as_tuple().exponent, high.as_tuple().exponent)
            )
            parsed_rows.append(
                (date.fromisoformat(fields[0]), close, high, price_tick)
            )
        except (TypeError, ValueError):
            continue
    if not parsed_rows:
        raise RuntimeError("Eastmoney kline response has no valid daily rows")
    latest_date, latest_close, _, price_tick = max(
        parsed_rows,
        key=lambda row: row[0],
    )
    high_52w, high_as_of = rolling_52w_high(
        [(row_date, high) for row_date, _, high, _ in parsed_rows],
        "Eastmoney",
    )
    if latest_date.isoformat() != high_as_of:
        raise RuntimeError("Eastmoney kline latest-date calculation mismatch")
    return latest_close, price_tick, high_52w, high_as_of


def parse_eastmoney_kline_high(
    data: dict[str, Any],
) -> tuple[Decimal, str]:
    _, _, high_52w, as_of = parse_eastmoney_kline_snapshot(data)
    return high_52w, as_of


def fetch_eastmoney_kline_snapshot(
    quote_code: str,
) -> tuple[Decimal, Decimal, Decimal, str]:
    query = urllib.parse.urlencode(
        {
            "secid": eastmoney_secid(quote_code),
            "ut": EASTMONEY_UT,
            "klt": "101",
            "fqt": "0",
            "beg": (date.today() - timedelta(days=400)).strftime("%Y%m%d"),
            "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
    )
    data = request_json(
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{query}"
    )
    item = data.get("data")
    expected_code = quote_code[2:]
    if not isinstance(item, dict) or str(item.get("code", "")) != expected_code:
        actual_code = item.get("code") if isinstance(item, dict) else None
        raise RuntimeError(
            "Eastmoney kline security mismatch: "
            f"expected {expected_code}, got {actual_code}"
        )
    return parse_eastmoney_kline_snapshot(data)


def normalized_quote_date(value: str) -> str:
    compact = re.sub(r"\D", "", value)
    if len(compact) < 8:
        raise RuntimeError(f"quote timestamp has no trading date: {value!r}")
    return datetime.strptime(compact[:8], "%Y%m%d").date().isoformat()


def verify_ashare_snapshot(
    snapshot: Snapshot,
    quote_code: str,
    tencent_high: tuple[Decimal, str],
    eastmoney_kline: tuple[Decimal, Decimal, Decimal, str],
) -> Snapshot:
    (
        secondary_price,
        price_tick,
        secondary_high,
        secondary_high_as_of,
    ) = eastmoney_kline
    primary_quote_as_of = normalized_quote_date(snapshot.as_of)
    if primary_quote_as_of != secondary_high_as_of:
        raise RuntimeError(
            f"{quote_code} current price date conflict: "
            f"Tencent={primary_quote_as_of}, "
            f"Eastmoney={secondary_high_as_of}"
        )
    price_tolerance = max(
        price_tick * Decimal("5"),
        max(snapshot.price, secondary_price) * Decimal("0.001"),
    )
    if abs(snapshot.price - secondary_price) > price_tolerance:
        raise RuntimeError(
            f"{quote_code} current price data conflict: "
            f"Tencent={snapshot.price}, Eastmoney={secondary_price}, "
            f"tolerance={price_tolerance}"
        )

    primary_high, primary_high_as_of = tencent_high
    if primary_high_as_of != secondary_high_as_of:
        raise RuntimeError(
            f"{quote_code} 52-week high date conflict: "
            f"Tencent={primary_high_as_of}, "
            f"Eastmoney={secondary_high_as_of}"
        )
    if abs(primary_high - secondary_high) > ASHARE_HIGH_TOLERANCE:
        raise RuntimeError(
            f"{quote_code} 52-week unadjusted intraday high conflict: "
            f"Tencent={primary_high}, Eastmoney={secondary_high}, "
            f"tolerance={ASHARE_HIGH_TOLERANCE}"
        )

    verified_high = max(primary_high, secondary_high, snapshot.price)
    snapshot.high_52w = verified_high
    snapshot.high_52w_basis = (
        "unadjusted intraday high verified by Tencent+Eastmoney "
        f"through {primary_high_as_of}"
    )
    snapshot.source = (
        "腾讯实时行情（东方财富不复权日K复核）；"
        "腾讯/东方财富52周高点双源通过"
    )
    return snapshot


def fetch_tencent_snapshots(
    watchlist: list[dict[str, Any]],
) -> tuple[dict[str, Snapshot], dict[str, str]]:
    code_to_item = {
        item["quote_code"]: item for item in watchlist if item.get("quote_code")
    }
    if not code_to_item:
        return {}, {}
    query = urllib.parse.quote(",".join(code_to_item), safe=",.")
    raw = request_text(
        f"https://qt.gtimg.cn/q={query}",
        default_encoding="gbk",
    )
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
            snapshot = parse_tencent_line(
                line,
                quote_code=quote_code,
                currency=item.get("currency", ""),
            )
            if quote_code.startswith(ASHARE_PREFIXES):
                snapshot = verify_ashare_snapshot(
                    snapshot,
                    quote_code,
                    fetch_tencent_kline_high(quote_code),
                    fetch_eastmoney_kline_snapshot(quote_code),
                )
            snapshots[symbol] = snapshot
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
        high_52w_basis="unadjusted daily intraday high",
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
            high_52w_basis=item.get("high_52w_basis", "file override"),
        )
    return snapshots


def load_metric_overrides(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    raw = load_json(path)
    if not isinstance(raw, dict):
        raise ValueError("metrics file must contain an object keyed by symbol")
    metrics = {}
    for symbol, values in raw.items():
        if not isinstance(values, dict):
            raise ValueError(f"metrics for {symbol} must be an object")
        metrics[symbol] = values
    return metrics


class VisibleTextParser(HTMLParser):
    """Collect visible text without depending on third-party HTML packages."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            value = data.strip()
            if value:
                self.parts.append(value)


def visible_html_text(raw: str) -> str:
    parser = VisibleTextParser()
    parser.feed(raw)
    parser.close()
    return "\n".join(parser.parts)


def regex_value(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text, flags=re.DOTALL)
    if not match:
        raise RuntimeError(f"page missing {label}")
    return match.group(1)


def normalize_date(value: str) -> str:
    parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    return parsed.isoformat()


def parse_index_valuation_html(raw: str) -> tuple[dict[str, Any], str]:
    text = visible_html_text(raw)
    as_of = normalize_date(
        regex_value(
            r"最新交易日\s*[:：]\s*(\d{4}-\d{1,2}-\d{1,2})",
            text,
            "latest trading date",
        )
    )
    compact = re.sub(r"\s+", "", text)
    section = regex_value(
        r"(PE[·・]市盈率.*?)(?:PB[·・]市净率|$)",
        compact,
        "PE section",
    )
    pe = regex_value(
        r"PE[·・]市盈率[^\d]*(\d+(?:\.\d+)?)",
        section,
        "PE",
    )
    percentile = regex_value(
        r"近10年百分位(\d+(?:\.\d+)?)%",
        section,
        "10-year PE percentile",
    )
    return {"pe": pe, "valuation_percentile": percentile}, as_of


def parse_fund_nav_html(raw: str) -> tuple[dict[str, Any], str]:
    text = visible_html_text(raw)
    nav = regex_value(
        r"单位净值\s*[:：]\s*(\d+(?:\.\d+)?)",
        text,
        "unit NAV",
    )
    as_of = normalize_date(
        regex_value(
            r"数据日期\s*[:：]\s*(\d{4}-\d{1,2}-\d{1,2})",
            text,
            "NAV date",
        )
    )
    status = regex_value(
        r"开放申购\s*[:：]\s*(是|否)",
        text,
        "subscription status",
    )
    return {
        "nav": nav,
        "subscription_open": status == "是",
    }, as_of


def metric_date_issue(
    as_of: str,
    max_age_days: int,
    today: datetime | None = None,
) -> str | None:
    current_date = (today or datetime.now().astimezone()).date()
    data_date = datetime.strptime(as_of, "%Y-%m-%d").date()
    age_days = (current_date - data_date).days
    if age_days < 0:
        return f"数据日期 {as_of} 晚于当前日期"
    if age_days > max_age_days:
        return f"数据日期 {as_of} 已超过 {max_age_days} 天新鲜度限制"
    return None


def required_metric_names(item: dict[str, Any]) -> set[str]:
    return {
        check["metric"]
        for group in item.get("metric_gate_groups", [])
        for check in group.get("checks", [])
    }


def fetch_item_metrics(
    item: dict[str, Any],
    snapshot: Snapshot,
    today: datetime | None = None,
) -> dict[str, Any]:
    source = item["metric_source"]
    source_type = source["type"]
    source_name = source.get("name", source_type)
    url = source["url"]
    raw = request_text(
        url,
        timeout=int(source.get("timeout_seconds", 15)),
        default_encoding=source.get("encoding", "utf-8"),
    )
    if source_type == "index_valuation":
        values, as_of = parse_index_valuation_html(raw)
        context = ""
    elif source_type == "fund_nav":
        parsed, as_of = parse_fund_nav_html(raw)
        nav = decimal(parsed["nav"])
        premium_pct = (
            (snapshot.price / nav - Decimal("1")) * Decimal("100")
        ).quantize(Decimal("0.01"))
        values = {
            "premium_pct": str(premium_pct),
            "subscription_open": parsed["subscription_open"],
        }
        context = f"净值 {nav}"
    else:
        raise ValueError(f"unsupported metric source type: {source_type}")

    issue = metric_date_issue(
        as_of,
        int(source.get("max_age_days", 5)),
        today=today,
    )
    meta = {
        "source": source_name,
        "as_of": as_of,
        "url": url,
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    if context:
        meta["context"] = context
    if issue:
        return {"_meta": dict(meta, issue=issue)}
    return dict(values, _meta=meta)


def metric_values_are_fresh(
    values: dict[str, Any],
    max_age_days: int,
    today: datetime | None = None,
) -> bool:
    meta = values.get("_meta", {})
    as_of = meta.get("as_of")
    if not as_of or meta.get("issue"):
        return False
    try:
        return metric_date_issue(as_of, max_age_days, today=today) is None
    except (TypeError, ValueError):
        return False


def fetch_auto_metrics(
    watchlist: list[dict[str, Any]],
    snapshots: dict[str, Snapshot],
    cached_metrics: dict[str, dict[str, Any]] | None = None,
    today: datetime | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    metrics: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    cache = cached_metrics or {}
    for item in watchlist:
        source = item.get("metric_source")
        symbol = item["symbol"]
        if not source or symbol not in snapshots:
            continue
        try:
            values = fetch_item_metrics(item, snapshots[symbol], today=today)
            issue = values.get("_meta", {}).get("issue")
            if issue:
                raise RuntimeError(issue)
            metrics[symbol] = values
        except Exception as exc:
            cached = cache.get(symbol)
            max_age_days = int(source.get("max_age_days", 5))
            if cached and metric_values_are_fresh(
                cached,
                max_age_days,
                today=today,
            ):
                values = dict(cached)
                meta = dict(values.get("_meta", {}))
                meta["cached"] = True
                values["_meta"] = meta
                metrics[symbol] = values
                warnings.append(
                    f"{item['name']} ({symbol}) 自动指标获取失败，"
                    f"使用 {meta.get('as_of', '未知日期')} 缓存：{exc}"
                )
            else:
                metrics[symbol] = {
                    "_meta": {
                        "source": source.get("name", source["type"]),
                        "url": source["url"],
                        "issue": str(exc),
                    }
                }
                warnings.append(
                    f"{item['name']} ({symbol}) 自动指标不可用：{exc}"
                )
    return metrics, warnings


def merge_metrics(
    auto_metrics: dict[str, dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
    watchlist: list[dict[str, Any]],
    override_source: str | None = None,
) -> dict[str, dict[str, Any]]:
    merged = {
        symbol: dict(values)
        for symbol, values in auto_metrics.items()
    }
    item_by_symbol = {item["symbol"]: item for item in watchlist}
    for symbol, override_values in overrides.items():
        values = merged.setdefault(symbol, {})
        auto_meta = dict(values.get("_meta", {}))
        values.update(override_values)
        override_meta = override_values.get("_meta", {})
        meta = dict(auto_meta, **override_meta)
        meta["override"] = override_source or "metrics file"
        required = required_metric_names(item_by_symbol.get(symbol, {}))
        if required and required.issubset(values):
            meta.pop("issue", None)
        values["_meta"] = meta
    return merged


def snapshots_from_state(state: dict[str, Any]) -> dict[str, Snapshot]:
    snapshots: dict[str, Snapshot] = {}
    for symbol, item in state.get("symbols", {}).items():
        if item.get("price") is None or item.get("high_52w") is None:
            continue
        is_ashare = symbol.endswith((".SS", ".SZ"))
        high_basis = item.get("high_52w_basis", "")
        if is_ashare and (
            not high_basis.startswith("unadjusted")
            or "verified by Tencent+Eastmoney" not in high_basis
        ):
            continue
        snapshots[symbol] = Snapshot(
            price=decimal(item["price"]),
            high_52w=decimal(item["high_52w"]),
            currency=item.get("currency", ""),
            as_of=item.get("as_of", ""),
            source="last successful snapshot",
            high_52w_basis=high_basis,
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


def compare_metric(actual: Any, operator: str, expected: Any) -> bool:
    if isinstance(expected, bool):
        if not isinstance(actual, bool):
            raise ValueError(f"expected boolean metric, got {actual!r}")
        left, right = actual, expected
    elif operator in {"==", "!="} and isinstance(expected, str):
        left, right = str(actual), expected
    else:
        left, right = decimal(actual), decimal(expected)

    operations = {
        "<=": lambda: left <= right,
        "<": lambda: left < right,
        ">=": lambda: left >= right,
        ">": lambda: left > right,
        "==": lambda: left == right,
        "!=": lambda: left != right,
    }
    if operator not in operations:
        raise ValueError(f"unsupported metric operator: {operator}")
    return operations[operator]()


def metric_gate_status(
    item: dict[str, Any],
    metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    groups = item.get("metric_gate_groups", [])
    if not groups:
        return {
            "status": "not_required",
            "details": [],
            "missing": [],
            "source_note": "",
        }

    values = metrics or {}
    meta = values.get("_meta", {})
    source_parts = []
    if meta.get("source"):
        source_parts.append(str(meta["source"]))
    if meta.get("as_of"):
        source_parts.append(str(meta["as_of"]))
    if meta.get("context"):
        source_parts.append(str(meta["context"]))
    if meta.get("cached"):
        source_parts.append("缓存")
    if meta.get("override"):
        source_parts.append(f"覆盖:{meta['override']}")
    source_note = " ".join(source_parts)
    source_issue = meta.get("issue")
    group_statuses = []
    details = []
    missing = []
    failed = []
    for group in groups:
        mode = group.get("mode", "all")
        if mode not in {"all", "any"}:
            raise ValueError(f"unsupported metric gate mode: {mode}")
        check_statuses = []
        for check in group.get("checks", []):
            metric = check["metric"]
            label = check.get(
                "label",
                f"{metric} {check['operator']} {check['value']}",
            )
            if metric not in values:
                check_statuses.append("unknown")
                if source_issue:
                    details.append(f"{label}: 数据不可用({source_issue})")
                else:
                    details.append(f"{label}: 缺少数据")
                missing.append(label)
                continue
            try:
                passed = compare_metric(
                    values[metric],
                    check["operator"],
                    check["value"],
                )
            except (TypeError, ValueError) as exc:
                check_statuses.append("unknown")
                details.append(f"{label}: 数据错误({exc})")
                continue
            check_statuses.append("passed" if passed else "blocked")
            result = (
                f"{label}: {'通过' if passed else '不通过'}"
                f"(实际 {values[metric]})"
            )
            details.append(result)
            if not passed:
                failed.append(f"{label}(实际 {values[metric]})")

        if not check_statuses:
            group_statuses.append("unknown")
        elif mode == "all":
            if "blocked" in check_statuses:
                group_statuses.append("blocked")
            elif all(status == "passed" for status in check_statuses):
                group_statuses.append("passed")
            else:
                group_statuses.append("unknown")
        elif "passed" in check_statuses:
            group_statuses.append("passed")
        elif all(status == "blocked" for status in check_statuses):
            group_statuses.append("blocked")
        else:
            group_statuses.append("unknown")

    if "blocked" in group_statuses:
        status = "blocked"
    elif group_statuses and all(value == "passed" for value in group_statuses):
        status = "passed"
    else:
        status = "unknown"
    return {
        "status": status,
        "details": details,
        "missing": missing,
        "failed": failed,
        "source_note": source_note,
        "source_issue": source_issue,
    }


def quote_targets(watchlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return tradable instruments plus any separate drawdown benchmarks."""
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in watchlist:
        candidates = [item]
        benchmark = item.get("benchmark")
        if benchmark:
            candidates.append(
                {
                    "name": benchmark.get("name", f"{item['name']}回撤基准"),
                    "symbol": benchmark["symbol"],
                    "quote_code": benchmark.get("quote_code"),
                    "quote_symbol": benchmark.get(
                        "quote_symbol",
                        benchmark["symbol"],
                    ),
                    "currency": benchmark.get("currency", ""),
                }
            )
        for candidate in candidates:
            symbol = candidate["symbol"]
            if symbol in seen:
                continue
            seen.add(symbol)
            targets.append(candidate)
    return targets


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):,.2f}"


def display_timestamp(value: str) -> str:
    compact = re.sub(r"\D", "", value)
    if len(compact) >= 14:
        try:
            parsed = datetime.strptime(compact[:14], "%Y%m%d%H%M%S")
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return value or "未知"


def evaluate(
    config: dict[str, Any],
    snapshots: dict[str, Snapshot],
    previous_state: dict[str, Any],
    repeat_active: bool,
    metrics: dict[str, dict[str, Any]] | None = None,
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
        benchmark = item.get("benchmark")
        drawdown_snapshot = (
            snapshots[benchmark["symbol"]]
            if benchmark
            else snapshot
        )
        drawdown_name = (
            benchmark.get("name", benchmark["symbol"])
            if benchmark
            else "本标的"
        )
        price_level = active_price_level(item, snapshot.price)
        drawdown_level = active_drawdown_level(
            item,
            drawdown_snapshot.drawdown_pct,
        )
        metric_gate = metric_gate_status(item, (metrics or {}).get(symbol))
        old = old_symbols.get(symbol, {})

        if is_new_or_deeper(price_level, old.get("price_level"), repeat_active):
            alerts.append(
                {
                    "kind": "price",
                    "name": item["name"],
                    "symbol": symbol,
                    "snapshot": snapshot,
                    "drawdown_snapshot": drawdown_snapshot,
                    "drawdown_name": drawdown_name,
                    "level": price_level,
                    "gate": item.get("gate", ""),
                    "metric_gate": metric_gate,
                }
            )
        if is_new_or_deeper(drawdown_level, old.get("drawdown_level"), repeat_active):
            alerts.append(
                {
                    "kind": "drawdown",
                    "name": item["name"],
                    "symbol": symbol,
                    "snapshot": snapshot,
                    "drawdown_snapshot": drawdown_snapshot,
                    "drawdown_name": drawdown_name,
                    "level": drawdown_level,
                    "gate": item.get("gate", ""),
                    "metric_gate": metric_gate,
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
            "high_52w_basis": snapshot.high_52w_basis,
        }
        rows.append(
            {
                "name": item["name"],
                "symbol": symbol,
                "snapshot": snapshot,
                "drawdown_snapshot": drawdown_snapshot,
                "drawdown_name": drawdown_name,
                "price_level": price_level,
                "drawdown_level": drawdown_level,
                "metric_gate": metric_gate,
            }
        )
    return alerts, rows, next_state


def render_alert(alert: dict[str, Any]) -> str:
    snapshot = alert["snapshot"]
    drawdown_snapshot = alert.get("drawdown_snapshot", snapshot)
    drawdown_name = alert.get("drawdown_name", "本标的")
    level = alert["level"]
    if alert["kind"] == "price":
        condition = f"价格 <= {money(decimal(level['at_or_below']))}"
        market_state = f"52周回撤 {drawdown_snapshot.drawdown_pct:.1f}%"
    else:
        condition = f"52周回撤 >= {decimal(level['at_or_above_pct'])}%"
        market_state = (
            f"{drawdown_name} 52周回撤 "
            f"{drawdown_snapshot.drawdown_pct:.1f}%"
        )
    line = (
        f"- {alert['name']} ({alert['symbol']}): 当前 {money(snapshot.price)} "
        f"{snapshot.currency}, {market_state}；"
        f"触发“{level['label']}”（{condition}）。{level['action']}"
    )
    if alert["gate"]:
        line += f" 组合约束：{alert['gate']}"
    metric_gate = alert.get("metric_gate", {})
    status = metric_gate.get("status")
    if status in {"passed", "blocked", "unknown"}:
        unknown_label = (
            "指标数据不可用"
            if metric_gate.get("source_issue")
            else "缺少指标数据"
        )
        labels = {
            "passed": "通过",
            "blocked": "阻止",
            "unknown": unknown_label,
        }
        details = "；".join(metric_gate.get("details", []))
        line += f" 数据闸门：{labels[status]}"
        if details:
            line += f"（{details}）"
    return line


def row_conclusion(row: dict[str, Any]) -> str:
    triggered = bool(row["price_level"] or row["drawdown_level"])
    if not triggered:
        return "未触发"
    gate_status = row["metric_gate"]["status"]
    if gate_status in {"not_required", "passed"}:
        return "已触发，等待复核"
    if gate_status == "blocked":
        return "已触发，但指标不满足"
    if row["metric_gate"].get("source_issue"):
        return "已触发，但指标不可用"
    return "已触发，但缺指标"


def render_dashboard(rows: list[dict[str, Any]]) -> list[str]:
    lines = ["", f"监控明细（{len(rows)} 个标的）", "=" * 72]
    status_labels = {
        "not_required": "不需要",
        "passed": "已满足",
        "blocked": "未满足",
        "unknown": "数据不可用",
    }
    for index, row in enumerate(rows, start=1):
        snapshot = row["snapshot"]
        drawdown_snapshot = row["drawdown_snapshot"]
        price_level = row["price_level"]
        drawdown_level = row["drawdown_level"]
        metric_gate = row["metric_gate"]
        price_band = price_level["label"] if price_level else "未触发"
        drawdown_band = drawdown_level["label"] if drawdown_level else "未触发"
        basis_label = (
            "不复权"
            if drawdown_snapshot.high_52w_basis.startswith("unadjusted")
            else "行情口径"
        )
        lines.extend(
            [
                f"[{index:02d}] {row['name']} ({row['symbol']})",
                f"  当前价格：{money(snapshot.price)} {snapshot.currency}",
                (
                    f"  回撤情况：{drawdown_snapshot.drawdown_pct:.1f}%"
                    f"（{row['drawdown_name']} 52周盘中高点/{basis_label} "
                    f"{money(drawdown_snapshot.high_52w)} "
                    f"{drawdown_snapshot.currency}）"
                ),
                f"  触发档位：价格={price_band}；回撤={drawdown_band}",
                (
                    "  指标核验："
                    f"{status_labels.get(metric_gate['status'], '未知')}"
                ),
                (
                    f"  行情来源：{snapshot.source}"
                    f"；时间={display_timestamp(snapshot.as_of)}"
                ),
            ]
        )
        if drawdown_snapshot is not snapshot:
            lines.append(
                f"  回撤来源：{drawdown_snapshot.source}"
                f"；时间={display_timestamp(drawdown_snapshot.as_of)}"
            )
        if metric_gate["status"] != "not_required":
            for detail in metric_gate.get("details", []):
                lines.append(f"    - {detail}")
            if metric_gate.get("source_note"):
                lines.append(f"    - 来源：{metric_gate['source_note']}")
        lines.append(f"  当前结论：{row_conclusion(row)}")
        if index != len(rows):
            lines.append("-" * 72)
    return lines


def command_check(args: argparse.Namespace) -> int:
    config = load_json(args.config)
    previous_state = load_json(args.state, default={"symbols": {}})
    overrides = load_price_overrides(args.prices_file) if args.prices_file else {}
    metric_overrides = load_metric_overrides(args.metrics_file)
    cached = snapshots_from_state(previous_state)
    snapshots: dict[str, Snapshot] = dict(overrides)
    errors: list[str] = []
    warnings: list[str] = []

    watchlist = config.get("watchlist", [])
    targets = quote_targets(watchlist)
    if not args.prices_file:
        try:
            tencent_snapshots, tencent_errors = fetch_tencent_snapshots(targets)
            snapshots.update(tencent_snapshots)
        except Exception as exc:
            tencent_errors = {
                item["symbol"]: f"Tencent batch request failed: {exc}"
                for item in targets
            }
    else:
        tencent_errors = {}

    for item in targets:
        symbol = item["symbol"]
        if symbol in snapshots:
            continue
        if item.get("quote_code", "").startswith(ASHARE_PREFIXES):
            errors.append(
                f"{item['name']} ({symbol})；A股双源核验未通过，"
                f"停止告警计算；{tencent_errors.get(symbol, '腾讯无数据')}"
            )
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

    if args.no_auto_metrics:
        auto_metrics = {}
        metric_warnings = []
    else:
        auto_metrics, metric_warnings = fetch_auto_metrics(
            watchlist,
            snapshots,
            cached_metrics=previous_state.get("metrics", {}),
        )
    metrics = merge_metrics(
        auto_metrics,
        metric_overrides,
        watchlist,
        override_source=args.metrics_file,
    )
    warnings.extend(metric_warnings)

    evaluation_config = dict(config)
    evaluation_config["watchlist"] = [
        item
        for item in watchlist
        if item["symbol"] in snapshots
        and (
            not item.get("benchmark")
            or item["benchmark"]["symbol"] in snapshots
        )
    ]
    alerts, rows, next_state = evaluate(
        evaluation_config,
        snapshots,
        previous_state,
        repeat_active=args.repeat_active,
        metrics=metrics,
    )

    for target in targets:
        symbol = target["symbol"]
        if symbol not in snapshots or symbol in next_state["symbols"]:
            continue
        snapshot = snapshots[symbol]
        next_state["symbols"][symbol] = {
            "price_level": None,
            "drawdown_level": None,
            "price": str(snapshot.price),
            "high_52w": str(snapshot.high_52w),
            "currency": snapshot.currency,
            "as_of": snapshot.as_of,
            "source": snapshot.source,
            "high_52w_basis": snapshot.high_52w_basis,
        }

    if auto_metrics:
        next_state["metrics"] = auto_metrics

    if not args.no_state and rows:
        write_json(args.state, next_state)

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    active_count = sum(
        1 for row in rows if row["price_level"] or row["drawdown_level"]
    )
    if not rows:
        print(f"DATA_UNAVAILABLE | {now} | checked=0 | errors={len(errors)}")
    elif alerts:
        signal_type = "ACTIVE_SIGNALS" if args.repeat_active else "NEW_SIGNALS"
        print(f"{signal_type}={len(alerts)} | {now}")
        for alert in alerts:
            print(render_alert(alert))
        print("")
        print("执行前统一检查：")
        for gate in config.get("portfolio_gates", []):
            print(f"- {gate}")
    else:
        if active_count:
            print(
                f"NO_NEW_SIGNALS | {now} | active={active_count} | "
                f"checked={len(rows)} | warnings={len(warnings)} | "
                f"errors={len(errors)}"
            )
            print(
                "说明：当前仍有标的处于触发区，但没有比上次进入更深档位；"
                "使用 --repeat-active 可重复显示。"
            )
        else:
            print(
                f"NO_ACTIVE_SIGNALS | {now} | checked={len(rows)} | "
                f"warnings={len(warnings)} | errors={len(errors)}"
            )

    if args.show_all:
        print("\n".join(render_dashboard(rows)))
        if any(row["metric_gate"]["status"] == "unknown" for row in rows):
            print("\n指标说明：")
            print(
                "- “数据不可用/缺少”表示自动来源失败、过期或未配置，"
                "不表示“待补仓”。"
            )
            print(
                "- 可使用 --metrics-file 覆盖自动数据；价格触发且指标"
                "核验通过，才进入人工复核。"
            )
    if errors:
        print("\n数据错误：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
    if warnings and args.show_all:
        print("\n数据警告：", file=sys.stderr)
        for warning in warnings:
            print(f"- {warning}", file=sys.stderr)
    return 1 if not rows else 0


def command_list(args: argparse.Namespace) -> int:
    config = load_json(args.config)
    method = config.get("selection_method", {})
    print(f"筛选方法：{method.get('name', '未配置')}")
    if method.get("scope_note"):
        print(f"口径：{method['scope_note']}")

    print("\n行动观察池：")
    print("| 标的 | 代码 | 币种 | 价格档 | 回撤档 | 需额外指标 |")
    print("|---|---|---|---:|---:|---|")
    for item in config.get("watchlist", []):
        print(
            f"| {item['name']} | {item['symbol']} | {item.get('currency', '-')} | "
            f"{len(item.get('price_levels', []))} | "
            f"{len(item.get('drawdown_levels', []))} | "
            f"{'有' if item.get('metric_gate_groups') else '-'} |"
        )

    research_pool = config.get("research_pool", [])
    if research_pool:
        print("\n研究候选池（不触发价格告警）：")
        print("| 标的 | 代码 | 角色 | 状态 |")
        print("|---|---|---|---|")
        for item in research_pool:
            print(
                f"| {item['name']} | {item['symbol']} | "
                f"{item.get('role', '-')} | {item.get('status', '-')} |"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor investment trigger levels.")
    subparsers = parser.add_subparsers(dest="command")
    check = subparsers.add_parser("check", help="Fetch prices and evaluate triggers.")
    check.add_argument("--config", default=DEFAULT_CONFIG)
    check.add_argument("--state", default=DEFAULT_STATE)
    check.add_argument("--prices-file", help="Optional offline snapshot JSON.")
    check.add_argument(
        "--metrics-file",
        help="Optional JSON overrides for automatically fetched metrics.",
    )
    check.add_argument(
        "--no-auto-metrics",
        action="store_true",
        help="Do not fetch configured valuation, NAV, or subscription metrics.",
    )
    check.add_argument("--show-all", action="store_true")
    check.add_argument("--repeat-active", action="store_true")
    check.add_argument("--no-state", action="store_true")
    list_command = subparsers.add_parser(
        "list",
        help="Show action and research watchlists without fetching prices.",
    )
    list_command.add_argument("--config", default=DEFAULT_CONFIG)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in {None, "check"}:
        if args.command is None:
            args = parser.parse_args(["check"])
        return command_check(args)
    if args.command == "list":
        return command_list(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
