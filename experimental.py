"""
EXPERIMENTAL PAGE  (3x leveraged ETFs)
======================================
Writes data/backtest_experimental.json, which the dashboard shows at
index.html?data=experimental, in the same format as the other backtest pages.

Groups
  Reversal @ time : bets the move so far flips.
                    Up so far -> buy the 3x BEAR ETF; down so far -> buy the 3x BULL ETF.
  Momentum @ time : the mirror image: bets the move so far keeps going.
      Two entry times:
        9:30  "move so far" = the overnight gap (yesterday's close -> today's open)
        10:30 "move so far" = the first hour (9:30 open -> 10:30)
      Three models each, with targets on the SPY / QQQ move (stop = half):
          Conservative +0.25%   Moderate +0.40%   Aggressive +0.60%
      The 3x ETF moves about 3 times that. Out by target, stop or 3:55 PM.
  Ladder 5% : $100 of the 3x bull ETF when SPY/QQQ closes 5% under its yearly high,
              another $100 at 10% under, 15%, ...
  Ladder 3% : the same with 3% steps: 3%, 6%, 9%, ...
      Both lock in the yearly high at the first buy and sell EVERY rung once SPY/QQQ
      gets back to 1% under that high. No stop, buy-only.
  All dip rules look at SPY / QQQ themselves, never at the leveraged ETF.

Pricing (same method as the other 3x pages)
  Day trades: bought at the real 3x ETF price at the entry minute, exits found
  minute by minute on SPY/QQQ, ETF exit priced at 3x the SPY/QQQ move.
  Dip trades: real 3x ETF closing prices, so multi-week drift is included.
  Every buy and sell pays SLIPPAGE per share.

Runs (each one feeds its own dashboard tab):
    python experimental.py 2026-01-01 "" backtest_experimental.json              # this year, all groups
    python experimental.py 2021-10-01 "" backtest_experimental_5y.json daily     # last 5 years, ladders
    python experimental.py 2022-01-01 2023-12-29 backtest_experimental_down.json daily
          # the 2022-23 round trip: SPY/QQQ fell about 25-35%, then finished
          # roughly where they started
Arguments: START  END (blank = latest close)  OUTPUT FILE NAME in data/  [daily]
"daily" uses one open and close per day instead of minute data. That's all the
ladders need (they act on the close), so long runs take seconds. The day-trading
groups need minute data, so they're left out of daily runs.
"""

import json
import os
import sys
import time
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------- settings ---
START = "2026-01-01"
PAIRS = {"SPY": {"bull": "SPXL", "bear": "SPXS"}, "QQQ": {"bull": "TQQQ", "bear": "SQQQ"}}
LEV = 3
ENTRY_TIMES = ["09:30", "10:30"]                 # ET. 9:30 trades the overnight gap
TIERS = {"Conservative": 0.25, "Moderate": 0.40, "Aggressive": 0.60}   # % move of SPY/QQQ; stop = half
TRADE_DOLLARS = 100.0
SLIPPAGE = 0.005                # $ per share on every buy and every sell
EOD_TIME = "15:55"              # day trades closed here; dip bots check their rules here
LOOKBACK = 252                  # trading days in the "yearly high"
LADDERS = {"dip5": 0.05, "ladder3": 0.03}   # group key -> step between buys
SELL_BELOW_HIGH = 0.01          # sell all rungs at 1% under the high locked in at the first buy
LADDER_MAX_RUNGS = None         # None = no cap

DATA_DIR = Path(__file__).resolve().parent / "data"
TICKERS = list(PAIRS)
ALL_SYMBOLS = TICKERS + [s for p in PAIRS.values() for s in (p["bull"], p["bear"])]


