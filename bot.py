"""
10:30 Reversal Bot  —  paper trades SPY and QQQ options around 10:30 ET.

Thesis: the morning move tends to reverse around 10:30 ET (7:30 PT).
  1. At 10:25 ET, look at the morning trend (price vs. the 9:30 open).
  2. Up morning  -> wait for price to close below VWAP, then buy puts.
     Down morning -> wait for price to close above VWAP, then buy calls.
  3. Three models per ticker share the entry but exit differently:
       A: +1% target / -0.5% stop   B: +2% / -1%   C: +3% / -1.5%
     (percent of the option's price)
  4. Anything still open at the time stop gets sold.

Results are appended to data/results.json, which the website reads.
"""
import json
import os
import time
import traceback
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, OptionLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

ET = ZoneInfo("America/New_York")

# ------------------------------ Settings ------------------------------
TICKERS = ["SPY", "QQQ"]
MODELS = {"A": (0.01, 0.005), "B": (0.02, 0.01), "C": (0.03, 0.015)}  # (target, stop)
CONTRACTS_PER_MODEL = 1
TREND_THRESHOLD = 0.003        # morning must move 0.3%+ from the open to count as a trend
WINDOW_START = dtime(10, 25)   # 7:25 PT
WINDOW_END = dtime(10, 35)     # 7:35 PT
TIME_STOP = dtime(11, 30)      # sell anything still open (8:30 PT)
USE_RSI_FILTER = False         # True = also require RSI >70 (up) / <30 (down) in last 30 min
POLL_SECONDS = 10
RESULTS = Path("data/results.json")
# ----------------------------------------------------------------------

TEST_MODE = os.getenv("TEST_MODE", "").lower() == "true"
KEY, SECRET = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]

trading = TradingClient(KEY, SECRET, paper=True)
stocks = StockHistoricalDataClient(KEY, SECRET)
options = OptionHistoricalDataClient(KEY, SECRET)


def now_et():
    return datetime.now(ET)


def load_results():
    if RESULTS.exists():
        return json.loads(RESULTS.read_text())
    return {"trades": [], "days": []}


def save_results(results):
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2))


# ---------------------------- Market data -----------------------------
def get_bars(symbol):
    """Completed 1-minute bars for today since 9:30 ET (free IEX feed)."""
    today = now_et().date()
    start = datetime.combine(today, dtime(9, 30), tzinfo=ET)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                           start=start, feed=DataFeed.IEX)
    df = stocks.get_stock_bars(req).df
    if df.empty:
        return df
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    cutoff = pd.Timestamp(now_et().replace(second=0, microsecond=0))
    return df[df.index < cutoff]  # drop the bar that's still forming


def vwap(df):
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return (typical * df["volume"]).cumsum() / df["volume"].cumsum()


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + gain / loss)


def pick_contract(symbol, price, kind):
    """Nearest-expiration (usually same-day) contract with strike closest to price."""
    today = now_et().date()
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=today,
        expiration_date_lte=today + timedelta(days=7),
        type=ContractType.CALL if kind == "CALL" else ContractType.PUT,
        strike_price_gte=str(round(price - 5)),
        strike_price_lte=str(round(price + 5)),
        limit=1000,
    )
    contracts = trading.get_option_contracts(req).option_contracts
    if not contracts:
        raise RuntimeError(f"No option contracts found for {symbol}")
    soonest = min(c.expiration_date for c in contracts)
    pool = [c for c in contracts if c.expiration_date == soonest]
    return min(pool, key=lambda c: abs(float(c.strike_price) - price))


def option_mid(symbol):
    quote = options.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
    if quote.bid_price and quote.ask_price:
        return (float(quote.bid_price) + float(quote.ask_price)) / 2
    return None


def market_order(symbol, qty, side):
    """Send a market order and wait for the fill price."""
    order = trading.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY))
    for _ in range(30):
        order = trading.get_order_by_id(order.id)
        if order.filled_avg_price:
            return float(order.filled_avg_price)
        time.sleep(1)
    raise RuntimeError(f"Order for {symbol} did not fill within 30s")


