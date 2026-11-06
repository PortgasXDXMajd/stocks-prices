"""Daily pick run, right after the US open: ask Claude (with web search) for up to 3
news-driven picks, validate, store. Entry = price at the moment of the pick (filled by
app.prices from 1-minute bars; app.daily runs both).

Paper experiment only: nothing here (or anywhere in this app) places orders.

Usage:  python -m app.picker          (env: ANTHROPIC_API_KEY, MODEL, MAX_SEARCHES, EFFORT, FORCE)

API notes (checked against Anthropic's docs on 2026-11-05):
- Web search server tool: newest version is "web_search_20260209" (dynamic filtering),
  supported on claude-opus-5-5 (and Opus 4.6+, Sonnet 4.6+). Older models only take
  "web_search_20250305"; we fall back to it automatically if the API rejects the new one.
  No beta header is needed for web search.
- Reasoning effort: `output_config: {"effort": "low"|"medium"|"high"|"xhigh"|"max"}` (GA, no
  beta header). On claude-opus-5-5 the default is "medium", so we send EFFORT (default "high").
  Thinking is always on for claude-opus-5-5 (adaptive); `{"type": "disabled"}` or a
  `budget_tokens` budget return 400. We send `thinking: {"type": "adaptive"}` explicitly so
  older models (Opus 4.6-4.8) also think. Thinking tokens count toward max_tokens, which is why
  MAX_TOKENS is 16000 rather than the 8000 in the build prompt: 8000 risks the answer being cut
  off before the final JSON at effort "high".
- pause_turn: the server-side tool loop can pause (stop_reason "pause_turn"). To resume, re-send
  the original user message plus the assistant content returned so far, unchanged (thinking
  blocks included), with NO extra "continue" user message; the server resumes automatically.
- Refusals: Opus 5.5 safety classifiers can decline (HTTP 200, stop_reason "refusal"). The docs
  recommend opting into server-side fallback: `"fallbacks": "default"` with the beta header
  `anthropic-beta: server-side-fallback-2026-07-01`. We send it, and drop it automatically if
  the API rejects it (e.g. a MODEL override that does not support it).
- anthropic-version stays "2023-06-01" (still accepted; we send no sampling parameters, which
  is the only part of the newer 2026-10-21 version that would matter here).
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable

from app import db

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_SEARCHES = 10
DEFAULT_EFFORT = "high"
MAX_TOKENS = 16000
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_SEARCH_TOOL_BASIC = "web_search_20250305"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TURNS = 6  # pause_turn continuations, total requests per picking pass
MAX_ATTEMPTS = 5  # per request, for 429 / 5xx / 529 / network errors
REQUEST_TIMEOUT = 900  # seconds; web-search turns can take minutes
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/coins/markets?"
    "vs_currency=usd&order=market_cap_desc&per_page=250&page=1&sparkline=false"
)
MAX_REASON = 200
MIN_STOCK_PRICE = 1.0

# Yahoo `exchangeName` codes of the allowed US exchanges (section 1: Nasdaq, NYSE, NYSE
# American, NYSE Arca, Cboe). Checked live on 2026-11-05: AAPL NMS, QQQ NGM, MARA NCM,
# ANET NYQ, UEC ASE, SPY PCX, ARKK BTS; OTC names report PNK/OID/OQX/OQB (e.g. TCEHY PNK,
# NSRGY OID). Allowlist, not blocklist: anything else (OTC, foreign venues, unknown) is
# rejected, so OTC/pink sheets and foreign-only listings can never slip through.
US_EXCHANGES = {
    "NMS", "NGM", "NCM", "NAS", "NASDAQ",  # Nasdaq Global Select / Global / Capital Market
    "NYQ", "NYS", "NYSE",                   # NYSE
    "ASE", "AMEX",                          # NYSE American
    "PCX", "ARCA",                          # NYSE Arca
    "BTS", "BATS", "CBOE", "BZX",           # Cboe BZX
}
# Picks only while the US regular session is open (New York time): the entry is the live
# price at pick time, which needs a trading market.
PICK_START = dtime(9, 30)
PICK_END = dtime(16, 0)

STABLECOINS = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDS", "USDD", "FRAX",
    "BUSD", "GUSD", "USDP", "RLUSD", "USD1", "EURC", "EURS", "EURT", "USDX", "USD0",
    "USDY", "USDB", "LUSD", "SUSD", "CRVUSD", "GHO", "USDG", "USDF", "USDA", "USDL",
    "FXUSD", "MIM", "DOLA", "BOLD", "USDTB", "SUSDE", "SUSDS", "SDAI", "AUSD", "USR",
    "DEUSD", "USYC", "BUIDL", "USTB", "OUSG", "FRXUSD", "USDO", "USDQ", "EURQ", "EURCV",
    "XSGD", "BRZ", "TRYB", "UST", "USTC", "HUSD", "CUSD", "CEUR", "ZUSD", "USDK",
}
_STABLE_PATTERN = re.compile(r"^(USD|EUR)|USD$")
_STOCK_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
_CRYPTO_RE = re.compile(r"^[A-Z0-9]{1,15}$")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class ClaudeError(RuntimeError):
    """The API call failed (after retries). The message never contains key material."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- guard