def label(t):                    # "14:00" -> "2:00 PM"
    h, m = map(int, t.split(":"))
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def build_groups():
    rev_colors = ["#2B2722", "#4C7A5B", "#3F6E8C", "#6B4C7A"]
    mom_colors = ["#8C5A3C", "#9B5E7A", "#8A7A2E", "#5E8F99"]
    groups = []
    tier_models = [{"key": k, "rule": f"SPY/QQQ +{v:.2f}% / −{v / 2:.3g}% (3× ETF ≈ +{v * LEV:.2f}%)"} for k, v in TIERS.items()]
    for i, t in enumerate(ENTRY_TIMES):
        c = t.replace(":", "")
        what = "the overnight gap at the 9:30 open" if t == "09:30" else f"the move since the open at {label(t)} ET"
        groups.append({"key": f"rev{c}", "kind": "intraday", "fade": True, "time": t,
                       "name": f"Reversal {label(t)}", "color": rev_colors[i % 4], "models": tier_models,
                       "desc": f"Fades {what} · out by target, stop or 3:55"})
        groups.append({"key": f"mom{c}", "kind": "intraday", "fade": False, "time": t,
                       "name": f"Momentum {label(t)}", "color": mom_colors[i % 4], "models": tier_models,
                       "desc": f"Follows {what} · out by target, stop or 3:55"})
    for key, step, color in (("dip5", 0.05, "#B8875A"), ("ladder3", 0.03, "#7A6F9B")):
        st = f"{step * 100:.0f}%"
        groups.append({"key": key, "kind": "ladder", "step": step, "name": f"Ladder {st}", "color": color,
                       "models": [{"key": "$100 per rung", "rule": f"Buy $100 every {st} down from the yearly high · sell all at 1% under that high · no stop"}],
                       "desc": f"Adds $100 at every {st} drop from the yearly high, sells everything at 1% under it · holds for days"})
    return groups


GROUPS = build_groups()


# ------------------------------------------------------------- price tools ---
def price_at(bars, hhmm):
    """That minute's opening price; if the minute had no trades, the last close before it."""
    if not bars:
        return None
    if hhmm in bars:
        return bars[hhmm][0]
    earlier = [t for t in bars if t < hhmm]
    return bars[max(earlier)][3] if earlier else None


def after(bars, start, end):
    return [(t, bars[t]) for t in sorted(bars) if start <= t < end]


