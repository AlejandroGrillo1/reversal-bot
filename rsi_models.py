"""
RSI MODELS  (3x leveraged, signals on SPY / QQQ / MU)
=====================================================
Writes data/backtest_rsi.json for the dashboard tab index.html?data=rsi.

RSI is 14-period Wilder RSI on 1-minute closes of SPY / QQQ, warmed up with the
previous afternoon so it is ready at 9:35. A signal is checked when each minute
closes and the trade is bought at the next minute's opening price.

Groups (each on SPY and QQQ, three tiers each)
  RSI Reversal : RSI falls under 30 then crosses back above 30 -> buy the 3x BULL ETF
                 RSI rises over 70 then crosses back below 70  -> buy the 3x BEAR ETF
  RSI Momentum : RSI crosses up through 60   -> buy the 3x BULL ETF
                 RSI crosses down through 40 -> buy the 3x BEAR ETF
  RSI Long only  : just the "back above 30" buys from RSI Reversal
  RSI Short only : just the "back below 70" buys (bear ETF) from RSI Reversal
  RSI Clean reversal : like RSI Reversal, but only after two clean swings. A "top-out" =
                 RSI went over 70 and came back under; a "bottom-out" = under 30 and
                 back over. It trades the third extreme of an alternating run:
                 bottom, top, BOTTOM -> trade that bottom; top, bottom, TOP -> trade
                 that top. If the run started with a double (top, top, bottom, top),
                 or there are repeats (top, top, top), nothing is traded.
  RSI Third touch : trades only the 3rd top-out (or bottom-out) in a row, betting the
                 repeated pushes are finally exhausted. Same direction as RSI Reversal.
                 Change STAGGER_COUNT to 4 to wait for the fourth.
  RSI Reversal 9 : exactly RSI Reversal, but on a 9-period RSI (the last 9 minutes)
                 instead of 14, so it reacts faster and signals more often.
  Top-outs and bottom-outs are counted from 9:30; trades still start at 9:35.

Rules for every model
  New trades from 9:35 to 12:00 ET. One trade open at a time, at most 5 per day.
  Targets on the SPY / QQQ move: Conservative +0.30%, Moderate +0.55%,
  Aggressive +0.80%; stop at half the target (1:2).
  A trade that hasn't hit its target or stop by 2:30 PM is sold then if it's
  in the green; otherwise it holds (target and stop still live) until 3:55.
  Priced like the other 3x pages: real ETF price at entry, exit at 3x the
  SPY / QQQ move, $0.005 per share each way.
  MU (Micron) has no 3x ETF (there's a 2x bull, MUU, and a 1x bear, MUD), so MU
  trades are a 3x estimate: $100 x 3 x MU's move, minus MU_COST round trip.
  Same signals, tiers and rules as SPY and QQQ.

    python rsi_models.py [START_DATE]       (default 2026-01-01)
"""

import json
import os
import sys
import datetime as dt

import experimental as X          # shares the Alpaca data code and price tools

START = "2026-01-01"
RSI_LEN = 14
RSI_FAST = 9                      # for the RSI Reversal 9 group
FIRST_SIGNAL, LAST_TIME = "09:35", "12:00"      # window for NEW trades
GREEN_CHECK, EOD_TIME = "14:30", "15:55"         # sell at 2:30 if green, else hold to 3:55
MAX_TRADES = 5
STAGGER_COUNT = 3                 # RSI Third touch trades the Nth extreme in a row
ORD = {2: "2nd", 3: "3rd"}.get(STAGGER_COUNT, f"{STAGGER_COUNT}th")
TIERS = {"Conservative": 0.30, "Moderate": 0.55, "Aggressive": 0.80}   # % SPY/QQQ move; stop = half
LEV, SLIP, TRADE = X.LEV, X.SLIPPAGE, X.TRADE_DOLLARS
# ticker -> its 3x bull / bear ETFs. None = no 3x ETF exists, so it's priced as a 3x estimate.
PAIRS = {**X.PAIRS, "MU": {"bull": None, "bear": None}}
MU_COST = 0.0003                  # round-trip cost for the 3x estimate (0.03% of the trade)
OUT = X.DATA_DIR / "backtest_rsi.json"

