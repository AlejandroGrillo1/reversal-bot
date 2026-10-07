"""
IDEA LAB  (3x leveraged ETFs, signals on SPY / QQQ)
====================================================
Writes data/backtest_lab.json for the dashboard tab index.html?data=lab.

Ten different ideas, each with its own reason it might work, plus a Random
control as the yardstick. Eight trade inside the day; two hold for days
(the "longer horizon" idea).

  1  Opening range breakout  First 15 minutes set a high and low. First 1-minute close
                             above the high -> bull ETF; below the low -> bear ETF.
                             Why: the opening range is where overnight orders get absorbed;
                             a clean break often means big players are pushing one way.
  2  Gap fill                Opens 0.30%+ away from yesterday's close -> bet on it moving
                             back toward that close, entered at 9:35.
                             Why: many gaps are an overreaction to overnight news in thin
                             pre-market trading and partly retrace once full volume arrives.
  3  VWAP snap-back          10:30-2:30, price 0.40%+ away from the day's VWAP -> bet on a
                             move back toward it.
                             Why: big funds benchmark against VWAP, and their execution
                             algorithms lean against prices that stretch far from it.
  4  Lunch fade              11:30-1:30, any 15-minute move of 0.20%+ -> fade it.
                             Why: midday volume is thin, so moves happen without much real
                             information behind them and tend to drift back.
  5  Power hour breakout     At 3:00, at the day's high -> bull ETF to the close; at the
                             day's low -> bear ETF.
                             Why: late-day highs and lows pull in closing flows (leveraged
                             ETF rebalancing, closing-auction orders) in the same direction.
  6  Volume spike fade       A 1-minute bar with 4x normal volume and a 0.15%+ move -> fade it.
                             Why: one-minute volume bursts are often a single large order or
                             a stop cascade, and price tends to give some of it back.
  7  SPY-QQQ snap-back       When QQQ runs 0.35%+ ahead of (or behind) SPY since the open,
                             bet the gap closes: sell the leader, buy the laggard.
                             Why: the two move together about 90% of the time, so a big
                             divergence inside one day tends to shrink.
  8  Trend day               By 10:30, moved 0.60%+ from the open and stayed on one side of
                             VWAP the whole time -> ride it to the close.
                             Why: strong one-way opens often become "trend days", where
                             institutions keep adding in the same direction all session.
  9  RSI(2) swing (days)     Daily 2-period RSI under 10 while above the 200-day average ->
                             buy the bull ETF at 3:55; sell when price closes above its
                             5-day average (or after a set number of days).
                             Why: a long-studied swing setup (Larry Connors); short, sharp
                             dips inside an uptrend have tended to bounce within days.
  10 Turn of the month (days) Buy the bull ETF at 3:55 two trading days before month end,
                             hold into the new month.
                             Why: pension contributions, payroll investing and month-end
                             rebalancing bring steady buying around the turn of the month.
  R  Random control          Random minutes 9:35-3:00, coin flip for direction, same exits.

Intraday ideas: new trades 9:35-3:00, one at a time, out by target, stop or 3:55.
  Tiers (target = stop, 1:1, on SPY/QQQ's move): Conservative ±0.20%, Moderate ±0.35%, Aggressive ±0.50%.
Multi-day ideas: tiers set the longest hold: Conservative 3 days, Moderate 5, Aggressive 10
  (turn of month: out on the 1st, 3rd or 5th trading day of the new month).
Pricing like the other 3x pages; multi-day trades use real 3x ETF closing prices,
so the ETFs' multi-day drift is included. $0.005 per share each way.

    python lab_models.py [START] [END] [OUTPUT FILE]     (default 2026-01-01 to the latest close)
"""

import json
import os
import random
import sys
import datetime as dt

import experimental as X
from rsi_models import minute_closes

START = "2026-01-01"
PAIRS, LEV, SLIP, TRADE, ET = X.PAIRS, X.LEV, X.SLIPPAGE, X.TRADE_DOLLARS, X.ET
FIRST, LAST_NEW, EOD = "09:35", "15:00", "15:55"
TIERS = {"Conservative": 0.20, "Moderate": 0.35, "Aggressive": 0.50}     # 1:1, % of SPY/QQQ move
HOLD_DAYS = {"Conservative": 3, "Moderate": 5, "Aggressive": 10}         # RSI(2) swing
TOM_EXIT = {"Conservative": 1, "Moderate": 3, "Aggressive": 5}           # turn of month: Nth day of new month
OUT = X.DATA_DIR / "backtest_lab.json"