# ------------------------------------------------------------------ engine ---
class Engine:
    def __init__(self):
        self.trades, self.days, self.open = [], [], []
        self.ladder = {key: {tk: {"H": None} for tk in TICKERS} for key in LADDERS}
        self.risk = {key: {tk: {"max_in": 0.0, "worst_open": 0.0, "worst_date": None, "max_rungs": 0} for tk in TICKERS} for key in LADDERS}
        self.edge = {t: {tk: {"n": 0, "rev": 0} for tk in TICKERS} for t in ENTRY_TIMES}

    # --- helpers
    def _trade(self, date, tk, mode, model, etf, bull, entry_fill, exit_fill, und_in, und_out,
               reason, t_in, t_out, entry_date=None, exit_date=None, rung=None):
        shares = TRADE_DOLLARS / entry_fill
        rec = {"date": date, "ticker": tk, "mode": mode, "model": model,
               "option": "CALL" if bull else "PUT", "side": "long 3×" if bull else "inverse 3×",
               "contract": etf + (f" · rung {rung}" if rung else ""),
               "entry": round(entry_fill, 2), "exit": round(exit_fill, 2),
               "pnl_pct": round(exit_fill / entry_fill - 1, 5),
               "pnl_usd": round((exit_fill - entry_fill) * shares, 2),
               "etf_entry": round(und_in, 2), "etf_exit": round(und_out, 2),
               "etf_move": round(und_out / und_in - 1, 5), "reason": reason,
               "entry_time": t_in + ":00", "exit_time": t_out + ":00"}
        if entry_date:
            rec["entry_date"], rec["exit_date"] = entry_date, exit_date
        self.trades.append(rec)

    # --- day trades
    def intraday(self, day, bars, prev_close):
        for tk, pair in PAIRS.items():
            ub = bars.get(tk, {})
            # "Move so far": at 9:30 that's the overnight gap, later it's the move since the open.
            ref = lambda T: prev_close.get(tk) if T == "09:30" else price_at(ub, "09:30")
            for g in (g for g in GROUPS if g["kind"] == "intraday"):
                T = g["time"]
                p_open, p_in = ref(T), price_at(ub, T)
                if not p_open or not p_in or p_in == p_open:
                    self.days.append({"date": day, "ticker": tk, "mode": g["key"], "direction": "flat",
                                      "traded": False, "note": "Flat or missing data at the entry time, no trade"})
                    continue
                up = p_in > p_open
                bull = (not up) if g["fade"] else up
                d = 1 if bull else -1
                etf = pair["bull"] if bull else pair["bear"]
                etf_raw = price_at(bars.get(etf, {}), T)
                if not etf_raw:
                    continue
                entry_fill = etf_raw + SLIPPAGE
                for model, tp in TIERS.items():
                    tgt = p_in * (1 + d * tp / 100)
                    stp = p_in * (1 - d * tp / 200)
                    hit = None
                    for t, (o, h, l, c) in after(ub, T, EOD_TIME):
                        if t != T and d * (o - stp) <= 0: hit = (t, o, "stop"); break
                        if t != T and d * (o - tgt) >= 0: hit = (t, o, "target"); break
                        worst, best = (l, h) if bull else (h, l)
                        if d * (worst - stp) <= 0: hit = (t, stp, "stop"); break    # both in one minute: stop first
                        if d * (best - tgt) >= 0: hit = (t, tgt, "target"); break
                    if not hit:
                        hit = (EOD_TIME, price_at(ub, EOD_TIME), "end of day")
                    t_out, u_out, reason = hit
                    fav = d * (u_out / p_in - 1)
                    exit_fill = max(0.01, etf_raw * (1 + LEV * fav) - SLIPPAGE)
                    self._trade(day, tk, g["key"], model, etf, bull, entry_fill, exit_fill, p_in, u_out, reason, T, t_out)
                mv = (p_in / p_open - 1) * 100
                what = f"opened {'up' if up else 'down'} {abs(mv):.2f}% from yesterday's close" if T == "09:30" \
                    else f"{'up' if up else 'down'} {abs(mv):.2f}% by {label(T)}"
                self.days.append({"date": day, "ticker": tk, "mode": g["key"], "direction": "up" if up else "down", "traded": True,
                                  "note": f"{tk} {what}, bought {etf}, all models closed"})
            # raw edge check: did the rest of the day go the other way?
            p_close = price_at(ub, EOD_TIME)
            for T in ENTRY_TIMES:
                p_open, p_in = ref(T), price_at(ub, T)
                if p_open and p_in and p_close and p_in != p_open and p_close != p_in:
                    e = self.edge[T][tk]; e["n"] += 1
                    e["rev"] += (p_close > p_in) != (p_in > p_open)

    # --- dip bots
    def _buy_dip(self, mode, model, tk, day, etf_raw, u, rung=None):
        pos = {"mode": mode, "model": model, "ticker": tk, "etf": PAIRS[tk]["bull"], "entry_date": day,
               "entry_fill": etf_raw + SLIPPAGE, "und_in": u, "rung": rung}
        self.open.append(pos)
        return pos

    def _sell_dip(self, pos, day, etf_raw, u, reason):
        self._trade(day, pos["ticker"], pos["mode"], pos["model"], pos["etf"], True, pos["entry_fill"],
                    max(0.01, etf_raw - SLIPPAGE), pos["und_in"], u, reason, EOD_TIME, EOD_TIME,
                    pos["entry_date"], day, pos["rung"])
        self.open.remove(pos)

    def dips(self, day, bars, high52):
        for tk, pair in PAIRS.items():
            u, e, H52 = price_at(bars.get(tk, {}), EOD_TIME), price_at(bars.get(pair["bull"], {}), EOD_TIME), high52.get(tk)
            if not u or not e or not H52:
                continue
            under = (1 - u / H52) * 100
            for key, step in LADDERS.items():
                L, note, traded = self.ladder[key][tk], "", False
                rungs = [p for p in self.open if p["mode"] == key and p["ticker"] == tk]
                sell_at = L["H"] * (1 - SELL_BELOW_HIGH) if L["H"] else None
                if rungs and u >= sell_at:
                    for p in rungs:
                        self._sell_dip(p, day, e, u, "1% under high")
                    note, traded = f"Back to ${sell_at:.2f} (1% under the high), sold all {len(rungs)} rung{'s' if len(rungs) > 1 else ''}", True
                    self.ladder[key][tk] = {"H": None}
                else:
                    if not rungs and u <= H52 * (1 - step):
                        L["H"] = H52                       # lock in the high at the first buy
                    n0 = n = len(rungs)
                    while L["H"] and u <= L["H"] * (1 - step * (n + 1)) and not (LADDER_MAX_RUNGS and n >= LADDER_MAX_RUNGS):
                        n += 1
                        self._buy_dip(key, "$100 per rung", tk, day, e, u, rung=n)
                    if n > n0:
                        note, traded = f"{under:.1f}% under the high, bought rung{'s' if n - n0 > 1 else ''} {', '.join(map(str, range(n0 + 1, n + 1)))}", True
                    elif n:
                        note = (f"Holding {n} rung{'s' if n > 1 else ''} (${n * TRADE_DOLLARS:.0f}); next buy ${L['H'] * (1 - step * (n + 1)):.2f}, "
                                f"sells all at ${L['H'] * (1 - SELL_BELOW_HIGH):.2f}")
                    else:
                        note = (f"{under:.1f}% under its yearly high" if under > 0 else "Above its yearly high") + f"; first buy at {step * 100:.0f}% under"
                self.days.append({"date": day, "ticker": tk, "mode": key, "direction": "", "traded": traded, "note": note})
                # risk: money tied up and the worst paper loss along the way (at today's close)
                held = [p for p in self.open if p["mode"] == key and p["ticker"] == tk]
                r = self.risk[key][tk]
                r["max_in"] = max(r["max_in"], len(held) * TRADE_DOLLARS)
                r["max_rungs"] = max(r["max_rungs"], len(held))
                unreal = sum((e - SLIPPAGE - p["entry_fill"]) * TRADE_DOLLARS / p["entry_fill"] for p in held)
                if unreal < r["worst_open"]:
                    r["worst_open"], r["worst_date"] = round(unreal, 2), day

    def still_open(self, day, bars):
        """Anything the dip bots still hold is sold at the last close, so the totals show
        where everything stands right now. These trades are tagged open_at_end."""
        for p in list(self.open):
            e = price_at(bars.get(p["etf"], {}), EOD_TIME)
            u = price_at(bars.get(p["ticker"], {}), EOD_TIME)
            if e and u:
                self._sell_dip(p, day, e, u, "closed at run end")
                self.trades[-1]["open_at_end"] = True

    def edge_note(self):
        parts = []
        for T in ENTRY_TIMES:
            bits = [f"{tk} {100 * e['rev'] / e['n']:.0f}%" for tk, e in self.edge[T].items() if e["n"]]
            if bits:
                parts.append(f"{label(T)}: {' / '.join(bits)}")
        return ("Raw edge check, share of days the rest of the session went against the move so far (gap at 9:30, first hour at 10:30) "
                "(coin flip = 50%): " + "; ".join(parts)) if parts else ""