def _berlin(now: datetime | None) -> datetime:
    if now is None:
        return db.berlin_now()
    if now.tzinfo is None:
        return now.replace(tzinfo=db.BERLIN)
    return now.astimezone(db.BERLIN)


def guard(conn: sqlite3.Connection, now_berlin: datetime, force: bool) -> str | None:
    """Return why this run must not pick (caller prints it, exits 0), or None to go on."""
    now_berlin = _berlin(now_berlin)
    today = now_berlin.date()
    if today.weekday() >= 5:
        return f"{today}: weekend ({_WEEKDAYS[today.weekday()]}), no picks."
    last = db.get_config(conn, "last_pick_date")
    if today.isoformat() > last:
        return f"{today}: pick window closed (last pick date {last}), no picks."
    run_id = f"{today.isoformat()}-picks"
    if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone():
        return f"{today}: runs row {run_id} already exists (one picking pass per day), no picks."
    ny = now_berlin.astimezone(db.NEW_YORK).time().replace(tzinfo=None)
    if not (PICK_START <= ny < PICK_END) and not force:
        return (
            f"{today}: New York time {ny:%H:%M} is outside the US session "
            f"({PICK_START:%H:%M}-{PICK_END:%H:%M}); no picks. Set FORCE=1 only for testing."
        )
    return None


def market_open_now(now_utc: datetime) -> bool:
    """True if QQQ traded in the last few minutes (catches US holidays). Network boundary."""
    from app import prices

    return db.STOCK_BENCH in prices.fetch_minute_prices([db.STOCK_BENCH], now_utc)


def excluded_symbols(conn: sqlite3.Connection, today: date) -> list[str]:
    """Symbols picked in the last 28 calendar days (~20 US trading days), upper case."""
    days = int(db.get_config(conn, "exclusion_window_calendar_days"))
    since = (today - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT UPPER(symbol) AS s FROM picks WHERE pick_date >= ? AND pick_date <= ?",
        (since, today.isoformat()),
    ).fetchall()
    return sorted(r["s"] for r in rows)


# --------------------------------------------------------------------------- prompts