INTRA = [{"key": k, "rule": f"SPY/QQQ ±{v:.2f}% (1:1) · out by 3:55"} for k, v in TIERS.items()]
GROUPS = [
    {"key": "orb", "name": "Opening range breakout", "color": "#2B2722", "kind": "intra", "max": 1,
     "desc": "Break of the first 15 minutes' high/low · why: big players pushing through where overnight orders were absorbed"},
    {"key": "gap", "name": "Gap fill", "color": "#4C7A5B", "kind": "intra", "max": 1,
     "desc": "Gap of 0.30%+ at the open, bet it moves back toward yesterday's close · why: thin pre-market overreactions"},
    {"key": "vwap", "name": "VWAP snap-back", "color": "#3F6E8C", "kind": "intra", "max": 3,
     "desc": "0.40%+ from VWAP (10:30–2:30), bet on a move back · why: fund execution algorithms lean against stretched prices"},
    {"key": "lunch", "name": "Lunch fade", "color": "#8C5A3C", "kind": "intra", "max": 3,
     "desc": "15-minute move of 0.20%+ at lunch (11:30–1:30), fade it · why: thin midday trading, little real information"},
    {"key": "power", "name": "Power hour breakout", "color": "#6B4C7A", "kind": "intra", "max": 1,
     "desc": "At the day's high/low at 3:00, ride it to the close · why: closing flows push the same way"},
    {"key": "vspike", "name": "Volume spike fade", "color": "#8A7A2E", "kind": "intra", "max": 3,
     "desc": "1-minute bar with 4× normal volume and a 0.15%+ move, fade it · why: one big order or a stop cascade tends to give back"},
    {"key": "pair", "name": "SPY–QQQ snap-back", "color": "#5E8F99", "kind": "intra", "max": 2,
     "desc": "QQQ 0.35%+ ahead of or behind SPY since the open, bet the gap closes · why: they move together ~90% of the time"},
    {"key": "trend", "name": "Trend day", "color": "#A35D5D", "kind": "intra", "max": 1,
     "desc": "0.60%+ by 10:30 and one side of VWAP the whole time, ride it · why: strong one-way opens often keep going all day"},
    {"key": "rsi2", "name": "RSI(2) swing (days)", "color": "#B8875A", "kind": "rsi2",
     "desc": "Daily RSI(2) under 10 above the 200-day average, buy; sell above the 5-day average · why: dips in uptrends bounce within days",
     "models": [{"key": k, "rule": f"Sell when the close beats its 5-day average, or after {n} days"} for k, n in HOLD_DAYS.items()]},
    {"key": "tom", "name": "Turn of the month (days)", "color": "#7A6F9B", "kind": "tom",
     "desc": "Buy 2 trading days before month end, hold into the new month · why: pension, payroll and rebalancing money arrives",
     "models": [{"key": k, "rule": f"Sell on trading day {n} of the new month"} for k, n in TOM_EXIT.items()]},
    {"key": "lab_random", "name": "Random control", "color": "#9A9083", "kind": "intra", "max": 3,
     "desc": "Random minutes 9:35–3:00, coin flip for direction, same exits · the yardstick to beat"},
]


def tm(i):
    """Minute index after 9:30 -> 'HH:MM'."""
    return f"{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}"


def nxt(t, times):
    later = [x for x in times if x > t]
    return later[0] if later else None


def vwap_series(ub, times):
    out, pv, vol = {}, 0.0, 0.0
    for t in times:
        o, h, l, c = ub[t][:4]
        v = ub[t][4] if len(ub[t]) > 4 else 1.0
        pv += (h + l + c) / 3 * v; vol += v
        out[t] = pv / vol if vol else c
    return out