# -------------------------------------------------------------- data layer ---
def _keys():
    k = (os.getenv("ALPACA_KEY") or os.getenv("ALPACA_API_KEY") or "").strip()
    s = (os.getenv("ALPACA_SECRET") or os.getenv("ALPACA_SECRET_KEY") or "").strip()
    if not k or not s:
        sys.exit("ALPACA_KEY / ALPACA_SECRET are missing. Add them under Settings > Secrets and variables > Actions.")
    return k, s


class AlpacaData:
    def __init__(self):
        from alpaca.data.historical import StockHistoricalDataClient
        self.client = StockHistoricalDataClient(*_keys())
        self.feed = "sip"

    def _bars(self, symbols, start, end, timeframe):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.enums import Adjustment, DataFeed
        last = None
        for feed in ([self.feed, "iex"] if self.feed == "sip" else ["iex"]):
            for attempt in range(3):
                try:
                    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=timeframe, start=start, end=end,
                                           adjustment=Adjustment.SPLIT,
                                           feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX)
                    data = self.client.get_stock_bars(req).data
                    if feed != self.feed:
                        print(f"  SIP data unavailable, using IEX ({last})"); self.feed = feed
                    return data
                except Exception as err:
                    last = err; time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Could not download bars: {last}")

    def daily_closes(self, symbols, start, end):
        from alpaca.data.timeframe import TimeFrame
        data = self._bars(symbols, dt.datetime.combine(start, dt.time(), ET), dt.datetime.combine(end, dt.time(23, 59), ET), TimeFrame.Day)
        return {s: sorted((r.timestamp.astimezone(ET).date().isoformat(), float(r.close)) for r in rows) for s, rows in data.items()}

    def daily_bars(self, symbols, start, end):
        """{date: {symbol: {'09:30': open, '15:55': close}}} in the same shape as minute bars."""
        from alpaca.data.timeframe import TimeFrame
        data = self._bars(symbols, dt.datetime.combine(start, dt.time(), ET), dt.datetime.combine(end, dt.time(23, 59), ET), TimeFrame.Day)
        out = {}
        for sym, rows in data.items():
            for r in rows:
                d = r.timestamp.astimezone(ET).date().isoformat()
                o, c = float(r.open), float(r.close)
                out.setdefault(d, {})[sym] = {"09:30": (o, o, o, o), EOD_TIME: (c, c, c, c)}
        return out

    def minute_bars(self, symbols, days):
        from alpaca.data.timeframe import TimeFrame
        out = {d: {} for d in days}
        for mo in sorted({d[:7] for d in days}):
            md = [d for d in days if d.startswith(mo)]
            print(f"  minute bars {mo} ...")
            data = self._bars(symbols, dt.datetime.combine(dt.date.fromisoformat(md[0]), dt.time(9, 30), ET),
                              dt.datetime.combine(dt.date.fromisoformat(md[-1]), dt.time(16, 0), ET), TimeFrame.Minute)
            for sym, rows in data.items():
                for r in rows:
                    ts = r.timestamp.astimezone(ET)
                    d, hm = ts.date().isoformat(), ts.strftime("%H:%M")
                    if d in out and "09:30" <= hm < "16:00":
                        out[d].setdefault(sym, {})[hm] = (float(r.open), float(r.high), float(r.low), float(r.close))
        return out


