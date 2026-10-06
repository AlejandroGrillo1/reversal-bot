"""
10:30 Reversal Bot  —  paper trades SPY and QQQ options around 10:30 ET.

Thesis: the morning move tends to reverse around 10:30 ET (7:30 PT).
  1. At 10:22 ET, look at the morning trend (price vs. the 9:30 open).
  2. Up morning  -> wait for price to close below VWAP, then buy puts.
     Down morning -> wait for price to close above VWAP, then buy calls.
     RSI filter: the morning must also have been stretched (1-min RSI above 70
     on up mornings / below 30 on down mornings in the last 30 minutes).
  3. Each contract bought costs about TARGET_COST ($100): the bot picks the
     strike whose price is closest to $1.00 per share.
  4. Three models per ticker share the entry but exit differently. Targets
     are measured on the ETF's move in your favor, not the option's price:
       Conservative: ETF moves 0.10% your way (stop: 0.05% against)
       Moderate:     0.25% (stop: 0.125%)
       Aggressive:   0.40% (stop: 0.20%)
     P&L is still the real option P&L.
     Option stop: each model also sells if the option is down 10% / 20% / 35%
     (Conservative / Moderate / Aggressive), whatever SPY is doing.
     5-minute rule: once a model has been held HOLD_MINUTES, it sells as soon
     as the option is green (bid above entry). If it's red at that point, it
     keeps holding and only sells on a recovery once the option is up at least
     RECOVERY_MIN_RETURN (3%), or when it hits its target or stop, or at the
     end-of-day close.
  5. No time stop: positions ride until their target or stop hits. The only
     forced exit is the end-of-day close (same-day options expire at 4pm ET).

Each ticker also runs an "always" copy of the three models that skips the
trend, VWAP and RSI checks and trades every day at 10:28 ET (7:28 PT), betting against
whichever way the morning moved. That shows whether the signal adds value.

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
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest, OptionLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

ET = ZoneInfo("America/New_York")

# ------------------------------ Settings ------------------------------
TICKERS = ["SPY", "QQQ"]
MODELS = {
    # (ETF target, ETF stop, option stop). ETF numbers are % moves in SPY/QQQ
    # (0.0025 = 0.25%). The option stop sells if the option itself is down that
    # much, sized to roughly the loss the ETF stop would cause, so slow time
    # decay on a flat day can't bleed a trade past its max loss.
    "Conservative": (0.0010, 0.0005, 0.10),
    "Moderate": (0.0025, 0.00125, 0.20),
    "Aggressive": (0.0040, 0.0020, 0.35),
}
CONTRACTS_PER_MODEL = 1
HOLD_MINUTES = 5              # after this, sell any model that's green
RECOVERY_MIN_RETURN = 0.03    # if red at the 5-min mark, a recovery must reach +3% before selling
TARGET_COST = 100            # dollars per contract: picks the option whose price is closest to this
TREND_THRESHOLD = 0.003        # morning must move 0.3%+ from the open to count as a trend
WINDOW_START = dtime(10, 22)   # 7:22 PT
WINDOW_END = dtime(10, 40)     # 7:40 PT
ALWAYS_ENTRY = dtime(10, 28)   # 7:28 PT: when the always-trade models enter
EOD_CLOSE = dtime(15, 30)      # end-of-day exit, 12:30 PT (options expire at 4pm ET;
                               # GitHub also caps a run at 6 hours)
USE_RSI_FILTER = True          # signal models also require RSI >70 (up) / <30 (down) in last 30 min
MODES = ["signal", "always"]   # signal: needs trend + VWAP cross + RSI. always: trades every day at ALWAYS_ENTRY
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
    """Nearest-expiration contract whose cost is closest to TARGET_COST.
    Option prices are per share and a contract is 100 shares, so a $1.00 ask = $100."""
    today = now_et().date()
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=today,
        expiration_date_lte=today + timedelta(days=7),
        type=ContractType.CALL if kind == "CALL" else ContractType.PUT,
        strike_price_gte=str(round(price - 20)),
        strike_price_lte=str(round(price + 20)),
        limit=1000,
    )
    contracts = trading.get_option_contracts(req).option_contracts
    if not contracts:
        raise RuntimeError(f"No option contracts found for {symbol}")
    soonest = min(c.expiration_date for c in contracts)
    pool = [c for c in contracts if c.expiration_date == soonest]

    target = TARGET_COST / 100
    quotes = options.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=[c.symbol for c in pool]))
    best, best_diff = None, None
    for c in pool:
        q = quotes.get(c.symbol)
        if not q or not q.bid_price or not q.ask_price:
            continue
        diff = abs(float(q.ask_price) - target)
        if best is None or diff < best_diff:
            best, best_diff = c, diff
    if best is None:  # no quotes came back: fall back to the at-the-money strike
        best = min(pool, key=lambda c: abs(float(c.strike_price) - price))
    print(f"[{symbol}] Picked {best.symbol} (target ${TARGET_COST}/contract)")
    return best


def etf_price(symbol):
    """Latest traded price of the ETF (free IEX feed)."""
    trade = stocks.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))[symbol]
    return float(trade.price)


def option_bid(symbol):
    """Price we could sell at right now."""
    quote = options.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
    return float(quote.bid_price) if quote.bid_price else None


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
    def __init__(self, symbol, mode):
        self.symbol = symbol
        self.mode = mode
        self.tag = f"[{symbol} {mode}]"
        self.phase = "wait"          # wait -> watch -> in_trade -> done
        self.direction = None        # "up" / "down" morning
        self.kind = None             # "PUT" / "CALL"
        self.contract = None
        self.entry = None
        self.etf_entry = None
        self.red_at_5 = None         # was the option red at the 5-minute mark?
        self.entry_time = None
        self.time_stop = None
        self.open_models = []
        self.errors = 0
        self.day = {"date": str(now_et().date()), "ticker": symbol, "mode": mode, "direction": None,
                    "traded": False, "note": "", "test": TEST_MODE}

    def finish(self, note):
        self.day["note"] = note
        self.phase = "done"
        print(f"{self.tag} {note}")

    def step(self, now, results):
        if self.phase == "wait":
            start = ALWAYS_ENTRY if self.mode == "always" else WINDOW_START
            if not TEST_MODE and now.time() < start:
                return
            bars = get_bars(self.symbol)
            if len(bars) < 5:
                return
            move = bars["close"].iloc[-1] / bars["open"].iloc[0] - 1
            if TEST_MODE or self.mode == "always":
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
            print(f"{self.tag} Morning {self.direction} {move:+.2%}")
            if TEST_MODE or self.mode == "always":
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
                    self.exit(m, "end of day", results)
            else:
                px = etf_price(self.symbol)
                move = px / self.etf_entry - 1
                if self.kind == "PUT":
                    move = -move          # puts profit when the ETF falls
                held_long_enough = now - self.entry_time >= timedelta(minutes=HOLD_MINUTES)
                bid = option_bid(self.contract)
                if held_long_enough and bid is not None and self.red_at_5 is None:
                    self.red_at_5 = bid <= self.entry
                    print(f"{self.tag} 5-min mark: {'red' if self.red_at_5 else 'green'} (bid {bid:.2f} vs entry {self.entry:.2f})")
                if self.red_at_5:
                    sell_ok, why = bid is not None and bid >= self.entry * (1 + RECOVERY_MIN_RETURN), f"recovered +{RECOVERY_MIN_RETURN:.0%}"
                else:
                    sell_ok, why = bid is not None and bid > self.entry, "green after 5 min"
                for m in list(self.open_models):
                    target, stop, opt_stop = MODELS[m]
                    if move >= target:
                        self.exit(m, "target", results, px)
                    elif move <= -stop:
                        self.exit(m, "stop", results, px)
                    elif bid is not None and bid <= self.entry * (1 - opt_stop):
                        self.exit(m, "option stop", results, px)
                    elif held_long_enough and self.red_at_5 is not None and sell_ok:
                        self.exit(m, why, results, px)
            if not self.open_models:
                self.finish(f"Morning {self.direction}, bought {self.kind}s, all models closed")

    def enter(self, price, now):
        c = pick_contract(self.symbol, price, self.kind)
        self.contract = c.symbol
        self.entry = market_order(c.symbol, CONTRACTS_PER_MODEL * len(MODELS), OrderSide.BUY)
        self.etf_entry = etf_price(self.symbol)
        self.entry_time = now_et()
        self.time_stop = (now + timedelta(minutes=10)) if TEST_MODE else \
            datetime.combine(now.date(), EOD_CLOSE, tzinfo=ET)
        self.open_models = list(MODELS)
        self.day["traded"] = True
        self.phase = "in_trade"
        print(f"{self.tag} Bought {self.contract} @ {self.entry:.2f} ({self.symbol} at {self.etf_entry:.2f})")

    def exit(self, model, reason, results, etf_now=None):
        px = market_order(self.contract, CONTRACTS_PER_MODEL, OrderSide.SELL)
        if etf_now is None:
            try:
                etf_now = etf_price(self.symbol)
            except Exception:
                etf_now = self.etf_entry
        self.open_models.remove(model)
        results["trades"].append({
            "date": self.day["date"], "ticker": self.symbol, "mode": self.mode, "model": model,
            "morning": self.direction, "option": self.kind, "contract": self.contract,
            "entry": round(self.entry, 4), "exit": round(px, 4),
            "pnl_pct": round(px / self.entry - 1, 4),
            "pnl_usd": round((px - self.entry) * 100 * CONTRACTS_PER_MODEL, 2),
            "etf_entry": round(self.etf_entry, 2), "etf_exit": round(etf_now, 2),
            "etf_move": round(etf_now / self.etf_entry - 1, 5),
            "reason": reason,
            "entry_time": self.entry_time.strftime("%H:%M:%S"),
            "exit_time": now_et().strftime("%H:%M:%S"),
            "test": TEST_MODE,
        })
        print(f"{self.tag} {model} sold @ {px:.2f} ({reason})")


def main():
    results = load_results()
    now = now_et()
    if not trading.get_clock().is_open:
        print("Market is closed today, exiting.")
        return
    if not TEST_MODE:
        # Two cron times cover daylight saving; only the one landing 9:30-10:22 ET runs.
        if not (dtime(9, 30) <= now.time() < WINDOW_START):
            print(f"Started at {now:%H:%M} ET, outside start window, exiting.")
            return
        if any(d["date"] == str(now.date()) and not d.get("test") for d in results["days"]):
            print("Already ran today, exiting.")
            return

    bots = [TickerBot(s, mode) for s in TICKERS for mode in MODES]
    hard_stop = datetime.combine(now.date(), EOD_CLOSE, tzinfo=ET) + timedelta(minutes=10)
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
