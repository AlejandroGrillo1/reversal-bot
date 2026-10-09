"""
RSI MODELS  (3x leveraged, signals on SPY / QQQ / MU)
=====================================================
Writes data/backtest_rsi.json for the dashboard tab index.html?data=rsi.

RSI is 14-period Wilder RSI on 1-minute closes, warmed up with the previous
afternoon so it is ready at 9:35. The overnight gap is taken out of the warm-up
(the afternoon is shifted to end at today's open), so a gap doesn't look like
one giant 1-minute move. A signal is checked when each minute
closes and the trade is bought at the next minute's opening price.

Groups (each on SPY, QQQ and MU, four tiers each)
  A "top-out" = RSI went over 70 and came back under; a "bottom-out" = under 30 and back
  over. Bottom-outs buy the 3x BULL ETF and top-outs buy the 3x BEAR ETF (a reversal bet),
  unless a group says otherwise.
  RSI Momentum   : crosses up through 60 -> bull ETF; down through 40 -> bear ETF.
  RSI Long only  : every bottom-out (bull ETF only).
  RSI Short only : every top-out (bear ETF only).
  RSI Clean reversal : the 3rd extreme of a clean alternating run (bottom, top, BOTTOM or
                 top, bottom, TOP); a run that starts with a double doesn't count.
  RSI 3 hits (75+) : the 3rd top-out (bottom-out) in a row. Hits 2 and 3 only count if RSI
                 reached 75 or higher on that visit (25 or lower for bottoms); weaker
                 ones are ignored. The first hit can be any top-out (bottom-out).
  RSI 4 hits (75+) : the same, trading the 4th hit; hits 2-4 must reach 75+ (25-).
  RSI 5 hits / RSI 6 hits : the 5th / 6th top-out (bottom-out) in a row at plain 70 / 30.
  RSI Reversal 9 : every top-out / bottom-out on a 9-period RSI (faster, more signals).
  RSI 20, 2 bounces : on a 20-period RSI (slower), the 2nd top-out (bottom-out) in a row.
  RSI Into extreme : buys the moment RSI first crosses INTO an extreme instead of waiting
                 for the rebound: drops below 30 -> bull ETF; rises above 70 -> bear ETF.
  Random control : the yardstick. Random minutes in the entry window, coin flip for
                 direction, same tiers, exits and limits. (Seeded: same picks every run.)
  Extremes are counted from 9:30; trades start at 9:35.

Rules for every model
  New trades from 9:35 AM to 1:00 PM ET. One trade open at a time, at most 5 per day.
  Targets on the stock's own move, stop the same size as the target (1:1):
      SPY / QQQ : Conservative ±0.11%, Moderate ±0.20%, Aggressive ±0.29%, Super aggressive ±0.38%
      MU        : Conservative ±0.23%, Moderate ±0.40%, Aggressive ±0.58%, Super aggressive ±0.76%
  (MU's are 2x SPY / QQQ's. At 1:1, random entries win about half the time.)
  Target and stop stay live until 3:30 PM; anything still open is sold at 3:30,
  green or red. (No more "sell if green" check.)
  Priced like the other 3x pages: real ETF price at entry, exit at 3x the
  SPY / QQQ move, $0.005 per share each way.
  MU (Micron) has no 3x ETF (there's a 2x bull, MUU, and a 1x bear, MUD), so MU
  trades are a 3x estimate: $100 x 3 x MU's move, minus MU_COST round trip.
  Same signals, tiers and rules as SPY and QQQ.

    python rsi_models.py [START] [END] [OUTPUT FILE]     (default 2026-01-01 to the latest close)
"""

import json
import os
import random
import sys
import datetime as dt

import experimental as X          # shares the Alpaca data code and price tools