# ------------------------------- Bot ----------------------------------
class TickerBot:
    def __init__(self, symbol):
        self.symbol = symbol
        self.phase = "wait"          # wait -> watch -> in_trade -> done
        self.direction = None        # "up" / "down" morning
        self.kind = None             # "PUT" / "CALL"
        self.contract = None
        self.entry = None
        self.entry_time = None
        self.time_stop = None
        self.open_models = []
        self.errors = 0
        self.day = {"date": str(now_et().date()), "ticker": symbol, "direction": None,
                    "traded": False, "note": "", "test": TEST_MODE}

    def finish(self, note):
        self.day["note"] = note
        self.phase = "done"
        print(f"[{self.symbol}] {note}")

    def step(self, now, results):
        if self.phase == "wait":
            if not TEST_MODE and now.time() < WINDOW_START:
                return
            bars = get_bars(self.symbol)
            if len(bars) < 5:
                return
            move = bars["close"].iloc[-1] / bars["open"].iloc[0] - 1
            if TEST_MODE:
                self.direction = "up" if move >= 0 else "down"
            elif move > TREND_THRESHOLD:
                self.direction = "up"
            elif move < -TREND_THRESHOLD:
                self.direction = "down"
            else:
                self.day["direction"] = "flat"
                return self.finish(f"Flat morning ({move:+.2%}), no trade")
            self.day["direction"] = self.direction
            self.kind = "PUT" if self.direction == "up" else "CALL"
            print(f"[{self.symbol}] Morning {self.direction} {move:+.2%}, watching for VWAP cross")
            if TEST_MODE:
                return self.enter(bars["close"].iloc[-1], now)
            self.phase = "watch"

        elif self.phase == "watch":
            if now.time() > WINDOW_END:
                return self.finish(f"Morning {self.direction}, no VWAP cross in window")
            bars = get_bars(self.symbol)
            price, v = bars["close"].iloc[-1], vwap(bars).iloc[-1]
            crossed = price < v if self.direction == "up" else price > v
            if USE_RSI_FILTER:
                recent = rsi(bars["close"]).iloc[-30:]
                crossed = crossed and (recent.max() > 70 if self.direction == "up" else recent.min() < 30)
            if crossed:
                self.enter(price, now)

        elif self.phase == "in_trade":
            if now >= self.time_stop:
                for m in list(self.open_models):
                    self.exit(m, "time stop", results)
            else:
                mid = option_mid(self.contract)
                if mid is None:
                    return
                for m in list(self.open_models):
                    target, stop = MODELS[m]
                    if mid >= self.entry * (1 + target):
                        self.exit(m, "target", results)
                    elif mid <= self.entry * (1 - stop):
                        self.exit(m, "stop", results)
            if not self.open_models:
                self.finish(f"Morning {self.direction}, bought {self.kind}s, all models closed")

    def enter(self, price, now):
        c = pick_contract(self.symbol, price, self.kind)
        self.contract = c.symbol
        self.entry = market_order(c.symbol, CONTRACTS_PER_MODEL * len(MODELS), OrderSide.BUY)
        self.entry_time = now_et()
        self.time_stop = (now + timedelta(minutes=10)) if TEST_MODE else \
            datetime.combine(now.date(), TIME_STOP, tzinfo=ET)
        self.open_models = list(MODELS)
        self.day["traded"] = True
        self.phase = "in_trade"
        print(f"[{self.symbol}] Bought {self.contract} @ {self.entry:.2f}")

    def exit(self, model, reason, results):
        px = market_order(self.contract, CONTRACTS_PER_MODEL, OrderSide.SELL)
        self.open_models.remove(model)
        results["trades"].append({
            "date": self.day["date"], "ticker": self.symbol, "model": model,
            "morning": self.direction, "option": self.kind, "contract": self.contract,
            "entry": round(self.entry, 4), "exit": round(px, 4),
            "pnl_pct": round(px / self.entry - 1, 4),
            "pnl_usd": round((px - self.entry) * 100 * CONTRACTS_PER_MODEL, 2),
            "reason": reason,
            "entry_time": self.entry_time.strftime("%H:%M:%S"),
            "exit_time": now_et().strftime("%H:%M:%S"),
            "test": TEST_MODE,
        })
        print(f"[{self.symbol}] Model {model} sold @ {px:.2f} ({reason})")


def main():
    results = load_results()
    now = now_et()
    if not trading.get_clock().is_open:
        print("Market is closed today, exiting.")
        return
    if not TEST_MODE:
        # Two cron times cover daylight saving; only the one landing 9:30-10:25 ET runs.
        if not (dtime(9, 30) <= now.time() < WINDOW_START):
            print(f"Started at {now:%H:%M} ET, outside start window, exiting.")
            return
        if any(d["date"] == str(now.date()) and not d.get("test") for d in results["days"]):
            print("Already ran today, exiting.")
            return

    bots = [TickerBot(s) for s in TICKERS]
    hard_stop = datetime.combine(now.date(), TIME_STOP, tzinfo=ET) + timedelta(minutes=20)
    if TEST_MODE:
        hard_stop = now + timedelta(minutes=30)
    try:
        while not all(b.phase == "done" for b in bots) and now_et() < hard_stop:
            now = now_et()
            for b in bots:
                if b.phase == "done":
                    continue
                try:
                    b.step(now, results)
                except Exception:
                    traceback.print_exc()
                    b.errors += 1
                    if b.errors > 20 and b.phase != "in_trade":
                        b.finish("Stopped after repeated errors")
            time.sleep(POLL_SECONDS)
    finally:
        # Safety net: close anything still open, then record the day.
        for b in bots:
            for m in list(b.open_models):
                try:
                    b.exit(m, "forced close", results)
                except Exception:
                    traceback.print_exc()
            if b.phase != "done":
                b.day["note"] = b.day["note"] or "Run ended early"
            results["days"].append(b.day)
        save_results(results)


if __name__ == "__main__":
    main()
