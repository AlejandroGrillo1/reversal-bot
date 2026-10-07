"""
INTRADAY MOMENTUM  (3x leveraged ETFs, signals on SPY / QQQ)
============================================================
Writes data/backtest_imom.json for the dashboard tab index.html?data=imom.

The idea comes from published research, not from tuning on this data:
  Gao, Han, Li & Zhou, "Market Intraday Momentum" (Journal of Financial
  Economics, 2018) found that the S&P 500's first half hour (yesterday's close
  to 10:00) predicts its LAST half hour (3:30 to 4:00), in the same direction.
  Later work (e.g. Baltussen, Da, Lammers & Martens, 2021) ties it to forced
  late-day trading: leveraged ETFs and options dealers have to buy into the
  close on up days and sell on down days. A structural reason, not a pattern
  found by searching through indicators.

The rule (fixed in advance, no tuning)
  At 10:00, measure the move from yesterday's 4:00 close.
  At 3:30, buy the 3x ETF in that direction (up -> SPXL / TQQQ, down -> SPXS / SQQQ).
  Sell at the 4:00 close. No target, no stop, one trade per day at most.
  Tiers = how big the morning move has to be:
      Aggressive  : every day
      Moderate    : only if the morning move is at least 0.25%
      Conservative: only if the morning move is at least 0.50%
  Random control: same 3:30 -> 4:00 trade every day, coin flip for direction.

Runs on 30-minute bars, so five years takes seconds. Priced like the other
3x pages: real ETF price at 3:30, exit at 3x the SPY / QQQ move, $0.005 per
share each way.

    python intraday_momentum.py [START_DATE]       (default 2021-10-01)
"""

import json
import os
import random
import sys
import time
import datetime as dt

import experimental as X

START = "2021-10-01"
PAIRS, LEV, SLIP, TRADE, ET = X.PAIRS, X.LEV, X.SLIPPAGE, X.TRADE_DOLLARS, X.ET
SIGNAL_BAR, ENTRY_BAR = "09:30", "15:30"         # 9:30 bar closes at 10:00; the 3:30 bar runs 3:30 -> 4:00
TIERS = {"Conservative": 0.50, "Moderate": 0.25, "Aggressive": 0.0}   # minimum morning move, %
OUT = X.DATA_DIR / "backtest_imom.json"

MODELS = [{"key": k, "rule": f"Trade if the 10:00 move is at least {v:.2f}% · hold 3:30 → 4:00" if v else "Trade every day · hold 3:30 → 4:00"}
          for k, v in TIERS.items()]
GROUPS = [
    {"key": "imom", "name": "Intraday momentum", "color": "#2B2722",
     "desc": "Morning move (yesterday's close → 10:00) sets the direction · buy at 3:30, sell at 4:00"},
    {"key": "imom_random", "name": "Random control", "color": "#9A9083",
     "desc": "Same 3:30 → 4:00 trade, coin flip for direction · the yardstick to beat"},
]


class Bars30:
    """30-minute bars from Alpaca: {day: {symbol: {'HH:MM': (o, h, l, c)}}}, regular hours only."""

    def __init__(self):
        self.alpaca = X.AlpacaData()
        self.feed = self.alpaca.feed

    def fetch(self, symbols, start, end):
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        out = {}
        y = start
        while y <= end:                                   # a year at a time
            y_end = min(dt.date(y.year, 12, 31), end)
            print(f"  30-minute bars {y} .. {y_end}")
            data = self.alpaca._bars(symbols, dt.datetime.combine(y, dt.time(9, 30), ET),
                                     dt.datetime.combine(y_end, dt.time(16, 0), ET), TimeFrame(30, TimeFrameUnit.Minute))
            for sym, rows in data.items():
                for r in rows:
                    ts = r.timestamp.astimezone(ET)
                    hm = ts.strftime("%H:%M")
                    if "09:30" <= hm <= "15:30":
                        out.setdefault(ts.date().isoformat(), {}).setdefault(sym, {})[hm] = (
                            float(r.open), float(r.high), float(r.low), float(r.close))
            y = y_end + dt.timedelta(days=1)
        self.feed = self.alpaca.feed
        return out