START = "2026-01-01"
RSI_LEN = 14
RSI_FAST = 9                      # for the RSI Reversal 9 group
RSI_SLOW = 20                     # for the RSI 20, 2 bounces group
STRONG_HI, STRONG_LO = 75, 25     # "strong" hits for the 3- and 4-hit models
FIRST_SIGNAL, LAST_TIME = "09:35", "13:00"      # window for NEW trades
EOD_TIME = "15:30"                               # everything still open is sold here, win or lose
MAX_TRADES = 5
# hit-count models: (signal name, hits in a row, must hits 2..N be strong?)
HIT_MODELS = [("hit3s", 3, True), ("hit4s", 4, True), ("hit5", 5, False), ("hit6", 6, False), ("hit2", 2, False)]
# Target % of the stock's own move, per ticker. STOP_RATIO 1.0 = stop as big as the target (1:1).
TIERS = {
    "SPY": {"Conservative": 0.11, "Moderate": 0.20, "Aggressive": 0.29, "Super aggressive": 0.38},
    "QQQ": {"Conservative": 0.11, "Moderate": 0.20, "Aggressive": 0.29, "Super aggressive": 0.38},
    "MU":  {"Conservative": 0.23, "Moderate": 0.40, "Aggressive": 0.58, "Super aggressive": 0.76},
}
STOP_RATIO = 1.0
RANDOM_CANDIDATES = 8             # random entry times offered to the Random control each day
TIER_NAMES = list(TIERS["SPY"])
LEV, SLIP, TRADE = X.LEV, X.SLIPPAGE, X.TRADE_DOLLARS
# ticker -> its 3x bull / bear ETFs. None = no 3x ETF exists, so it's priced as a 3x estimate.
PAIRS = {**X.PAIRS, "MU": {"bull": None, "bear": None}}
MU_COST = 0.0003                  # round-trip cost for the 3x estimate (0.03% of the trade)
OUT = X.DATA_DIR / "backtest_rsi.json"

# RSI chart settings per group. events: "out" = mark crossings back out of an extreme (top-outs /
# bottom-outs), "in" = crossings into an extreme, "cross" = crossings of the hi / lo lines (momentum)
RSI_VIEW = {
    "rsi_mom": {"series": "r14", "hi": 60, "lo": 40, "events": "cross"},
    "rsi_rev9": {"series": "r9", "hi": 70, "lo": 30, "events": "out"},
    "rsi_into": {"series": "r14", "hi": 70, "lo": 30, "events": "in"},
    "rsi_hit3": {"series": "r14", "hi": 70, "lo": 30, "events": "out", "strong": [75, 25], "hits": 3},
    "rsi_hit4": {"series": "r14", "hi": 70, "lo": 30, "events": "out", "strong": [75, 25], "hits": 4},
    "rsi_hit5": {"series": "r14", "hi": 70, "lo": 30, "events": "out", "hits": 5},
    "rsi_hit6": {"series": "r14", "hi": 70, "lo": 30, "events": "out", "hits": 6},
    "rsi_r20b2": {"series": "r20", "hi": 70, "lo": 30, "events": "out", "hits": 2},
}

MODELS = [{"key": k, "rule": f"SPY/QQQ ±{TIERS['SPY'][k]:.2f}% · MU ±{TIERS['MU'][k]:.2f}% · 1:1 (3× ≈ ±{TIERS['SPY'][k] * LEV:.2f}% / ±{TIERS['MU'][k] * LEV:.2f}%)"}
          for k in TIER_NAMES]