def build_system_prompt() -> str:
    return """You pick assets for a PAPER-TRADING experiment. No money is traded and no orders are placed; the picks are only recorded and scored afterwards.

THE EXPERIMENT
Question: do the names you pick from the news, right after the US market opens, go up over the next 1, 5 and 20 US trading days, and do they beat their benchmark?
- You are called shortly after the US regular session opens (09:30 New York). Each pick is entered at its live price in the minute you give your answer - not at the open, not at yesterday's close.
- Results: the official close 1, 5 and 20 US trading days after today, compared with the entry price. Stocks/ETFs are compared with QQQ, crypto with BTC-USD, over the same span. Round-trip costs (0.5% stocks, 1.0% crypto) are recorded too.
- So a pick needs a reason to keep rising from TODAY'S CURRENT PRICE over the coming days to weeks. News that the market already fully priced in (e.g. a stock that gapped up 15% at the open with nothing left to discover) is not an edge; check today's opening move before picking.

RESEARCH (use web search; prefer primary sources and reputable financial news from the last ~24 hours)
Look for fresh, specific catalysts, for example:
- earnings and guidance (beats/misses, raised/cut guidance, after-hours and pre-market moves);
- FDA and other regulatory decisions, court rulings;
- M&A, takeover bids, activist stakes, spin-offs;
- major contracts, partnerships, product launches;
- index inclusion/removal;
- analyst upgrades/downgrades and price-target changes from notable firms;
- unusual volume, pre-market or opening moves with an identifiable cause;
- crypto: exchange listings, ETF news/flows, protocol upgrades, token unlocks, hacks, regulatory actions.
Check the facts you rely on (numbers, dates, that the event really happened today/overnight). Prefer catalysts with documented follow-through tendencies (e.g. post-earnings-announcement drift after a beat-and-raise) over one-off hype.

ALLOWED
- Stocks and ETFs listed on a US exchange (Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe) whose last close is above $1.
- Crypto that is in the top 250 by market cap and is NOT a stablecoin.

NOT ALLOWED (such picks are rejected automatically)
- OTC / pink-sheet securities and foreign-only listings (no US exchange listing). ADRs listed on a US exchange are fine.
- Coins outside the top 250 by market cap, and any stablecoin (USDT, USDC, DAI, USDe, FDUSD, PYUSD, etc.) or other fiat-pegged token.
- Any symbol on the EXCLUDED list given in the user message (picked in the last ~20 US trading days).
- The same symbol twice.

TICKERS - exact symbols, because they are looked up automatically:
- Stocks/ETFs: the US exchange ticker, uppercase, class shares with a dot, e.g. AAPL, BRK.B, QQQM. No exchange prefixes or suffixes (not "NASDAQ:AAPL", not "7203.T").
- Crypto: the plain coin symbol, uppercase, e.g. SOL, ETH, LINK (not "SOL-USD", not "SOLUSDT").
- "type" is exactly "stock" (also for ETFs) or "crypto".

HOW MANY
- At most 3 picks in total (stocks and/or crypto). Zero is allowed, and zero is BETTER than a weak pick: only pick when you see a concrete, news-driven reason to expect the name to rise and beat its benchmark from the current price over the next days to weeks.
- Each "reason" is at most 200 characters: the specific catalyst with key numbers and why you expect outperformance. "news_url" is the single best source URL for that catalyst.

OUTPUT
Do your research first. Then finish your answer with ONLY this JSON object as the very last thing you write (no text after it):
{"picks":[{"symbol":"AMD","name":"Advanced Micro Devices","type":"stock","reason":"...","news_url":"https://..."}],"summary":"one or two sentences on today's market news and why you picked what you picked (or nothing)"}
For no picks, use {"picks":[],"summary":"..."}."""


def build_user_prompt(now_berlin: datetime, excluded: list[str], max_searches: int) -> str:
    now_berlin = _berlin(now_berlin)
    now_ny = now_berlin.astimezone(db.NEW_YORK)
    excl = ", ".join(excluded) if excluded else "(none)"
    return (
        f"Current time: {_WEEKDAYS[now_berlin.weekday()]} {now_berlin:%Y-%m-%d %H:%M} Berlin "
        f"({now_berlin:%Z}), which is {now_ny:%Y-%m-%d %H:%M} in New York.\n"
        "The US regular session opened at 09:30 New York time. Every pick (stock or crypto) "
        "is entered at its live price in the minute you finish your answer.\n"
        f"You have up to {max_searches} web searches.\n"
        f"EXCLUDED symbols (picked in the last 28 calendar days, do not pick): {excl}\n\n"
        "Research the overnight and this morning's news (including how the names traded at "
        "the open) and give today's picks (0 to 3), ending with the JSON object."
    )


# --------------------------------------------------------------------------- Claude API


def _post(body: dict, api_key: str, betas: list[str]) -> dict:
    """One POST with retries for 429/5xx/529 and network errors. Never logs headers or key."""
    data = json.dumps(body).encode("utf-8")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    last_err = "unknown error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
        retry_after: float | None = None
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                payload = json.loads(e.read().decode("utf-8", "replace"))
                err = payload.get("error") or {}
                detail = f"{err.get('type', 'error')}: {err.get('message', '')}".strip()
            except Exception:
                detail = "no error body"
            last_err = f"HTTP {status} {detail}"
            if status not in RETRY_STATUSES:
                raise ClaudeError(last_err, status) from None
            ra = e.headers.get("retry-after") if e.headers else None
            try:
                retry_after = float(ra) if ra else None
            except ValueError:
                retry_after = None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            reason = getattr(e, "reason", None) or e.__class__.__name__
            last_err = f"network error: {reason}"
        except json.JSONDecodeError:
            last_err = "response was not valid JSON"
        if attempt == MAX_ATTEMPTS:
            break
        delay = retry_after if retry_after is not None else min(5 * 2 ** (attempt - 1), 120)
        delay = min(delay, 300) + random.uniform(0, 2)
        print(f"Anthropic API: {last_err}; retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.0f}s",
              file=sys.stderr)
        time.sleep(delay)
    raise ClaudeError(f"giving up after {MAX_ATTEMPTS} attempts: {last_err}")