MODELS = [{"key": k, "rule": f"SPY/QQQ/MU +{v:.2f}% / −{v / 2:.3g}% (3× ≈ +{v * LEV:.2f}%)"} for k, v in TIERS.items()]
GROUPS = [
    {"key": "rsi_rev", "name": "RSI Reversal", "color": "#2B2722", "bull": "up30", "bear": "down70",
     "desc": "Back above 30 → bull ETF · back below 70 → bear ETF · new trades 9:35–12:00, max 5 a day"},
    {"key": "rsi_mom", "name": "RSI Momentum", "color": "#4C7A5B", "bull": "up60", "bear": "down40",
     "desc": "Crosses up through 60 → bull ETF · down through 40 → bear ETF · new trades 9:35–12:00, max 5 a day"},
    {"key": "rsi_long", "name": "RSI Long only", "color": "#3F6E8C", "bull": "up30", "bear": None,
     "desc": "Only the buys: back above 30 → bull ETF · new trades 9:35–12:00, max 5 a day"},
    {"key": "rsi_short", "name": "RSI Short only", "color": "#8C5A3C", "bull": None, "bear": "down70",
     "desc": "Only the sells: back below 70 → bear ETF · new trades 9:35–12:00, max 5 a day"},
    {"key": "rsi_clean", "name": "RSI Clean reversal", "color": "#6B4C7A", "bull": "clean_up30", "bear": "clean_down70",
     "desc": "Trades the 3rd extreme of a clean alternating run (bottom, top, bottom or top, bottom, top) · new trades 9:35–12:00, max 5 a day"},
    {"key": "rsi_third", "name": "RSI Third touch", "color": "#8A7A2E", "bull": "third_up30", "bear": "third_down70",
     "desc": f"Trades the {ORD} top-out or bottom-out in a row, as a reversal · new trades 9:35–12:00, max 5 a day"},
    {"key": "rsi_rev9", "name": "RSI Reversal 9", "color": "#5E8F99", "bull": "r9_up30", "bear": "r9_down70",
     "desc": f"Same as RSI Reversal on a {RSI_FAST}-period RSI · back above 30 → bull ETF · back below 70 → bear ETF"},
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
    """{'up30': [minute, ...], 'down70': [...], 'up60': [...], 'down40': [...]} by the minute the signal closed."""
    sig = {k: [] for k in ("up30", "down70", "up60", "down40",
                           "clean_up30", "clean_down70", "third_up30", "third_down70")}
    extremes = []                                    # ("B" or "T", minute), counted from 9:30
    for i in range(1, len(times)):
        a, b, t = rsi[i - 1], rsi[i], times[i]
        if a is None or b is None or t >= LAST_TIME:
            continue
        if a < 30 <= b: extremes.append(("B", t))
        if a > 70 >= b: extremes.append(("T", t))
        if t < FIRST_SIGNAL:
            continue
        if a < 30 <= b: sig["up30"].append(t)
        if a > 70 >= b: sig["down70"].append(t)
        if a < 60 <= b: sig["up60"].append(t)
        if a > 40 >= b: sig["down40"].append(t)
    kinds = [k for k, _ in extremes]
    for i, (k, t) in enumerate(extremes):
        if t < FIRST_SIGNAL:
            continue
        name = "up30" if k == "B" else "down70"
        # two clean swings: the last three extremes alternate (B,T,B or T,B,T),
        # and the first of the three wasn't part of a double
        if (i >= 2 and kinds[i - 1] != k and kinds[i - 2] == k
                and (i < 3 or kinds[i - 3] != kinds[i - 2])):
            sig["clean_" + name].append(t)
        # how many of this same extreme in a row, ending here
        same = 0
        while i - same >= 0 and kinds[i - same] == k:
            same += 1
        if same == STAGGER_COUNT:
            sig["third_" + name].append(t)
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


def next_minute(t, times):
    later = [x for x in times if x > t]
    return later[0] if later else None


class Engine:
    def __init__(self):
        self.trades, self.days = [], []
        self.carry = {tk: [] for tk in PAIRS}          # previous afternoon's closes, to warm up RSI
        self.bars = {}                                  # {day: {ticker: [1-minute closes from 9:30]}} for the day chart

    def day(self, day, bars):
        for tk, pair in PAIRS.items():
            ub = bars.get(tk, {})
            times = sorted(ub)
            if not times:
                continue
            self.bars.setdefault(day, {})[tk] = minute_closes(ub)
            warm = self.carry[tk]
            closes = warm + [ub[t][3] for t in times]
            self.carry[tk] = [ub[t][3] for t in times][-60:]
            sig = signals(times, wilder_rsi(closes)[len(warm):])
            fast = signals(times, wilder_rsi(closes, RSI_FAST)[len(warm):])
            sig.update({"r9_" + k: v for k, v in fast.items()})
            for g in GROUPS:
                events = sorted([(t, True) for t in (sig[g["bull"]] if g["bull"] else [])] +
                                [(t, False) for t in (sig[g["bear"]] if g["bear"] else [])])
                counts = []
                for model, tp in TIERS.items():
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
                    tally = ", ".join(f"{model} {len(c)}" for model, c in zip(TIERS, counts))
                    note = f"Trades by model: {tally}"
                else:
                    note = "No RSI signal between 9:35 and 12:00"
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
        tgt, stp = p_in * (1 + d * tp / 100), p_in * (1 - d * tp / 200)
        value = lambda u: max(0.01, etf_raw * (1 + LEV * d * (u / p_in - 1)) - cost)
        hit, checked = None, False
        for t, (o, h, l, c) in X.after(ub, t_in, EOD_TIME):
            if t != t_in and d * (o - stp) <= 0: hit = (t, o, "stop"); break
            if t != t_in and d * (o - tgt) >= 0: hit = (t, o, "target"); break
            if t >= GREEN_CHECK and not checked:          # 2:30 PM: take it if it's in the green
                checked = True
                if value(o) > entry_fill: hit = (t, o, "2:30 green"); break
            worst, best = (l, h) if bull else (h, l)
            if d * (worst - stp) <= 0: hit = (t, stp, "stop"); break       # both in one minute: stop first
            if d * (best - tgt) >= 0: hit = (t, tgt, "target"); break
        if not hit:
            hit = (EOD_TIME, X.price_at(ub, EOD_TIME), "end of day")
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


def run(data, start):
    now = dt.datetime.now(X.ET)
    end = now.date() if now.time() >= dt.time(16, 5) else now.date() - dt.timedelta(days=1)
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
        "note": (f"{RSI_LEN}-period RSI on 1-minute SPY/QQQ closes ({RSI_FAST}-period for RSI Reversal 9), warmed up with the prior afternoon. "
                 f"New trades 9:35–12:00, bought at the next minute's open; one trade at a time, max {MAX_TRADES} per model per day. "
                 f"No target or stop by 2:30 PM: sold then if green, otherwise held to 3:55. {LEV}x ETFs, ${TRADE:.0f} per trade, ${SLIP} per share each way. "
                 f"MU has no 3x ETF, so MU trades are a 3x estimate from Micron's own move, {MU_COST * 100:.2f}% round-trip cost"),
        "groups": [{k: g[k] for k in ("key", "name", "desc", "color")} | {"models": MODELS} for g in GROUPS],
        "tickers": list(PAIRS),
    }
    return {"meta": meta, "trades": eng.trades, "days": eng.days, "bars": eng.bars, "errors": []}


if __name__ == "__main__":
    start = dt.date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else START)
    result = run(X.AlpacaData(), start)
    X.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp"); tmp.write_text(json.dumps(result, separators=(",", ":"))); os.replace(tmp, OUT)
    print(f"Wrote {OUT.name}: {len(result['trades'])} trades")
