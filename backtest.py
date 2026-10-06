"""
Backtest for the 10:30 Reversal Bot.

Replays every trading day in a date range (default: 2026 year to date) through
the SAME rules as bot.py: trend check, VWAP cross, RSI filter, 25% / 35%
retracements, the always-trade entry, ETF targets and stops, option stops, the
hold-time green exit, the +3% recovery rule and the end-of-day close.

What's real vs estimated:
  - REAL: SPY/QQQ 1-minute prices from Alpaca (full-market SIP data when
    available), so entries, ETF moves, targets and stops are what actually
    happened.
  - ESTIMATED: option P&L. It uses the same model as the live bot's fallback:
    a ~$1 same-day option moves ~EST_LEVERAGE x the ETF's % move and loses
    ~EST_DECAY_PER_MIN of its value per minute, plus an assumed 1-cent spread
    each way. Great for ranking models, not for exact dollars.

Results go to data/backtest.json; the website shows them at index.html?data=backtest.
Run it from GitHub: Actions > Backtest > Run workflow.
"""
import json
import os
import sys
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import bot  # use the live bot's exact settings and indicator math

ET = ZoneInfo("America/New_York")
OUT = Path("data/backtest.json")
HALF_SPREAD = 0.01            # assumed: buy 1 cent above the estimate, sell 1 cent below
START = (os.getenv("BT_START") or "2026-01-02").strip()
END = (os.getenv("BT_END") or "").strip() or str((datetime.now(ET) - timedelta(days=1)).date())


# ------------------------------ Data -----------------------------------
def fetch(start, end):
    """1-minute bars for SPY and QQQ, month by month. Tries full-market SIP data, falls back to IEX."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    key, secret = os.getenv("ALPACA_KEY", "").strip(), os.getenv("ALPACA_SECRET", "").strip()
    if not key or not secret:
        sys.exit("ALPACA_KEY / ALPACA_SECRET are missing. Add them under Settings > Secrets and variables > Actions.")
    client = StockHistoricalDataClient(key, secret)

    frames, feed_used = [], None
    cur = pd.Timestamp(start, tz=ET)
    last = pd.Timestamp(end, tz=ET) + pd.Timedelta(days=1)
    while cur < last:
        nxt = min(cur + pd.DateOffset(months=1), last)
        df = None
        for feed in ([DataFeed.IEX] if feed_used == "iex" else [DataFeed.SIP, DataFeed.IEX]):
            try:
                req = StockBarsRequest(symbol_or_symbols=bot.TICKERS, timeframe=TimeFrame.Minute,
                                       start=cur.to_pydatetime(), end=nxt.to_pydatetime(), feed=feed)
                df = bot.retry(client.get_stock_bars, req, what=f"bars {cur:%Y-%m}", tries=4).df
                feed_used = "sip" if feed == DataFeed.SIP else "iex"
                break
            except Exception as e:
                print(f"  {feed} data failed for {cur:%Y-%m}: {type(e).__name__}: {e}")
        if df is None:
            raise RuntimeError(f"Couldn't download bars for {cur:%Y-%m}")
        print(f"  {cur:%Y-%m}: {len(df):,} bars ({feed_used.upper()})")
        if not df.empty:
            frames.append(df.reset_index())
        cur = nxt
    if not frames:
        raise RuntimeError("No price data came back for that date range")
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    return df, feed_used


def split_days(df):
    """{(ticker, date): regular-hours bars indexed by ET timestamp}"""
    out = {}
    for (sym, day), g in df.groupby([df["symbol"], df["timestamp"].dt.date]):
        g = g.set_index("timestamp").sort_index()
        g = g[(g.index.time >= dtime(9, 30)) & (g.index.time < dtime(16, 0))]
        if len(g) >= 30:
            out[(sym, day)] = g[["open", "high", "low", "close", "volume"]]
    return out


# ---------------------------- Simulation ---------------------------------
def at(day, t):
    return pd.Timestamp(datetime.combine(day, t), tz=ET)


def est_mid(move, minutes):
    """Estimated option value (starting at 1.00) after a favorable ETF move and some minutes of decay."""
    return min(5.0, max(0.01, 1.0 + move * bot.EST_LEVERAGE - minutes * bot.EST_DECAY_PER_MIN))


def find_entry(bars, day, mode):
    """Apply the live bot's entry rules. Returns (direction, entry_time, etf_price, note)."""
    start = bot.ALWAYS_ENTRY if mode == "always" else bot.WINDOW_START
    t0 = at(day, start)
    done = bars[bars.index < t0]
    if len(done) < 5:
        return None, None, None, "Not enough price data"
    day_open = done["open"].iloc[0]
    move = done["close"].iloc[-1] / day_open - 1

    if mode == "always":
        direction = "up" if move >= 0 else "down"
        kind = "PUT" if direction == "up" else "CALL"
        return direction, t0, done["close"].iloc[-1], f"Morning {direction}, bought {kind}s at 7:28"
    if move > bot.TREND_THRESHOLD:
        direction = "up"
    elif move < -bot.TREND_THRESHOLD:
        direction = "down"
    else:
        return "flat", None, None, f"Flat morning ({move:+.2%}), no trade"

    pct = bot.RETRACE_PCT.get(mode)
    trigger = f"{pct:.0%} retracement" if pct else "VWAP cross"
    t, end = t0, at(day, bot.WINDOW_END)
    while t <= end:
        done = bars[bars.index < t]
        if len(done) >= 15:
            price = done["close"].iloc[-1]
            if pct:
                if direction == "up":
                    high = done["high"].max()
                    crossed = high > day_open and price <= high - pct * (high - day_open)
                else:
                    low = done["low"].min()
                    crossed = low < day_open and price >= low + pct * (day_open - low)
            else:
                v = bot.vwap(done).iloc[-1]
                crossed = price < v if direction == "up" else price > v
            if crossed and bot.USE_RSI_FILTER:
                recent = bot.rsi(done["close"]).iloc[-30:]
                crossed = recent.max() > 70 if direction == "up" else recent.min() < 30
            if crossed:
                kind = "PUT" if direction == "up" else "CALL"
                return direction, t, price, f"Morning {direction}, bought {kind}s on the {trigger}"
        t += pd.Timedelta(minutes=1)
    return direction, None, None, f"Morning {direction}, no {trigger} with RSI confirmation in window"