def intraday_signals(day, tk, b, prev_close):
    """{group key: [(signal minute, bull?), ...]} for one ticker on one day."""
    ub = b.get(tk, {})
    times = sorted(ub)
    if len(times) < 60:
        return {}
    c = {t: ub[t][3] for t in times}
    o930 = ub[times[0]][0]
    vw = vwap_series(ub, times)
    sig = {g["key"]: [] for g in GROUPS if g["kind"] == "intra"}
    tradable = [t for t in times if FIRST <= t < LAST_NEW]

    # 1 opening range breakout (one signal, whichever side breaks first)
    rng_bars = [ub[t] for t in times if t < "09:45"]
    if rng_bars:
        hi, lo = max(x[1] for x in rng_bars), min(x[2] for x in rng_bars)
        for t in tradable:
            if t < "09:45":
                continue
            if c[t] > hi: sig["orb"].append((t, True)); break
            if c[t] < lo: sig["orb"].append((t, False)); break
    # 2 gap fill
    if prev_close and "09:35" in c:
        gap = o930 / prev_close - 1
        if abs(gap) >= 0.003:
            sig["gap"].append(("09:35", gap < 0))
    # 3 VWAP snap-back
    for t in tradable:
        if "10:30" <= t <= "14:30":
            dev = c[t] / vw[t] - 1
            if abs(dev) >= 0.004:
                sig["vwap"].append((t, dev < 0))
    # 4 lunch fade (15-minute moves)
    for i, t in enumerate(times):
        if "11:30" <= t <= "13:30" and i >= 15:
            mv = c[t] / c[times[i - 15]] - 1
            if abs(mv) >= 0.002:
                sig["lunch"].append((t, mv < 0))
    # 5 power hour breakout
    if "15:00" in c:
        before = [ub[t] for t in times if t < "15:00"]
        hi, lo = max(x[1] for x in before), min(x[2] for x in before)
        if c["15:00"] >= hi * 0.9995: sig["power"].append(("15:00", True))
        elif c["15:00"] <= lo * 1.0005: sig["power"].append(("15:00", False))
    # 6 volume spike fade
    vols = [ub[t][4] if len(ub[t]) > 4 else 0 for t in times]
    for i, t in enumerate(times):
        if i >= 20 and "09:45" <= t < LAST_NEW:
            avg = sum(vols[i - 20:i]) / 20
            mv = c[t] / ub[t][0] - 1
            if avg > 0 and vols[i] >= 4 * avg and abs(mv) >= 0.0015:
                sig["vspike"].append((t, mv < 0))
    # 7 SPY-QQQ snap-back (needs both)
    other = "QQQ" if tk == "SPY" else "SPY"
    ob = b.get(other, {})
    if ob:
        oo = ob[sorted(ob)[0]][0]
        for t in tradable:
            if t >= "10:00" and t in ob:
                me, them = c[t] / o930 - 1, ob[t][3] / oo - 1
                lead = me - them                           # positive = this ticker is ahead
                if abs(lead) >= 0.0035:
                    sig["pair"].append((t, lead < 0))      # ahead -> bear, behind -> bull
    # 8 trend day
    if "10:30" in c:
        mv = c["10:30"] / o930 - 1
        side = [c[t] > vw[t] for t in times if "09:45" <= t <= "10:30"]
        if abs(mv) >= 0.006 and side and (all(side) if mv > 0 else not any(side)):
            sig["trend"].append(("10:30", mv > 0))
    # R random control (seeded, same picks every run)
    r = random.Random(f"lab|{day}|{tk}")
    for t in sorted(r.sample(tradable, min(8, len(tradable)))):
        sig["lab_random"].append((t, r.random() < 0.5))
    return sig


def daily_rsi2(closes):
    if len(closes) < 3:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))][-2:]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))][-2:]
    ag, al = sum(gains) / 2, sum(losses) / 2
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