GROUPS = [
    {"key": "rsi_mom", "name": "RSI Momentum", "color": "#4C7A5B", "bull": "up60", "bear": "down40",
     "desc": "Crosses up through 60 → bull ETF · down through 40 → bear ETF · new trades 9:35–1:00, max 5 a day"},
    {"key": "rsi_long", "name": "RSI Long only", "color": "#3F6E8C", "bull": "up30", "bear": None,
     "desc": "Only the buys: back above 30 → bull ETF · new trades 9:35–1:00, max 5 a day"},
    {"key": "rsi_short", "name": "RSI Short only", "color": "#8C5A3C", "bull": None, "bear": "down70",
     "desc": "Only the sells: back below 70 → bear ETF · new trades 9:35–1:00, max 5 a day"},
    {"key": "rsi_clean", "name": "RSI Clean reversal", "color": "#6B4C7A", "bull": "clean_up30", "bear": "clean_down70",
     "desc": "Trades the 3rd extreme of a clean alternating run (bottom, top, bottom or top, bottom, top) · new trades 9:35–1:00, max 5 a day"},
    {"key": "rsi_hit3", "name": "RSI 3 hits (75+)", "color": "#8A7A2E", "bull": "hit3s_up30", "bear": "hit3s_down70",
     "desc": f"Trades the 3rd top-out (or bottom-out) in a row; hits 2 and 3 must reach {STRONG_HI}+ ({STRONG_LO} or lower for bottoms)"},
    {"key": "rsi_hit4", "name": "RSI 4 hits (75+)", "color": "#A35D5D", "bull": "hit4s_up30", "bear": "hit4s_down70",
     "desc": f"Trades the 4th top-out (or bottom-out) in a row; hits 2–4 must reach {STRONG_HI}+ ({STRONG_LO} or lower for bottoms)"},
    {"key": "rsi_hit5", "name": "RSI 5 hits", "color": "#7A6F9B", "bull": "hit5_up30", "bear": "hit5_down70",
     "desc": "Trades the 5th top-out (or bottom-out) in a row at the normal 70 / 30 levels"},
    {"key": "rsi_hit6", "name": "RSI 6 hits", "color": "#2B2722", "bull": "hit6_up30", "bear": "hit6_down70",
     "desc": "Trades the 6th top-out (or bottom-out) in a row at the normal 70 / 30 levels"},
    {"key": "rsi_rev9", "name": "RSI Reversal 9", "color": "#5E8F99", "bull": "r9_up30", "bear": "r9_down70",
     "desc": f"{RSI_FAST}-period RSI · back above 30 → bull ETF · back below 70 → bear ETF · new trades 9:35–1:00, max 5 a day"},
    {"key": "rsi_r20b2", "name": "RSI 20, 2 bounces", "color": "#3F5E8C", "bull": "r20_hit2_up30", "bear": "r20_hit2_down70",
     "desc": f"{RSI_SLOW}-period RSI · trades the 2nd bottom-out (or top-out) in a row at 70 / 30 · new trades 9:35–1:00, max 5 a day"},
    {"key": "rsi_into", "name": "RSI Into extreme", "color": "#B8875A", "bull": "into30", "bear": "into70",
     "desc": "Buys as RSI first drops below 30 → bull ETF · first rises above 70 → bear ETF · no waiting for the rebound"},
    {"key": "rsi_random", "name": "Random control", "color": "#9A9083", "bull": "rand_up", "bear": "rand_down",
     "desc": "Random entry times and a coin flip for direction · same tiers and exits · the yardstick to beat"},
]


