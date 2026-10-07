"""
SWING MODELS, LONG HISTORY  (3x leveraged ETFs, signals on SPY / QQQ)
=====================================================================
Writes data/backtest_swing.json for the dashboard tab index.html?data=swing.

The two multi-day ideas from the Idea Lab, tested on as much daily history as
Alpaca has (back to 2016), with a fair benchmark for each:

  RSI(2) swing      Daily 2-period RSI under 10 while above the 200-day average
                    -> buy the bull ETF at the close; sell when the close beats its
                    5-day average, or after 3 / 5 / 10 days (the three tiers).
  Turn of the month Buy the bull ETF at the close two trading days before month end;
                    sell at the close of trading day 1 / 3 / 5 of the new month.

  Random twins      For every real trade, a "twin" buys at a random date and holds for
                    exactly the same number of days. Same exposure, random timing.
                    Both models only ever buy, so in a rising market they make money
                    with any timing; the twin shows how much of the profit is
                    real timing skill and how much is just being long.

Everything trades at the daily close on real split-adjusted SPXL / TQQQ prices
(so the ETFs' multi-day drift is included), $0.005 per share each way.

    python swing_models.py [START] [END] [OUTPUT FILE]   (default 2016-01-01 to the latest close)
"""

import json
import os
import random
import sys
import datetime as dt

import experimental as X

START = "2016-01-01"
PAIRS, SLIP, TRADE, ET = X.PAIRS, X.SLIPPAGE, X.TRADE_DOLLARS, X.ET
HOLD_DAYS = {"Conservative": 3, "Moderate": 5, "Aggressive": 10}
TOM_EXIT = {"Conservative": 1, "Moderate": 3, "Aggressive": 5}
OUT = X.DATA_DIR / "backtest_swing.json"

GROUPS = [
    {"key": "rsi2", "name": "RSI(2) swing", "color": "#B8875A",
     "desc": "Daily RSI(2) under 10 above the 200-day average, buy; sell above the 5-day average or at the time limit",
     "models": [{"key": k, "rule": f"Sell when the close beats its 5-day average, or after {n} days"} for k, n in HOLD_DAYS.items()]},
    {"key": "rsi2_twin", "name": "RSI(2) random twin", "color": "#D8C3A5",
     "desc": "Each RSI(2) trade copied with a random start date and the same hold length · real timing vs just being long",
     "models": [{"key": k, "rule": f"Random start, same days held as RSI(2) {k}"} for k in HOLD_DAYS]},
    {"key": "tom", "name": "Turn of the month", "color": "#7A6F9B",
     "desc": "Buy 2 trading days before month end, sell on day 1 / 3 / 5 of the new month",
     "models": [{"key": k, "rule": f"Sell on trading day {n} of the new month"} for k, n in TOM_EXIT.items()]},
    {"key": "tom_twin", "name": "Turn of month random twin", "color": "#C2BBD6",
     "desc": "Each turn-of-month trade copied with a random start date and the same hold length · real timing vs just being long",
     "models": [{"key": k, "rule": f"Random start, same days held as Turn of the month {k}"} for k in TOM_EXIT]},
]


def rsi2(closes):
    if len(closes) < 3:
        return None
    g = [max(closes[i] - closes[i - 1], 0) for i in (-2, -1)]
    l = [max(closes[i - 1] - closes[i], 0) for i in (-2, -1)]
    ag, al = sum(g) / 2, sum(l) / 2
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def trade_rec(mode, model, tk, etf, dates, ix_in, ix_out, und, etfp, reason, open_end=False):
    fill_in, fill_out = etfp[ix_in] + SLIP, max(0.01, etfp[ix_out] - SLIP)
    r = {"date": dates[ix_out], "ticker": tk, "mode": mode, "model": model, "option": "CALL", "side": "long 3×",
         "contract": etf, "entry": round(fill_in, 2), "exit": round(fill_out, 2),
         "pnl_pct": round(fill_out / fill_in - 1, 5), "pnl_usd": round((fill_out - fill_in) * TRADE / fill_in, 2),
         "etf_entry": round(und[ix_in], 2), "etf_exit": round(und[ix_out], 2), "etf_move": round(und[ix_out] / und[ix_in] - 1, 5),
         "reason": reason, "entry_time": "16:00:00", "exit_time": "16:00:00",
         "entry_date": dates[ix_in], "exit_date": dates[ix_out], "_in": ix_in, "_out": ix_out}
    if open_end:
        r["open_at_end"] = True
    return r