class Engine:
    def __init__(self):
        self.trades, self.days, self.bars = [], [], {}
        self.open = []                                    # multi-day positions

    def rec(self, **k):
        self.trades.append(k)

    def intraday_trade(self, day, tk, mode, model, tp, bull, t_in, b):
        ub, pair = b.get(tk, {}), PAIRS[tk]
        d = 1 if bull else -1
        etf = pair["bull"] if bull else pair["bear"]
        p_in, etf_raw = X.price_at(ub, t_in), X.price_at(b.get(etf, {}), t_in)
        if not p_in or not etf_raw:
            return None
        tgt, stp = p_in * (1 + d * tp / 100), p_in * (1 - d * tp / 100)
        hit = None
        for t in sorted(x for x in ub if t_in <= x < EOD):
            o, h, l, c = ub[t][:4]
            if t != t_in and d * (o - stp) <= 0: hit = (t, o, "stop"); break
            if t != t_in and d * (o - tgt) >= 0: hit = (t, o, "target"); break
            worst, best = (l, h) if bull else (h, l)
            if d * (worst - stp) <= 0: hit = (t, stp, "stop"); break
            if d * (best - tgt) >= 0: hit = (t, tgt, "target"); break
        if not hit:
            hit = (EOD, X.price_at(ub, EOD), "end of day")
        t_out, u_out, reason = hit
        entry_fill = etf_raw + SLIP
        exit_fill = max(0.01, etf_raw * (1 + LEV * d * (u_out / p_in - 1)) - SLIP)
        self.rec(date=day, ticker=tk, mode=mode, model=model, option="CALL" if bull else "PUT",
                 side="long 3×" if bull else "inverse 3×", contract=etf, entry=round(entry_fill, 2), exit=round(exit_fill, 2),
                 pnl_pct=round(exit_fill / entry_fill - 1, 5), pnl_usd=round((exit_fill - entry_fill) * TRADE / entry_fill, 2),
                 etf_entry=round(p_in, 2), etf_exit=round(u_out, 2), etf_move=round(u_out / p_in - 1, 5),
                 reason=reason, entry_time=t_in + ":00", exit_time=t_out + ":00")
        return t_out

    def run_intraday(self, day, b, prev_close):
        for tk in PAIRS:
            ub = b.get(tk, {})
            if not ub:
                continue
            self.bars.setdefault(day, {})[tk] = minute_closes(ub)
            times = sorted(ub)
            sig = intraday_signals(day, tk, b, prev_close.get(tk))
            for g in (g for g in GROUPS if g["kind"] == "intra"):
                events, counts = sig.get(g["key"], []), []
                for model, tp in TIERS.items():
                    n, free_at = 0, "00:00"
                    for t_sig, bull in events:
                        if n >= g["max"] or t_sig < free_at:
                            continue
                        t_in = nxt(t_sig, times)
                        if not t_in or t_in >= EOD:
                            continue
                        out = self.intraday_trade(day, tk, g["key"], model, tp, bull, t_in, b)
                        if out:
                            n += 1; free_at = out
                    counts.append(n)
                note = (f"Signals: {len(events)} · trades by model: " + ", ".join(f"{m} {k}" for m, k in zip(TIERS, counts))) if events else "No signal today"
                self.days.append({"date": day, "ticker": tk, "mode": g["key"], "direction": "", "traded": bool(any(counts)), "note": note})

    # ---- multi-day ideas (act at 3:55 on real ETF prices)
    def swing_sell(self, p, day, etf_px, und_px, reason):
        fill = max(0.01, etf_px - SLIP)
        self.rec(date=day, ticker=p["ticker"], mode=p["mode"], model=p["model"], option="CALL", side="long 3×",
                 contract=p["etf"], entry=round(p["fill"], 2), exit=round(fill, 2), pnl_pct=round(fill / p["fill"] - 1, 5),
                 pnl_usd=round((fill - p["fill"]) * TRADE / p["fill"], 2), etf_entry=round(p["und"], 2), etf_exit=round(und_px, 2),
                 etf_move=round(und_px / p["und"] - 1, 5), reason=reason, entry_time=EOD + ":00", exit_time=EOD + ":00",
                 entry_date=p["day"], exit_date=day)
        self.open.remove(p)

    def run_swing(self, day, b, hist, day_index, month_pos):
        for tk, pair in PAIRS.items():
            u = X.price_at(b.get(tk, {}), EOD); e = X.price_at(b.get(pair["bull"], {}), EOD)
            if not u or not e:
                continue
            closes = hist.get(tk, []) + [u]                     # daily closes up to and including today (3:55)
            sma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
            sma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
            rsi = daily_rsi2(closes[-3:])
            notes = {"rsi2": [], "tom": []}
            for p in [p for p in self.open if p["ticker"] == tk]:
                held = day_index - p["idx"]
                if p["mode"] == "rsi2":
                    if sma5 and u > sma5: self.swing_sell(p, day, e, u, "above 5-day avg"); notes["rsi2"].append(f"{p['model']} sold")
                    elif held >= HOLD_DAYS[p["model"]]: self.swing_sell(p, day, e, u, "time limit"); notes["rsi2"].append(f"{p['model']} timed out")
                elif p["mode"] == "tom" and month_pos[0] >= TOM_EXIT[p["model"]] and month_pos[1] != p["month"]:
                    self.swing_sell(p, day, e, u, f"day {TOM_EXIT[p['model']]} of month"); notes["tom"].append(f"{p['model']} sold")
            held_models = {(p["mode"], p["model"]) for p in self.open if p["ticker"] == tk}
            if rsi is not None and sma200 and rsi < 10 and u > sma200:
                for m in HOLD_DAYS:
                    if ("rsi2", m) not in held_models:
                        self.open.append({"ticker": tk, "mode": "rsi2", "model": m, "etf": pair["bull"], "fill": e + SLIP,
                                          "und": u, "day": day, "idx": day_index, "month": day[:7]})
                notes["rsi2"].append(f"RSI(2) {rsi:.0f} above the 200-day average, bought")
            if month_pos[2] == 2:                               # two trading days before month end
                for m in TOM_EXIT:
                    if ("tom", m) not in held_models:
                        self.open.append({"ticker": tk, "mode": "tom", "model": m, "etf": pair["bull"], "fill": e + SLIP,
                                          "und": u, "day": day, "idx": day_index, "month": day[:7]})
                notes["tom"].append("Two trading days before month end, bought")
            for k, label in (("rsi2", f"RSI(2) {rsi:.0f}" if rsi is not None else "RSI(2) n/a"), ("tom", "")):
                txt = "; ".join(notes[k]) or (f"{label}, no setup" if k == "rsi2" else ("Holding into the new month" if any(p["mode"] == "tom" and p["ticker"] == tk for p in self.open) else "Waiting for month end"))
                self.days.append({"date": day, "ticker": tk, "mode": k, "direction": "", "traded": bool(notes[k]), "note": txt})

    def close_all(self, day, b):
        for p in list(self.open):
            e = X.price_at(b.get(p["etf"], {}), EOD); u = X.price_at(b.get(p["ticker"], {}), EOD)
            if e and u:
                self.swing_sell(p, day, e, u, "closed at run end")
                self.trades[-1]["open_at_end"] = True