def _texts(content: list[dict]) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def call_claude(
    system: str,
    user: str,
    *,
    api_key: str,
    model: str,
    max_searches: int,
    effort: str | None,
) -> dict:
    """Run one picking conversation (web search, pause_turn continuations up to MAX_TURNS).

    Returns {"text", "input_tokens", "output_tokens", "searches", "stop_reason"} plus
    "all_text" (text of every turn), "model" (model that answered) and "api_notes"
    (automatic downgrades applied, if any). Raises ClaudeError on failure.
    """
    use_fallbacks = True
    use_thinking = True
    tool_type = WEB_SEARCH_TOOL
    send_effort = effort
    api_notes: list[str] = []

    messages: list[dict] = [{"role": "user", "content": user}]
    assistant_content: list[dict] = []
    in_tok = out_tok = searches = 0
    counted_blocks = 0
    saw_usage_searches = False
    texts: list[str] = []
    final_text = ""
    stop_reason = None
    answered_by = model
    turns = 0
    downgrades_left = 4

    while turns < MAX_TURNS:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages + ([{"role": "assistant", "content": assistant_content}]
                                    if assistant_content else []),
            "tools": [{"type": tool_type, "name": "web_search", "max_uses": max_searches}],
        }
        if use_thinking:
            body["thinking"] = {"type": "adaptive"}
        if send_effort:
            body["output_config"] = {"effort": send_effort}
        betas = []
        if use_fallbacks:
            body["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        try:
            resp = _post(body, api_key, betas)
        except ClaudeError as e:
            # Degrade gracefully when a MODEL override rejects a newer feature (400 only).
            msg = str(e).lower()
            if e.status == 400 and downgrades_left > 0:
                downgrades_left -= 1
                if use_fallbacks and "fallback" in msg:
                    use_fallbacks = False
                    api_notes.append("server-side fallbacks not accepted; sent without")
                    continue
                if tool_type == WEB_SEARCH_TOOL and WEB_SEARCH_TOOL in msg:
                    tool_type = WEB_SEARCH_TOOL_BASIC
                    api_notes.append(f"{WEB_SEARCH_TOOL} not accepted; used {tool_type}")
                    continue
                if send_effort and ("effort" in msg or "output_config" in msg):
                    send_effort = None
                    api_notes.append("effort not accepted; sent without")
                    continue
                if use_thinking and "thinking" in msg:
                    use_thinking = False
                    api_notes.append("adaptive thinking not accepted; sent without")
                    continue
            raise
        turns += 1
        content = resp.get("content") or []
        usage = resp.get("usage") or {}
        in_tok += int(usage.get("input_tokens") or 0)
        in_tok += int(usage.get("cache_creation_input_tokens") or 0)
        in_tok += int(usage.get("cache_read_input_tokens") or 0)
        out_tok += int(usage.get("output_tokens") or 0)
        stu = usage.get("server_tool_use") or {}
        if "web_search_requests" in stu:
            saw_usage_searches = True
            searches += int(stu.get("web_search_requests") or 0)
        counted_blocks += sum(
            1 for b in content
            if b.get("type") == "server_tool_use" and b.get("name") == "web_search"
        )
        answered_by = resp.get("model") or answered_by
        stop_reason = resp.get("stop_reason")
        final_text = _texts(content)
        texts.append(final_text)
        if stop_reason != "pause_turn":
            break
        # Resume: send back everything the assistant produced so far, unchanged.
        assistant_content = assistant_content + content

    return {
        "text": final_text,
        "all_text": "\n".join(t for t in texts if t),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "searches": searches if saw_usage_searches else counted_blocks,
        "stop_reason": stop_reason,
        "model": answered_by,
        "api_notes": api_notes,
    }


# --------------------------------------------------------------------------- parsing


def _last_object_with_picks(text: str) -> tuple[int, dict] | None:
    """(distance of its start from the end of text, object) for the last {..."picks"...}."""
    dec = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"\{", text)]
    for i in reversed(starts):
        try:
            obj, _ = dec.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict) and "picks" in obj:
            return len(text) - i, obj
    return None