def run(data, start, end=None):
    now = dt.datetime.now(ET)
    latest = now.date() if now.time() >= dt.time(16, 5) else now.date() - dt.timedelta(days=1)
    end = min(end or latest, latest)
    syms = list(PAIRS) + [p["bull"] for p in PAIRS.values()]
    daily = data.daily_bars(syms, start - dt.timedelta(days=420), end)       # extra history for the 200-day average
    all_dates = sorted(d for d in daily if all(s in daily[d] for s in syms))
    first_trade_ix = next((i for i, d in enumerate(all_dates) if d >= start.isoformat()), None)
    if first_trade_ix is None:
        raise SystemExit("No data in that range.")
    dates = all_dates
    n = len(dates)
    # trading-day position within each month (and days left), using the real calendar
    months = {}
    for i, d in enumerate(dates):
        months.setdefault(d[:7], []).append(i)
    left, nth = {}, {}
    for mo, ixs in months.items():
        for k, i in enumerate(ixs):
            nth[i], left[i] = k + 1, len(ixs) - k - 1
    last_mo = dates[-1][:7]                                          # current month may be unfinished: count weekdays
    for i in months[last_mo]:
        dd, cnt = dt.date.fromisoformat(dates[i]) + dt.timedelta(days=1), 0
        while dd.month == dt.date.fromisoformat(dates[i]).month:
            cnt += dd.weekday() < 5; dd += dt.timedelta(days=1)
        left[i] = cnt

    trades, notes = [], {}                                            # notes: (date, ticker, group) -> [text]
    note = lambda d, tk, g, txt: notes.setdefault((d, tk, g), []).append(txt)
    for tk, pair in PAIRS.items():
        etf = pair["bull"]
        und = [daily[d][tk]["15:55"][3] for d in dates]
        etfp = [daily[d][etf]["15:55"][3] for d in dates]
        held = {}                                                     # (mode, model) -> entry index
        for i in range(n):
            if i < first_trade_ix:
                continue
            c = und[: i + 1]
            sma5 = sum(c[-5:]) / 5
            sma200 = sum(c[-200:]) / 200 if len(c) >= 200 else None
            r2 = rsi2(c[-3:])
            # exits first
            for (mode, model), ix in list(held.items()):
                if mode == "rsi2":
                    if und[i] > sma5: reason = "above 5-day avg"
                    elif i - ix >= HOLD_DAYS[model]: reason = "time limit"
                    else: continue
                else:
                    if dates[i][:7] != dates[ix][:7] and nth[i] >= TOM_EXIT[model]: reason = f"day {TOM_EXIT[model]} of month"
                    else: continue
                trades.append(trade_rec(mode, model, tk, etf, dates, ix, i, und, etfp, reason)); del held[(mode, model)]
                note(dates[i], tk, mode, f"{model} sold ({reason})")
            # entries
            if r2 is not None and sma200 and r2 < 10 and und[i] > sma200:
                for m in HOLD_DAYS:
                    if ("rsi2", m) not in held: held[("rsi2", m)] = i
                note(dates[i], tk, "rsi2", f"RSI(2) {r2:.0f} above the 200-day average, bought")
            if left[i] == 2:
                for m in TOM_EXIT:
                    if ("tom", m) not in held: held[("tom", m)] = i
                note(dates[i], tk, "tom", "Two trading days before month end, bought")
        for (mode, model), ix in held.items():                       # still open: value at the last close
            trades.append(trade_rec(mode, model, tk, etf, dates, ix, n - 1, und, etfp, "closed at run end", True))
            note(dates[n - 1], tk, mode, f"{model} still open, valued at the last close")

        # random twins: same hold length, random start (seeded, so the same every run)
        for t in [t for t in trades if t["ticker"] == tk and t["mode"] in ("rsi2", "tom")]:
            k = t["_out"] - t["_in"]
            rng = random.Random(f"twin|{tk}|{t['mode']}|{t['model']}|{t['_in']}")
            lo, hi = first_trade_ix, n - 1 - k
            if hi < lo:
                continue
            j = rng.randint(lo, hi)
            tw = trade_rec(t["mode"] + "_twin", t["model"], tk, etf, dates, j, j + k, und, etfp, f"random twin ({k} days)")
            trades.append(tw)
            note(dates[j + k], tk, tw["mode"], f"{t['model']} twin sold after {k} days")

    # year-by-year P&L per group (both tickers, all tiers)
    yearly = {}
    for t in trades:
        y = t["exit_date"][:4]
        yearly.setdefault(t["mode"], {}).setdefault(y, 0.0)
        yearly[t["mode"]][y] += t["pnl_usd"]
    yearly = {g: {y: round(v, 2) for y, v in sorted(ys.items())} for g, ys in yearly.items()}
    # buy and hold over the same period, for scale
    i0 = first_trade_ix
    bh = []
    for tk, pair in PAIRS.items():
        e0, e1 = daily[dates[i0]][pair["bull"]]["15:55"][3], daily[dates[-1]][pair["bull"]]["15:55"][3]
        u0, u1 = daily[dates[i0]][tk]["15:55"][3], daily[dates[-1]][tk]["15:55"][3]
        bh.append({"ticker": tk, "etf": pair["bull"], "und_ret": round(u1 / u0 - 1, 4), "etf_ret": round(e1 / e0 - 1, 4)})
    for t in trades:
        t.pop("_in", None); t.pop("_out", None)
    bhs = "; ".join(f"{b['ticker']} {b['und_ret'] * 100:+.0f}%, {b['etf']} {b['etf_ret'] * 100:+.0f}%" for b in bh)
    meta = {
        "generated": dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "start": dates[i0], "end": dates[-1], "trading_days": n - i0, "feed": data.feed,
        "note": ("Multi-day models on daily closes with real SPXL / TQQQ prices. Each has a random twin: the same trades, held the same "
                 "number of days, but started on random dates. A model only shows real timing skill if it clearly beats its twin. "
                 f"Buy and hold over this period: {bhs}. $100 per trade, ${SLIP} per share each way"),
        "groups": [{k: g[k] for k in ("key", "name", "desc", "color", "models")} for g in GROUPS],
        "tickers": list(PAIRS), "yearly": yearly, "buy_hold": bh,
    }
    days = [{"date": d, "ticker": tk, "mode": g, "direction": "", "traded": True, "note": "; ".join(v)}
            for (d, tk, g), v in sorted(notes.items())]
    return {"meta": meta, "trades": trades, "days": days, "errors": []}


if __name__ == "__main__":
    a = sys.argv[1:] + ["", "", ""]
    start = dt.date.fromisoformat(a[0] or START)
    end = dt.date.fromisoformat(a[1]) if a[1] else None
    out = X.DATA_DIR / (a[2] or OUT.name)
    result = run(X.AlpacaData(), start, end)
    X.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp"); tmp.write_text(json.dumps(result, separators=(",", ":"))); os.replace(tmp, out)
    print(f"Wrote {out.name}: {len(result['trades'])} trades, {result['meta']['start']} .. {result['meta']['end']}")