def run_models(bars, day, mode, direction, t_entry, etf_entry, ticker):
    """Walk minute by minute after entry and apply every exit rule to each model."""
    kind = "PUT" if direction == "up" else "CALL"
    sign = -1 if kind == "PUT" else 1
    eod = at(day, bot.EOD_CLOSE)
    after = bars[bars.index >= t_entry]
    entry_fill = 1.0 + HALF_SPREAD
    trades = []
    for model, (target, stop, opt_stop) in bot.MODELS.items():
        hold = bot.HOLD_MINUTES[mode][model]
        red, exit_ = None, None
        for ts, row in after.iterrows():
            mins = (ts - t_entry).total_seconds() / 60 + 1       # minutes held at the end of this bar
            hi = (row["high"] / etf_entry - 1) * sign
            lo = (row["low"] / etf_entry - 1) * sign
            fav, adv = max(hi, lo), min(hi, lo)
            close_move = (row["close"] / etf_entry - 1) * sign
            bar_end = ts + pd.Timedelta(minutes=1)
            if ts >= eod:
                exit_ = ("end of day", close_move, est_mid(close_move, mins), bar_end)
                break
            # Within one bar we can't tell which came first, so losses are checked first (conservative).
            if adv <= -stop:
                exit_ = ("stop", -stop, est_mid(-stop, mins), bar_end)
                break
            if est_mid(adv, mins) <= 1 - opt_stop:
                mv = (-opt_stop + mins * bot.EST_DECAY_PER_MIN) / bot.EST_LEVERAGE
                exit_ = ("option stop", mv, 1 - opt_stop, bar_end)
                break
            if fav >= target:
                exit_ = ("target", target, est_mid(target, mins), bar_end)
                break
            if mins >= hold:
                bid = est_mid(close_move, mins) - HALF_SPREAD
                if red is None:
                    red = bid <= entry_fill
                if red and bid >= entry_fill * (1 + bot.RECOVERY_MIN_RETURN):
                    exit_ = (f"recovered +{bot.RECOVERY_MIN_RETURN:.0%}", close_move, bid + HALF_SPREAD, bar_end)
                    break
                if not red and bid > entry_fill:
                    exit_ = (f"green after {hold} min", close_move, bid + HALF_SPREAD, bar_end)
                    break
        if exit_ is None:   # early-close day: sell at the last bar
            ts, row = after.index[-1], after.iloc[-1]
            mv = (row["close"] / etf_entry - 1) * sign
            exit_ = ("end of day", mv, est_mid(mv, (ts - t_entry).total_seconds() / 60 + 1), ts + pd.Timedelta(minutes=1))
        reason, mv, mid, t_exit = exit_
        sale = max(0.0, mid - HALF_SPREAD)
        etf_exit = etf_entry * (1 + sign * mv)
        trades.append({
            "date": str(day), "ticker": ticker, "mode": mode, "model": model,
            "morning": direction, "option": kind, "contract": f"{ticker} (backtest est.)",
            "entry": round(entry_fill, 4), "exit": round(sale, 4),
            "pnl_pct": round(sale / entry_fill - 1, 4),
            "pnl_usd": round((sale - entry_fill) * 100, 2),
            "etf_entry": round(float(etf_entry), 2), "etf_exit": round(float(etf_exit), 2),
            "etf_move": round(float(etf_exit / etf_entry - 1), 5),
            "reason": reason,
            "entry_time": t_entry.strftime("%H:%M:%S"), "exit_time": t_exit.strftime("%H:%M:%S"),
            "backtest": True, "test": False,
        })
    return trades