def parse_picks(text: str) -> dict:
    """Extract the last JSON object in `text` that has a "picks" key. Raises ValueError.

    Handles code fences, nested braces and prose before/after the JSON. A second, lenient
    pass tolerates trailing commas; whichever pass finds the later object wins.
    """
    if not text or not text.strip():
        raise ValueError("empty response text")
    found = [_last_object_with_picks(text)]
    lenient = re.sub(r",(\s*[}\]])", r"\1", text)
    if lenient != text:
        found.append(_last_object_with_picks(lenient))
    found = [f for f in found if f is not None]
    if not found:
        raise ValueError('no JSON object with a "picks" key found in the response')
    obj = min(found, key=lambda f: f[0])[1]
    if not isinstance(obj.get("picks"), list):
        raise ValueError('"picks" is not a list')
    return obj


# --------------------------------------------------------------------------- validation


# exchangeName seen by yahoo_last_close, so yahoo_exchange needs no second request.
_EXCHANGE_CACHE: dict[str, str | None] = {}


def _exchange_from(ticker) -> str | None:
    try:
        md = ticker.history_metadata or {}
    except Exception:
        return None
    code = str(md.get("exchangeName") or "").strip().upper()
    return code or None


def yahoo_last_close(yahoo_sym: str) -> float | None:
    """Last daily close from Yahoo via yfinance, or None if there is no history."""
    try:
        import logging

        import yfinance as yf

        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        ticker = yf.Ticker(yahoo_sym)
        hist = ticker.history(period="1mo", interval="1d", auto_adjust=False)
        _EXCHANGE_CACHE[yahoo_sym] = _exchange_from(ticker)
        if hist is None or hist.empty or "Close" not in hist:
            return None
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1])
    except Exception:
        return None


def yahoo_exchange(yahoo_sym: str) -> str | None:
    """Yahoo's listing-venue code (history_metadata exchangeName, e.g. NMS, NYQ, PNK),
    or None if unknown. Network boundary (tests mock it); reuses yahoo_last_close's lookup.
    """
    cached = _EXCHANGE_CACHE.get(yahoo_sym)
    if cached:
        return cached
    try:
        import logging

        import yfinance as yf

        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        ticker = yf.Ticker(yahoo_sym)
        ticker.history(period="5d", interval="1d", auto_adjust=False)
        code = _exchange_from(ticker)
    except Exception:
        return None
    _EXCHANGE_CACHE[yahoo_sym] = code
    return code