def run(data, start, end=None, daily=False):
    """Replays START..END one month at a time (keeps memory low on long runs).
    daily=True: ladders only, from one open and close per day."""
    now = dt.datetime.now(ET)
    latest = now.date() if now.time() >= dt.time(16, 5) else now.date() - dt.timedelta(days=1)
    end = min(end or latest, latest)
    closes = data.daily_closes(TICKERS, start - dt.timedelta(days=400), end)
    days = [d for d, _ in closes.get("SPY", []) if start.isoformat() <= d <= end.isoformat()]
    print(f"{len(days)} trading days: {days[0]} .. {days[-1]}")
    eng = Engine()
    first_px, last_px, last_bars, day_bars = {}, {}, None, {}
    if daily:
        symbols = TICKERS + [p["bull"] for p in PAIRS.values()]
        all_daily = data.daily_bars(symbols, start, end)
    for mo in sorted({d[:7] for d in days}):
        md = [d for d in days if d.startswith(mo)]
        bars = {d: all_daily.get(d, {}) for d in md} if daily else data.minute_bars(ALL_SYMBOLS, md)
        for d in md:
            if not bars[d].get("SPY"):
                print(f"  {d}: no data, skipped"); continue
            high52, prev_close = {}, {}
            for tk in TICKERS:
                prior = [c for dd, c in closes.get(tk, []) if dd < d]
                high52[tk] = max(prior[-LOOKBACK:]) if len(prior) >= 20 else None
                prev_close[tk] = prior[-1] if prior else None
            if not daily:
                eng.intraday(d, bars[d], prev_close)
                from rsi_models import minute_closes
                day_bars[d] = {tk: minute_closes(bars[d].get(tk, {})) for tk in TICKERS}
            eng.dips(d, bars[d], high52)
            for tk, pair in PAIRS.items():          # for the buy-and-hold comparison
                for sym, t in ((tk, None), (pair["bull"], None)):
                    b = bars[d].get(sym, {})
                    if sym not in first_px and price_at(b, "09:30"):
                        first_px[sym] = price_at(b, "09:30")
                    if price_at(b, EOD_TIME):
                        last_px[sym] = price_at(b, EOD_TIME)
            last_bars, last_day = bars[d], d
    eng.still_open(last_day, last_bars)
    buy_hold = []
    for tk, pair in PAIRS.items():
        etf = pair["bull"]
        if first_px.get(etf) and last_px.get(etf):
            buy_hold.append({"ticker": tk, "etf": etf,
                             "und_ret": round(last_px[tk] / first_px[tk] - 1, 4),
                             "etf_ret": round((last_px[etf] - SLIPPAGE) / (first_px[etf] + SLIPPAGE) - 1, 4)})
    ladder_stats = []
    for key, by_tk in eng.risk.items():
        for tk, r in by_tk.items():
            pnl = sum(t["pnl_usd"] for t in eng.trades if t["mode"] == key and t["ticker"] == tk)
            ladder_stats.append({"mode": key, "ticker": tk, "max_in": r["max_in"], "max_rungs": r["max_rungs"],
                                 "worst_open": r["worst_open"], "worst_date": r["worst_date"], "pnl": round(pnl, 2)})
    bh = "; ".join(f"{b['ticker']} {b['und_ret'] * 100:+.1f}%, {b['etf']} {b['etf_ret'] * 100:+.1f}%" for b in buy_hold)
    meta = {
        "generated": dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "start": days[0], "end": last_day, "trading_days": len(days), "feed": data.feed,
        "note": f"{LEV}x ETFs, ${TRADE_DOLLARS:.0f} per buy, ${SLIPPAGE} per share each way for the spread. "
                f"Buy and hold over this period: {bh}. " +
                ("Ladders only, using daily closes (the day-trading groups need minute data)" if daily else eng.edge_note()),
        "groups": [{k: g[k] for k in ("key", "name", "desc", "color", "models")} for g in GROUPS
                   if not (daily and g["kind"] == "intraday")],
        "ladder_stats": ladder_stats, "buy_hold": buy_hold,
    }
    out = {"meta": meta, "trades": eng.trades, "days": eng.days, "errors": []}
    if day_bars:
        out["bars"] = day_bars
    return out


if __name__ == "__main__":
    args = sys.argv[1:] + ["", "", "", ""]
    start = dt.date.fromisoformat(args[0] or START)
    end = dt.date.fromisoformat(args[1]) if args[1] else None
    out = DATA_DIR / (args[2] or "backtest_experimental.json")
    result = run(AlpacaData(), start, end, daily=args[3] == "daily")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp"); tmp.write_text(json.dumps(result, separators=(",", ":"))); os.replace(tmp, out)
    print(f"Wrote {out.name}: {len(result['trades'])} trades, {out.stat().st_size / 1e6:.1f} MB")