def run(data, start, end=None):
    now = dt.datetime.now(ET)
    latest = now.date() if now.time() >= dt.time(16, 5) else now.date() - dt.timedelta(days=1)
    end = min(end or latest, latest)
    bars = data.fetch(X.ALL_SYMBOLS, start - dt.timedelta(days=7), end)
    days = sorted(d for d in bars if bars[d].get("SPY") and ENTRY_BAR in bars[d]["SPY"])
    trades, notes, prev = [], [], {}
    for day in days:
        b = bars[day]
        for tk, pair in PAIRS.items():
            ub = b.get(tk, {})
            last_close = prev.get(tk)
            if ub.get(ENTRY_BAR):
                prev[tk] = ub[ENTRY_BAR][3]                   # today's 4:00 close, for tomorrow
            if day < start.isoformat() or not last_close or SIGNAL_BAR not in ub or ENTRY_BAR not in ub:
                continue
            morning = (ub[SIGNAL_BAR][3] / last_close - 1) * 100
            p_in, u_out = ub[ENTRY_BAR][0], ub[ENTRY_BAR][3]
            rng = random.Random(f"{day}|{tk}")
            choices = {"imom": morning > 0, "imom_random": rng.random() < 0.5}
            for mode, bull in choices.items():
                etf = pair["bull"] if bull else pair["bear"]
                eb = b.get(etf, {})
                if ENTRY_BAR not in eb or morning == 0:
                    continue
                etf_raw = eb[ENTRY_BAR][0]
                d = 1 if bull else -1
                entry_fill = etf_raw + SLIP
                exit_fill = max(0.01, etf_raw * (1 + LEV * d * (u_out / p_in - 1)) - SLIP)
                for model, need in TIERS.items():
                    if abs(morning) < need:                  # Random control uses the same days, only the direction is random
                        continue
                    trades.append({
                        "date": day, "ticker": tk, "mode": mode, "model": model,
                        "option": "CALL" if bull else "PUT", "side": "long 3×" if bull else "inverse 3×", "contract": etf,
                        "entry": round(entry_fill, 2), "exit": round(exit_fill, 2),
                        "pnl_pct": round(exit_fill / entry_fill - 1, 5),
                        "pnl_usd": round((exit_fill - entry_fill) * TRADE / entry_fill, 2),
                        "etf_entry": round(p_in, 2), "etf_exit": round(u_out, 2), "etf_move": round(u_out / p_in - 1, 5),
                        "reason": "4:00 close", "entry_time": "15:30:00", "exit_time": "16:00:00"})
            took = [m for m, need in TIERS.items() if abs(morning) >= need]
            direction = "up" if morning > 0 else "down"
            etf = pair["bull"] if morning > 0 else pair["bear"]
            notes.append({"date": day, "ticker": tk, "mode": "imom", "direction": direction, "traded": True,
                          "note": f"{tk} {direction} {abs(morning):.2f}% by 10:00 → {etf} at 3:30 ({', '.join(took)})"})
            notes.append({"date": day, "ticker": tk, "mode": "imom_random", "direction": "", "traded": True,
                          "note": f"Coin flip: {'bull' if choices['imom_random'] else 'bear'} ETF at 3:30"})
    tdays = sorted({t["date"] for t in trades})
    meta = {
        "generated": dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "start": tdays[0], "end": tdays[-1], "trading_days": len(tdays), "feed": data.feed,
        "note": ("Rule from published research (Gao, Han, Li & Zhou, 2018), applied unchanged: the move from yesterday's close "
                 "to 10:00 sets the direction for a 3:30 → 4:00 trade. Every year shown is after the paper's own data, so this is "
                 f"an out-of-sample test. {LEV}x ETFs, ${TRADE:.0f} per trade, ${SLIP} per share each way. "
                 "Compare with Random control, which makes the same trade with a coin flip for direction"),
        "groups": [{k: g[k] for k in ("key", "name", "desc", "color")} | {"models": MODELS} for g in GROUPS],
        "tickers": list(PAIRS),
    }
    return {"meta": meta, "trades": trades, "days": notes, "errors": []}


if __name__ == "__main__":
    start = dt.date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else START)
    result = run(Bars30(), start)
    X.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp"); tmp.write_text(json.dumps(result, separators=(",", ":"))); os.replace(tmp, OUT)
    print(f"Wrote {OUT.name}: {len(result['trades'])} trades over {result['meta']['trading_days']} days")