def coingecko_top_symbols() -> set[str] | None:
    """Upper-case symbols of the top 250 coins by market cap, or None if unreachable."""
    req = urllib.request.Request(
        COINGECKO_URL,
        headers={"accept": "application/json", "user-agent": "morning-picks/1.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            syms = {str(c.get("symbol", "")).upper() for c in data if isinstance(c, dict)}
            syms.discard("")
            return syms or None
        except Exception:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    return None


def _norm_symbol(raw: Any, type_: str) -> str:
    s = str(raw or "").strip().upper().lstrip("$")
    if ":" in s:  # "NASDAQ:AAPL" -> "AAPL"
        s = s.split(":")[-1].strip()
    if type_ == "crypto":
        for suffix in ("-USD", "/USD", "USDT"):
            if s.endswith(suffix) and len(s) > len(suffix) and s not in STABLECOINS:
                s = s[: -len(suffix)]
                break
    else:
        s = s.replace("/", ".")
        m = re.fullmatch(r"([A-Z]{1,5})-([A-Z]{1,2})", s)  # BRK-B -> BRK.B
        if m:
            s = f"{m.group(1)}.{m.group(2)}"
    return s


def is_stablecoin(sym: str) -> bool:
    return sym in STABLECOINS or bool(_STABLE_PATTERN.search(sym))


def validate_pick(
    p: dict,
    excluded: set[str],
    *,
    seen: set[str] | None = None,
    top_crypto: set[str] | None = None,
    last_close: Callable[[str], float | None] | None = None,
    exchange: Callable[[str], str | None] | None = None,
) -> tuple[dict | None, str | None]:
    """Check one pick from the model. Returns (row_dict, None) or (None, reason).

    seen: symbols already taken in this response (duplicates are rejected).
    top_crypto: top-250 coin symbols; None means unknown (CoinGecko unreachable) -> not checked.
    last_close: price lookup; defaults to yahoo_last_close (looked up at call time, so mocks work).
    exchange: listing-venue lookup for stocks; defaults to yahoo_exchange (same). Stocks must
      be listed on a US exchange (US_EXCHANGES); OTC, foreign or unknown venues are rejected.
    """
    if not isinstance(p, dict):
        return None, "pick is not a JSON object"
    type_ = str(p.get("type") or "").strip().lower()
    if type_ not in ("stock", "crypto"):
        return None, f"type {p.get('type')!r} is not 'stock' or 'crypto'"
    sym = _norm_symbol(p.get("symbol"), type_)
    if not sym:
        return None, "missing symbol"
    if type_ == "stock" and not _STOCK_RE.match(sym):
        return None, f"{sym}: not a plain US ticker"
    if type_ == "crypto" and not _CRYPTO_RE.match(sym):
        return None, f"{sym}: not a plain coin symbol"
    if sym in {e.upper() for e in excluded}:
        return None, f"{sym}: excluded (picked in the last 28 days)"
    if seen is not None and sym in seen:
        return None, f"{sym}: duplicate in this response"
    if type_ == "crypto":
        if is_stablecoin(sym):
            return None, f"{sym}: stablecoin"
        if top_crypto is not None and sym not in top_crypto:
            return None, f"{sym}: not in CoinGecko top 250 by market cap"
    ysym = db.yahoo_symbol(sym, type_)
    lookup = last_close or yahoo_last_close
    close = lookup(ysym)
    if close is None:
        return None, f"{sym}: no Yahoo price history for {ysym}"
    if type_ == "stock":
        venue = (exchange or yahoo_exchange)(ysym)
        if not venue:
            return None, f"{sym}: listing venue unknown (Yahoo gave no exchange), not verifiable as US-listed"
        if venue.upper() not in US_EXCHANGES:
            return None, f"{sym}: listed on {venue}, not a US exchange (OTC/foreign not allowed)"
        if close < MIN_STOCK_PRICE:
            return None, f"{sym}: last close ${close:.2f} is below $1"
    reason = " ".join(str(p.get("reason") or "").split())[:MAX_REASON]
    name = " ".join(str(p.get("name") or "").split())[:120] or None
    url = str(p.get("news_url") or "").strip()
    url = url if url.lower().startswith(("http://", "https://")) else None
    return {
        "symbol": sym,
        "yahoo_symbol": ysym,
        "name": name,
        "type": type_,
        "reason": reason,
        "news_url": url,
        "bench": db.bench_for(type_),
        "cost_pct": db.cost_for(type_),
        "last_close": close,
    }, None


# --------------------------------------------------------------------------- main


def _env_int(name: str, default: int) -> int:
    try:
        v = int((os.environ.get(name) or "").strip())
        return v if v > 0 else default
    except ValueError:
        return default


def _insert_run(conn, run_id, today, started, added, summary, issues, res) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (id, date, kind, started_utc, picks_added, rows_updated, "
        "summary, issues, input_tokens, output_tokens, searches) "
        "VALUES (?, ?, 'picks', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, today, started, added, added, summary,
            "; ".join(issues) if issues else "",
            res.get("input_tokens"), res.get("output_tokens"), res.get("searches"),
        ),
    )


def main(now_berlin: datetime | None = None) -> int:
    """now_berlin is for tests: it fixes both the guard time and the pick time."""
    fixed_now = now_berlin is not None
    now_berlin = _berlin(now_berlin)
    started = db.utc_iso()
    force = (os.environ.get("FORCE") or "").strip() == "1"
    conn = db.connect()
    try:
        stop = guard(conn, now_berlin, force)
        if stop:
            print(stop)
            return 0
        if not force and not market_open_now(now_berlin.astimezone(timezone.utc)):
            print(f"{now_berlin.date()}: no QQQ trades in the last minutes (US market closed, "
                  "e.g. a holiday, or Yahoo down); no picks. A later run today can retry.")
            return 0

        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY is not set; cannot call the Anthropic API.",
                  file=sys.stderr)
            return 1
        model = (os.environ.get("MODEL") or "").strip() or DEFAULT_MODEL
        max_searches = _env_int("MAX_SEARCHES", DEFAULT_MAX_SEARCHES)
        effort_env = os.environ.get("EFFORT")
        effort = (effort_env or "").strip().lower() or DEFAULT_EFFORT
        if effort in ("none", "off", "default"):
            effort = None

        today = now_berlin.date()
        today_s = today.isoformat()
        run_id = f"{today_s}-picks"
        excluded = excluded_symbols(conn, today)
        max_picks = int(db.get_config(conn, "max_picks_per_day"))

        try:
            res = call_claude(
                build_system_prompt(),
                build_user_prompt(now_berlin, excluded, max_searches),
                api_key=api_key, model=model, max_searches=max_searches, effort=effort,
            )
        except Exception as e:  # noqa: BLE001 - any failure: no runs row, manual re-run possible
            msg = str(e).replace(api_key, "***") if api_key else str(e)
            print(f"ERROR: Anthropic API call failed: {msg}", file=sys.stderr)
            return 1
        picked_at = now_berlin.astimezone(timezone.utc) if fixed_now else db.utc_now()

        issues: list[str] = list(res.get("api_notes") or [])
        stop_reason = res.get("stop_reason")
        if stop_reason not in (None, "end_turn"):
            issues.append(f"stop_reason={stop_reason}")
        answered_by = res.get("model") or model
        model_label = model if answered_by in (None, model) else f"{model} (served by {answered_by})"

        summary = ""
        raw_picks: list = []
        try:
            try:
                parsed = parse_picks(res.get("text") or "")
            except ValueError:
                parsed = parse_picks(res.get("all_text") or "")
            raw_picks = parsed.get("picks") or []
            summary = " ".join(str(parsed.get("summary") or "").split())[:1000]
        except ValueError as e:
            issues.append(f"parse error: {e}")

        top_crypto: set[str] | None = None
        if any(isinstance(p, dict) and str(p.get("type", "")).strip().lower() == "crypto"
               for p in raw_picks):
            top_crypto = coingecko_top_symbols()
            if top_crypto is None:
                issues.append("CoinGecko unreachable: top-250 check skipped")

        excl_set = set(excluded)
        seen: set[str] = set()
        accepted: list[dict] = []
        for p in raw_picks:
            label = str(p.get("symbol") if isinstance(p, dict) else p)
            if len(accepted) >= max_picks:
                issues.append(f"rejected {label}: over the {max_picks}-pick limit")
                continue
            row, why = validate_pick(p, excl_set, seen=seen, top_crypto=top_crypto)
            if isinstance(p, dict):
                t = str(p.get("type") or "").strip().lower()
                seen.add(_norm_symbol(p.get("symbol"), t if t in ("stock", "crypto") else "stock"))
            if row is None:
                issues.append(f"rejected {label}: {re.sub(r'^[A-Z0-9.]+: ', '', why or '')}")
                continue
            accepted.append(row)

        # Pick time = when Claude's answer arrived (validation lookups come after, but they
        # only check listings; the entry is priced at this moment, not later).
        pick_utc = picked_at or db.utc_now()
        run_utc = db.utc_iso(pick_utc)
        entry_date = pick_utc.astimezone(db.NEW_YORK).date().isoformat()
        added = 0
        for r in accepted:
            cur = conn.execute(
                "INSERT OR IGNORE INTO picks (id, pick_date, run_utc, symbol, yahoo_symbol, name, "
                "type, reason, news_url, bench, cost_pct, status, notes, model, entry_date, "
                "entry_rule) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_entry', '', ?, ?, ?)",
                (
                    db.pick_id(today_s, r["symbol"]), today_s, run_utc, r["symbol"],
                    r["yahoo_symbol"], r["name"], r["type"], r["reason"], r["news_url"],
                    r["bench"], r["cost_pct"], model_label, entry_date, db.ENTRY_PICK_TIME,
                ),
            )
            added += cur.rowcount

        _insert_run(conn, run_id, today_s, started, added, summary, issues, res)
        conn.commit()

        # Refresh REPORT.md so the commit already shows today's picks.
        try:
            from app import report

            report.write_report(conn)
        except Exception as e:  # noqa: BLE001 - the picks are stored; a report error is minor
            print(f"WARNING: could not write the report: {type(e).__name__}: {e}",
                  file=sys.stderr)

        names = ", ".join(f"{r['symbol']} ({r['type']})" for r in accepted) or "nothing"
        rejected = sum(1 for i in issues if i.startswith("rejected "))
        print(
            f"{today_s} picks: {names} | rejected {rejected} | model {answered_by} | "
            f"tokens in/out {res.get('input_tokens')}/{res.get('output_tokens')} | "
            f"searches {res.get('searches')}"
            + (f" | issues: {len(issues)}" if issues else "")
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