def month_positions(days):
    """For each day: (trading day number in its month, month, trading days left after it in the month)."""
    out = {}
    for i, d in enumerate(days):
        same = [x for x in days if x[:7] == d[:7]]
        n = same.index(d) + 1
        left = len(same) - n
        today = dt.date.fromisoformat(d)
        # the current month may not be complete yet: count remaining weekdays instead
        if d[:7] == days[-1][:7]:
            nd, left = today + dt.timedelta(days=1), 0
            while nd.month == today.month:
                if nd.weekday() < 5: left += 1
                nd += dt.timedelta(days=1)
        out[d] = (n, d[:7], left)
    return out


def run(data, start, end=None):
    now = dt.datetime.now(ET)
    latest = now.date() if now.time() >= dt.time(16, 5) else now.date() - dt.timedelta(days=1)
    end = min(end or latest, latest)
    closes = data.daily_closes(list(PAIRS), start - dt.timedelta(days=420), end)
    days = [d for d, _ in closes.get("SPY", []) if start.isoformat() <= d <= end.isoformat()]
    print(f"{len(days)} trading days: {days[0]} .. {days[-1]}")
    pos = month_positions(days)
    eng = Engine()
    for mo in sorted({d[:7] for d in days}):
        md = [d for d in days if d.startswith(mo)]
        bars = data.minute_bars(X.ALL_SYMBOLS, md)
        for d in md:
            b = bars[d]
            if not b.get("SPY"):
                continue
            prev = {tk: next((c for dd, c in reversed(closes.get(tk, [])) if dd < d), None) for tk in PAIRS}
            hist = {tk: [c for dd, c in closes.get(tk, []) if dd < d] for tk in PAIRS}
            eng.run_intraday(d, b, prev)
            eng.run_swing(d, b, hist, days.index(d), pos[d])
            last_b, last_d = b, d
    eng.close_all(last_d, last_b)
    meta = {
        "generated": dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "start": days[0], "end": last_d, "trading_days": len(days), "feed": data.feed,
        "note": ("Ten ideas, each with its own reason it might work, against a Random control. Intraday ideas: new trades 9:35 AM–3:00 PM, "
                 "one at a time, out by target or stop (the same size, 1:1) or 3:55. Multi-day ideas (RSI(2) swing, Turn of the month) hold "
                 f"for days on real 3x ETF closing prices. {LEV}x ETFs, ${TRADE:.0f} per trade, ${SLIP} per share each way"),
        "groups": [{k: g[k] for k in ("key", "name", "desc", "color")} | {"models": g.get("models", INTRA)} for g in GROUPS],
        "tickers": list(PAIRS),
        "entry_window": [FIRST, LAST_NEW],
    }
    return {"meta": meta, "trades": eng.trades, "days": eng.days, "bars": eng.bars, "errors": []}


class LabData(X.AlpacaData):
    """Like the shared Alpaca data, but minute bars also carry volume (needed for Volume spike fade)."""

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
                        out[d].setdefault(sym, {})[hm] = (float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
        return out


if __name__ == "__main__":
    # python lab_models.py [START] [END] [OUTPUT FILE]   (blank END = latest close)
    a = sys.argv[1:] + ["", "", ""]
    start = dt.date.fromisoformat(a[0] or START)
    end = dt.date.fromisoformat(a[1]) if a[1] else None
    out = X.DATA_DIR / (a[2] or OUT.name)
    result = run(LabData(), start, end)
    X.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp"); tmp.write_text(json.dumps(result, separators=(",", ":"))); os.replace(tmp, out)
    print(f"Wrote {out.name}: {len(result['trades'])} trades")