def simulate(day_bars):
    trades, days = [], []
    for (ticker, day) in sorted(day_bars, key=lambda k: (k[1], bot.TICKERS.index(k[0]))):
        bars = day_bars[(ticker, day)]
        for mode in bot.MODES:
            direction, t_entry, price, note = find_entry(bars, day, mode)
            rec = {"date": str(day), "ticker": ticker, "mode": mode, "direction": direction,
                   "traded": t_entry is not None, "note": note, "test": False}
            if t_entry is not None:
                trades += run_models(bars, day, mode, direction, t_entry, float(price), ticker)
                rec["note"] += ", all models closed"
            days.append(rec)
    return trades, days


def main():
    print(f"Backtest {START} to {END}")
    df, feed = fetch(START, END)
    day_bars = split_days(df)
    n_days = len({d for _, d in day_bars})
    print(f"{n_days} trading days loaded. Simulating {len(bot.MODES) * len(bot.TICKERS) * len(bot.MODELS)} models...")
    trades, days = simulate(day_bars)

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "meta": {
            "generated": datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
            "start": START, "end": END, "trading_days": n_days, "feed": feed,
            "note": "Real SPY/QQQ prices; option P&L estimated "
                    f"({bot.EST_LEVERAGE}x leverage, {bot.EST_DECAY_PER_MIN:.1%}/min decay, 1-cent spread each way)",
        },
        "trades": trades, "days": days, "errors": [],
    }, indent=1))

    # Quick summary in the Actions log
    t = pd.DataFrame(trades)
    if t.empty:
        print("No trades triggered in this range.")
        return
    s = t.groupby(["mode", "ticker", "model"]).agg(trades=("pnl_usd", "size"), win_rate=("pnl_usd", lambda x: (x > 0).mean()),
                                                   total=("pnl_usd", "sum")).sort_values("total", ascending=False)
    print(s.to_string(float_format=lambda v: f"{v:,.2f}"))
    print(f"\nSaved {len(trades):,} trades to {OUT}")


if __name__ == "__main__":
    main()