def wilder_rsi(closes, n=RSI_LEN):
    """RSI for each close (None until there is enough history)."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def signals(times, rsi):
    """{signal name: [minute, ...]} by the minute the signal closed."""
    sig = {k: [] for k in ("up30", "down70", "up60", "down40", "into30", "into70", "clean_up30", "clean_down70")}
    for name, _, _ in HIT_MODELS:
        sig[name + "_up30"], sig[name + "_down70"] = [], []
    extremes = []                                    # ("B" or "T", minute, deepest RSI of that visit), counted from 9:30
    peak = trough = None
    for i in range(1, len(times)):
        a, b, t = rsi[i - 1], rsi[i], times[i]
        if a is None or b is None or t >= LAST_TIME:
            continue
        if b > 70: peak = max(peak or b, b)
        if b < 30: trough = min(trough or b, b)
        if a < 30 <= b: extremes.append(("B", t, trough if trough is not None else a)); trough = None
        if a > 70 >= b: extremes.append(("T", t, peak if peak is not None else a)); peak = None
        if t < FIRST_SIGNAL:
            continue
        if a < 30 <= b: sig["up30"].append(t)
        if a > 70 >= b: sig["down70"].append(t)
        if a < 60 <= b: sig["up60"].append(t)
        if a > 40 >= b: sig["down40"].append(t)
        if a >= 30 > b: sig["into30"].append(t)          # just dropped into oversold
        if a <= 70 < b: sig["into70"].append(t)          # just rose into overbought
    kinds = [k for k, _, _ in extremes]
    for i, (k, t, _) in enumerate(extremes):
        if t < FIRST_SIGNAL:
            continue
        name = "up30" if k == "B" else "down70"
        # two clean swings: the last three extremes alternate (B,T,B or T,B,T),
        # and the first of the three wasn't part of a double
        if (i >= 2 and kinds[i - 1] != k and kinds[i - 2] == k
                and (i < 3 or kinds[i - 3] != kinds[i - 2])):
            sig["clean_" + name].append(t)
    # N hits in a row: the first hit can be any top-out (bottom-out); for "strong" models, later hits
    # only count if RSI reached 75+ (25 or lower) on that visit; weaker ones are ignored, not counted
    for name, n, strong in HIT_MODELS:
        kind, count = None, 0
        for k, t, depth in extremes:
            if k != kind:
                kind, count = k, 1
            elif strong and not (depth >= STRONG_HI if k == "T" else depth <= STRONG_LO):
                continue
            else:
                count += 1
            if count == n and t >= FIRST_SIGNAL:
                sig[f"{name}_{'up30' if k == 'B' else 'down70'}"].append(t)
    return sig


def minute_closes(ub):
    """Closes for every minute 9:30-15:59 (gaps filled with the last price), rounded to the cent."""
    out, last = [], None
    for i in range(390):
        t = f"{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}"
        if t in ub:
            last = ub[t][3]
        if last is not None:
            out.append(round(last, 2))
        elif out:
            out.append(out[-1])
    return out


def by_minute(times, values):
    """Spread values (one per bar time) over the 390 minutes from 9:30, one decimal, gaps carry forward."""
    vals = dict(zip(times, values))
    out, last = [], None
    for i in range(390):
        t = f"{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}"
        if vals.get(t) is not None:
            last = round(vals[t], 1)
        out.append(last)
    return out


def next_minute(t, times):
    later = [x for x in times if x > t]
    return later[0] if later else None


class Engine:
    def __init__(self):
        self.trades, self.days = [], []
        self.carry = {tk: [] for tk in PAIRS}          # previous afternoon's closes, to warm up RSI
        self.bars = {}                                  # {day: {ticker: [1-minute closes from 9:30]}} for the day chart
        self.rsi = {}                                   # {day: {ticker: {"r14": [...], "r9": [...]}}} for the RSI chart

    def day(self, day, bars):
        for tk, pair in PAIRS.items():
            ub = bars.get(tk, {})
            times = sorted(ub)
            if not times:
                continue
            self.bars.setdefault(day, {})[tk] = minute_closes(ub)
            warm = self.carry[tk]
            if warm:                                   # take the overnight gap out of the warm-up
                shift = ub[times[0]][0] / warm[-1]
                warm = [c * shift for c in warm]
            closes = warm + [ub[t][3] for t in times]
            self.carry[tk] = [ub[t][3] for t in times][-60:]
            r14, r9 = wilder_rsi(closes)[len(warm):], wilder_rsi(closes, RSI_FAST)[len(warm):]
            r20 = wilder_rsi(closes, RSI_SLOW)[len(warm):]
            self.rsi.setdefault(day, {})[tk] = {"r14": by_minute(times, r14), "r9": by_minute(times, r9), "r20": by_minute(times, r20)}
            sig = signals(times, r14)
            fast = signals(times, r9)
            sig.update({"r9_" + k: v for k, v in fast.items()})
            sig.update({"r20_" + k: v for k, v in signals(times, r20).items()})
            # Random control: random minutes in the entry window, coin-flip direction (seeded per day + ticker)
            rng = random.Random(f"{day}|{tk}")
            window = [t for t in times if FIRST_SIGNAL <= t < LAST_TIME]
            picks = sorted(rng.sample(window, min(RANDOM_CANDIDATES, len(window))))
            sig["rand_up"], sig["rand_down"] = [], []
            for t in picks:
                sig["rand_up" if rng.random() < 0.5 else "rand_down"].append(t)
            for g in GROUPS:
                events = sorted([(t, True) for t in (sig[g["bull"]] if g["bull"] else [])] +
                                [(t, False) for t in (sig[g["bear"]] if g["bear"] else [])])
                counts = []
                for model, tp in TIERS[tk].items():
                    n, free_at, res = 0, "00:00", []
                    for t_sig, bull in events:
                        if n >= MAX_TRADES or t_sig < free_at:
                            continue
                        t_in = next_minute(t_sig, times)
                        if not t_in or t_in >= LAST_TIME:
                            continue
                        r = self.trade(day, tk, pair, g["key"], model, tp, bull, t_in, ub, bars)
                        if r:
                            n += 1; free_at = r; res.append(self.trades[-1]["reason"])
                    counts.append(res)
                best = max(counts, key=len)
                if any(counts):
                    tally = ", ".join(f"{model} {len(c)}" for model, c in zip(TIER_NAMES, counts))
                    note = f"Trades by model: {tally}"
                else:
                    note = "No signal between 9:35 and 1:00"
                self.days.append({"date": day, "ticker": tk, "mode": g["key"], "direction": "",
                                  "traded": bool(best), "note": note})

    def trade(self, day, tk, pair, mode, model, tp, bull, t_in, ub, bars):
        d = 1 if bull else -1
        etf = pair["bull"] if bull else pair["bear"]
        p_in = X.price_at(ub, t_in)
        if etf:                                       # real 3x ETF
            etf_raw = X.price_at(bars.get(etf, {}), t_in)
            entry_fill, cost = (etf_raw + SLIP if etf_raw else None), SLIP
        else:                                         # no 3x ETF: price a $100 3x position from the stock's own move
            etf, etf_raw = f"{tk} 3× est.", 100.0
            entry_fill, cost = 100.0, 100.0 * MU_COST
        if not p_in or not etf_raw:
            return None
        tgt, stp = p_in * (1 + d * tp / 100), p_in * (1 - d * tp * STOP_RATIO / 100)
        value = lambda u: max(0.01, etf_raw * (1 + LEV * d * (u / p_in - 1)) - cost)
        hit = None
        for t, (o, h, l, c) in X.after(ub, t_in, EOD_TIME):
            if t != t_in and d * (o - stp) <= 0: hit = (t, o, "stop"); break
            if t != t_in and d * (o - tgt) >= 0: hit = (t, o, "target"); break
            worst, best = (l, h) if bull else (h, l)
            if d * (worst - stp) <= 0: hit = (t, stp, "stop"); break       # both in one minute: stop first
            if d * (best - tgt) >= 0: hit = (t, tgt, "target"); break
        if not hit:
            hit = (EOD_TIME, X.price_at(ub, EOD_TIME), "3:30 close")
        t_out, u_out, reason = hit
        exit_fill = value(u_out)
        self.trades.append({
            "date": day, "ticker": tk, "mode": mode, "model": model,
            "option": "CALL" if bull else "PUT", "side": "long 3×" if bull else "inverse 3×", "contract": etf,
            "entry": round(entry_fill, 2), "exit": round(exit_fill, 2),
            "pnl_pct": round(exit_fill / entry_fill - 1, 5),
            "pnl_usd": round((exit_fill - entry_fill) * TRADE / entry_fill, 2),
            "etf_entry": round(p_in, 2), "etf_exit": round(u_out, 2), "etf_move": round(u_out / p_in - 1, 5),
            "reason": reason, "entry_time": t_in + ":00", "exit_time": t_out + ":00"})
        return t_out


def run(data, start, end=None):
    now = dt.datetime.now(X.ET)
    latest = now.date() if now.time() >= dt.time(16, 5) else now.date() - dt.timedelta(days=1)
    end = min(end or latest, latest)
    closes = data.daily_closes(["SPY"], start - dt.timedelta(days=10), end)
    all_days = [d for d, _ in closes.get("SPY", [])]
    days = [d for d in all_days if d >= start.isoformat()]
    warm_day = [d for d in all_days if d < start.isoformat()][-1:]     # the afternoon before day one
    print(f"{len(days)} trading days: {days[0]} .. {days[-1]}")
    eng = Engine()
    syms = X.ALL_SYMBOLS + [tk for tk, p in PAIRS.items() if not p["bull"]]
    if warm_day:
        wb = data.minute_bars(list(PAIRS), warm_day)[warm_day[0]]
        for tk in PAIRS:
            eng.carry[tk] = [wb.get(tk, {})[t][3] for t in sorted(wb.get(tk, {}))][-60:]
    for mo in sorted({d[:7] for d in days}):
        md = [d for d in days if d.startswith(mo)]
        bars = data.minute_bars(syms, md)
        for d in md:
            if bars[d].get("SPY"):
                eng.day(d, bars[d])
    meta = {
        "generated": dt.datetime.now(X.ET).strftime("%Y-%m-%d %H:%M ET"),
        "start": days[0], "end": days[-1], "trading_days": len(days), "feed": data.feed,
        "note": (f"{RSI_LEN}-period RSI on 1-minute SPY/QQQ closes ({RSI_FAST}-period for RSI Reversal 9), warmed up with the prior afternoon (overnight gap removed). "
                 f"Targets and stops are the same size (1:1), so random entries win about half the time; compare every group with Random control. "
                 f"New trades 9:35 AM–1:00 PM, bought at the next minute's open; one trade at a time, max {MAX_TRADES} per model per day. "
                 f"Target and stop live until 3:30 PM, then anything still open is sold. {LEV}x ETFs, ${TRADE:.0f} per trade, ${SLIP} per share each way. "
                 f"MU has no 3x ETF, so MU trades are a 3x estimate from Micron's own move, {MU_COST * 100:.2f}% round-trip cost"),
        "groups": [{k: g[k] for k in ("key", "name", "desc", "color")} | {"models": MODELS} for g in GROUPS],
        "tickers": list(PAIRS),
        "entry_window": [FIRST_SIGNAL, LAST_TIME],          # shaded on the Day chart
        # which RSI line and levels the RSI chart shows for each group
        "rsi_view": {g["key"]: RSI_VIEW.get(g["key"], {"series": "r14", "hi": 70, "lo": 30, "events": "out"}) for g in GROUPS},
    }
    return {"meta": meta, "trades": eng.trades, "days": eng.days, "bars": eng.bars, "rsi": eng.rsi, "errors": []}


if __name__ == "__main__":
    # python rsi_models.py [START] [END] [OUTPUT FILE]   (blank END = latest close)
    a = sys.argv[1:] + ["", "", ""]
    start = dt.date.fromisoformat(a[0] or START)
    end = dt.date.fromisoformat(a[1]) if a[1] else None
    out = X.DATA_DIR / (a[2] or OUT.name)
    result = run(X.AlpacaData(), start, end)
    X.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp"); tmp.write_text(json.dumps(result, separators=(",", ":"))); os.replace(tmp, out)
    print(f"Wrote {out.name}: {len(result['trades'])} trades")
