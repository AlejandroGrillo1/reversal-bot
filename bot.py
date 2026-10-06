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
     Hold rule: once a model has been held its hold time (see HOLD_MINUTES),
     it sells as soon as the option is green (bid above entry). If it's red at
     that point, it keeps holding and only sells on a recovery once the option
     is up at least RECOVERY_MIN_RETURN (3%), or when it hits its target or
     stop, or at the end-of-day close.
  5. No time stop: positions ride until their target or stop hits. The only
     forced exit is the end-of-day close (same-day options expire at 4pm ET).

Each ticker runs five groups of the three models:
  signal:    the setup above, every model sells green after 5 min
  retrace35 / retrace25: same trend + RSI checks, but instead of the VWAP
             cross they enter once price has retraced 35% / 25% of the morning
             move back from the day's high (up mornings) or low (down
             mornings); 5-min hold
  staggered: same signal entry, but hold times of 5 / 12 / 20 min
             (Conservative / Moderate / Aggressive)
  always:    skips the trend, VWAP and RSI checks and trades every day at
             10:28 ET (7:28 PT), betting against the morning move; 5-min hold.
Comparing them shows whether the signal adds value and which hold time works.

ETF fallback: if the option side fails (no contract, buy rejected, options not
enabled), that group doesn't skip the day. It tracks the same trade on the ETF
price alone with no order placed, runs the same exits, and records an
ESTIMATED option P&L (tagged "fallback") so the day still has data.

Error handling: every API call is retried, one model or ticker failing never
stops the others, open positions keep retrying their sells until the close,
and every problem is written to results["errors"] so the website can show it.

Results are appended to data/results.json, which the website reads.
"""
import json
import os
import time
import traceback
import uuid
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
HOLD_MINUTES = {               # minutes before a green trade gets sold, per group and model
    "signal": {"Conservative": 5, "Moderate": 5, "Aggressive": 5},
    "retrace35": {"Conservative": 5, "Moderate": 5, "Aggressive": 5},
    "retrace25": {"Conservative": 5, "Moderate": 5, "Aggressive": 5},
    "staggered": {"Conservative": 5, "Moderate": 12, "Aggressive": 20},
    "always": {"Conservative": 5, "Moderate": 5, "Aggressive": 5},
}
RECOVERY_MIN_RETURN = 0.03    # if red at the hold mark, a recovery must reach +3% before selling
TARGET_COST = 100            # dollars per contract: picks the option whose price is closest to this
TREND_THRESHOLD = 0.003        # morning must move 0.3%+ from the open to count as a trend
WINDOW_START = dtime(10, 22)   # 7:22 PT
WINDOW_END = dtime(10, 50)     # 7:50 PT
ALWAYS_ENTRY = dtime(10, 28)   # 7:28 PT: when the always-trade models enter
EOD_CLOSE = dtime(15, 30)      # end-of-day exit, 12:30 PT (options expire at 4pm ET;
                               # GitHub also caps a run at 6 hours)
USE_RSI_FILTER = True          # signal models also require RSI >70 (up) / <30 (down) in last 30 min
MODES = ["signal", "retrace35", "retrace25", "staggered", "always"]
# signal/staggered: trend + VWAP cross + RSI. retrace groups: trend + pullback + RSI. always: trades daily
RETRACE_PCT = {"retrace35": 0.35, "retrace25": 0.25}   # how much of the open-to-extreme move must be given back
POLL_SECONDS = 10
FALLBACK_TO_ETF = True         # if options fail, track the trade on the ETF price instead of skipping
EST_LEVERAGE = 170             # fallback estimate: a ~$1 option moves ~170x the ETF's % move
EST_DECAY_PER_MIN = 0.002      # fallback estimate: option loses ~0.2% of its value per minute to time decay
RESULTS = Path("data/results.json")

# ------------------------- Error handling setup -----------------------
TEST_MODE = os.getenv("TEST_MODE", "").lower() == "true"
STATE = {"results": {"trades": [], "days": [], "errors": []}}
_error_counts = {}
trading = stocks = options = None


def now_et():
    return datetime.now(ET)


def load_results():
    try:
        if RESULTS.exists():
            data = json.loads(RESULTS.read_text())
        else:
            data = {}
    except Exception as e:  # corrupted file: keep a backup and start fresh rather than crash
        print(f"ERROR reading {RESULTS}: {e}. Backing it up and starting a new file.")
        RESULTS.rename(RESULTS.with_suffix(f".broken-{int(time.time())}.json"))
        data = {}
    for key in ("trades", "days", "errors"):
        data.setdefault(key, [])
    return data


def save_results(results):
    """Write results safely (temp file, then swap) so a crash mid-write can't corrupt it."""
    try:
        RESULTS.parent.mkdir(exist_ok=True)
        tmp = RESULTS.with_suffix(".tmp")
        tmp.write_text(json.dumps(results, indent=2))
        tmp.replace(RESULTS)
    except Exception as e:
        print(f"ERROR saving results: {e}")


def log_error(where, err, save=True):
    """Print an error, record it for the website, and keep going.
    Repeats of the same error are throttled so one flaky API can't flood the log."""
    msg = f"{type(err).__name__}: {err}" if isinstance(err, Exception) else str(err)
    key = f"{where}|{msg[:60]}"
    n = _error_counts[key] = _error_counts.get(key, 0) + 1
    print(f"ERROR [{where}] {msg}" + (f" (x{n})" if n > 1 else ""))
    if isinstance(err, Exception) and n <= 3:
        traceback.print_exc()
    if n <= 3 or n % 30 == 0:
        STATE["results"]["errors"].append({
            "date": str(now_et().date()), "time": now_et().strftime("%H:%M:%S"),
            "where": where, "message": msg[:300] + (f" (repeated {n}x)" if n > 3 else ""),
            "test": TEST_MODE,
        })
        if save:
            save_results(STATE["results"])


def retry(fn, *args, tries=3, wait=1.5, what="API call", **kwargs):
    """Call fn, retrying with a growing pause. Raises the last error if every try fails."""
    for attempt in range(1, tries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == tries:
                raise
            print(f"  retry {attempt}/{tries - 1} for {what}: {type(e).__name__}: {e}")
            time.sleep(wait * attempt)


def init_clients():
    global trading, stocks, options
    key, secret = os.getenv("ALPACA_KEY", "").strip(), os.getenv("ALPACA_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_KEY or ALPACA_SECRET is missing. Add both under "
                           "repo Settings > Secrets and variables > Actions.")
    trading = TradingClient(key, secret, paper=True)
    stocks = StockHistoricalDataClient(key, secret)
    options = OptionHistoricalDataClient(key, secret)


# ---------------------------- Market data -----------------------------
def get_bars(symbol):
    """Completed 1-minute bars for today since 9:30 ET (free IEX feed)."""
    today = now_et().date()
    start = datetime.combine(today, dtime(9, 30), tzinfo=ET)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                           start=start, feed=DataFeed.IEX)
    df = retry(stocks.get_stock_bars, req, what=f"{symbol} bars").df
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
    contracts = retry(trading.get_option_contracts, req, what=f"{symbol} contracts").option_contracts
    if not contracts:
        raise RuntimeError(f"No option contracts found for {symbol}")
    soonest = min(c.expiration_date for c in contracts)
    pool = [c for c in contracts if c.expiration_date == soonest]

    target = TARGET_COST / 100
    try:
        quotes = retry(options.get_option_latest_quote,
                       OptionLatestQuoteRequest(symbol_or_symbols=[c.symbol for c in pool]),
                       what=f"{symbol} option quotes")
    except Exception as e:
        log_error(f"{symbol} option quotes", e)
        quotes = {}
    best, best_diff = None, None
    for c in pool:
        q = quotes.get(c.symbol)
        if not q or not q.bid_price or not q.ask_price:
            continue
        diff = abs(float(q.ask_price) - target)
        if best is None or diff < best_diff:
            best, best_diff = c, diff
    if best is None:  # no quotes came back: fall back to the at-the-money strike
        print(f"[{symbol}] No option quotes, falling back to the at-the-money strike")
        best = min(pool, key=lambda c: abs(float(c.strike_price) - price))
    print(f"[{symbol}] Picked {best.symbol} (target ${TARGET_COST}/contract)")
    return best


def etf_price(symbol):
    """Latest traded price of the ETF (free IEX feed)."""
    trade = retry(stocks.get_stock_latest_trade,
                  StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX),
                  what=f"{symbol} price")[symbol]
    return float(trade.price)


def option_bid(symbol):
    """Price we could sell at right now (None if there's no bid)."""
    quote = retry(options.get_option_latest_quote, OptionLatestQuoteRequest(symbol_or_symbols=symbol),
                  what=f"{symbol} quote")[symbol]
    return float(quote.bid_price) if quote.bid_price else None


def _status(order):
    return str(getattr(order.status, "value", order.status)).lower()


def market_order(symbol, qty, side):
    """Send a market order and wait for it to fill. Returns (avg fill price, filled qty).
    Uses a client order id so a retried submit can never buy or sell twice."""
    cid = f"rb-{uuid.uuid4().hex[:24]}"
    req = MarketOrderRequest(symbol=symbol, qty=qty, side=side,
                             time_in_force=TimeInForce.DAY, client_order_id=cid)
    order = None
    for attempt in range(1, 4):
        try:
            order = trading.submit_order(req)
            break
        except Exception as e:
            try:  # the order may have gone through even though the reply errored
                order = trading.get_order_by_client_id(cid)
                break
            except Exception:
                pass
            if attempt == 3:
                raise RuntimeError(f"{side.value} {symbol} rejected: {e}") from e
            time.sleep(2 * attempt)

    for _ in range(45):
        try:
            order = trading.get_order_by_id(order.id)
        except Exception:
            time.sleep(1)
            continue
        filled = float(order.filled_qty or 0)
        if filled >= qty and order.filled_avg_price:
            return float(order.filled_avg_price), filled
        if _status(order) in ("rejected", "canceled", "cancelled", "expired"):
            if filled > 0 and order.filled_avg_price:
                return float(order.filled_avg_price), filled
            raise RuntimeError(f"{side.value} {symbol} order {_status(order)}")
        time.sleep(1)

    # Didn't fully fill in 45s: cancel the rest and keep whatever did fill.
    try:
        trading.cancel_order_by_id(order.id)
        time.sleep(1)
        order = trading.get_order_by_id(order.id)
    except Exception as e:
        print(f"  could not cancel slow order for {symbol}: {e}")
    filled = float(order.filled_qty or 0)
    if filled > 0 and order.filled_avg_price:
        return float(order.filled_avg_price), filled
    raise RuntimeError(f"{side.value} {symbol} did not fill within 45s, cancelled")


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
        self.red_at_mark = {}        # per model: was the option red at its hold mark?
        self.entry_time = None
        self.time_stop = None
        self.open_models = []
        self.errors = 0
        self.data_misses = 0
        self.sell_fails = {}
        self.fallback = False        # True = tracking on the ETF price only, no option position
        self.day = {"date": str(now_et().date()), "ticker": symbol, "mode": mode, "direction": None,
                    "traded": False, "note": "", "errors": 0, "test": TEST_MODE}

    def error(self, what, err):
        self.day["errors"] += 1
        log_error(f"{self.symbol} {self.mode}: {what}", err)

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
                if not TEST_MODE and now.time() > WINDOW_END:
                    return self.finish("No price data came in during the window, no trade")
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
            is_retrace = self.mode in RETRACE_PCT
            trigger = f"{RETRACE_PCT[self.mode]:.0%} retracement" if is_retrace else "VWAP cross"
            if now.time() > WINDOW_END:
                return self.finish(f"Morning {self.direction}, no {trigger} with RSI confirmation in window")
            bars = get_bars(self.symbol)
            if len(bars) < 15:
                return
            price = bars["close"].iloc[-1]
            if is_retrace:
                pct = RETRACE_PCT[self.mode]
                # Measure the move from the open to the day's extreme, then wait for a 33% give-back.
                day_open = bars["open"].iloc[0]
                if self.direction == "up":
                    high = bars["high"].max()
                    crossed = high > day_open and price <= high - pct * (high - day_open)
                else:
                    low = bars["low"].min()
                    crossed = low < day_open and price >= low + pct * (day_open - low)
            else:
                v = vwap(bars).iloc[-1]
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
                self.manage(now, results)
            if not self.open_models:
                note = (f"Morning {self.direction}, options failed so tracked on {self.symbol} price (estimated P&L)"
                        if self.fallback else f"Morning {self.direction}, bought {self.kind}s, all models closed")
                if self.day["errors"]:
                    note += f" ({self.day['errors']} error{'s' if self.day['errors'] > 1 else ''}, see log)"
                self.finish(note)

    def est_bid(self, move, now):
        """Fallback only: estimated option value from the ETF move and time decay."""
        minutes = (now - self.entry_time).total_seconds() / 60
        return min(self.entry * 5, max(0.01, self.entry * (1 + move * EST_LEVERAGE - minutes * EST_DECAY_PER_MIN)))

    def start_fallback(self, price, why):
        """Options failed: track this trade on the ETF price alone so the day still has data."""
        try:
            self.etf_entry = etf_price(self.symbol)
        except Exception:
            self.etf_entry = float(price)
        self.fallback = True
        self.contract = f"{self.symbol} (ETF est.)"
        self.entry = TARGET_COST / 100
        self.entry_time = now_et()
        self.time_stop = (self.entry_time + timedelta(minutes=10)) if TEST_MODE else \
            datetime.combine(self.entry_time.date(), EOD_CLOSE, tzinfo=ET)
        self.open_models = list(MODELS)
        self.day["traded"] = True
        self.day["fallback"] = True
        self.phase = "in_trade"
        print(f"{self.tag} {why}. Tracking on {self.symbol} price instead (estimated P&L, no order placed).")

    def manage(self, now, results):
        """Check every open model against its exits. Missing data skips only the checks that need it."""
        px = bid = move = None
        try:
            px = etf_price(self.symbol)
            move = px / self.etf_entry - 1
            if abs(move) > 0.05:      # a 5%+ jump in minutes is almost surely bad data: skip this tick
                raise ValueError(f"suspicious price {px:.2f} vs entry {self.etf_entry:.2f}")
            if self.kind == "PUT":
                move = -move          # puts profit when the ETF falls
        except Exception as e:
            self.data_misses += 1
            if self.data_misses in (6, 30) or self.data_misses % 90 == 0:
                self.error("ETF price unavailable, target/stop checks paused", e)
        try:
            if self.fallback:
                bid = self.est_bid(move, now) if move is not None else None
            else:
                bid = option_bid(self.contract)
        except Exception as e:
            self.data_misses += 1
            if self.data_misses in (6, 30) or self.data_misses % 90 == 0:
                self.error("option quote unavailable, option checks paused", e)
        if px is not None and bid is not None:
            self.data_misses = 0

        elapsed = now - self.entry_time
        for m in list(self.open_models):
            try:
                target, stop, opt_stop = MODELS[m]
                hold = HOLD_MINUTES[self.mode][m]
                if move is not None and move >= target:
                    self.exit(m, "target", results, px)
                elif move is not None and move <= -stop:
                    self.exit(m, "stop", results, px)
                elif bid is not None and bid <= self.entry * (1 - opt_stop):
                    self.exit(m, "option stop", results, px)
                elif elapsed >= timedelta(minutes=hold) and bid is not None:
                    if m not in self.red_at_mark:
                        self.red_at_mark[m] = bid <= self.entry
                        print(f"{self.tag} {m} {hold}-min mark: {'red' if self.red_at_mark[m] else 'green'} "
                              f"(bid {bid:.2f} vs entry {self.entry:.2f})")
                    if self.red_at_mark[m]:
                        if bid >= self.entry * (1 + RECOVERY_MIN_RETURN):
                            self.exit(m, f"recovered +{RECOVERY_MIN_RETURN:.0%}", results, px)
                    elif bid > self.entry:
                        self.exit(m, f"green after {hold} min", results, px)
            except Exception as e:   # one model's problem never blocks the others
                self.error(f"{m} check", e)

    def enter(self, price, now):
        try:
            c = pick_contract(self.symbol, price, self.kind)
        except Exception as e:
            self.error("picking a contract", e)
            if FALLBACK_TO_ETF:
                return self.start_fallback(price, "No option contract")
            return self.finish("Entry failed: couldn't find an option contract (see errors)")
        try:
            fill, filled = market_order(c.symbol, CONTRACTS_PER_MODEL * len(MODELS), OrderSide.BUY)
        except Exception as e:
            self.error("buy order", e)
            if FALLBACK_TO_ETF:
                return self.start_fallback(price, "Option buy failed")
            return self.finish("Entry failed: buy order didn't go through (see errors)")
        self.contract, self.entry = c.symbol, fill
        try:
            self.etf_entry = etf_price(self.symbol)
        except Exception as e:
            self.etf_entry = float(price)   # fall back to the last bar's close
            self.error("ETF price at entry, used last bar instead", e)
        self.entry_time = now_et()
        self.time_stop = (now + timedelta(minutes=10)) if TEST_MODE else \
            datetime.combine(now.date(), EOD_CLOSE, tzinfo=ET)
        n_models = int(filled // CONTRACTS_PER_MODEL)
        self.open_models = list(MODELS)[:n_models]
        if n_models < len(MODELS):
            self.error("partial fill", f"Only {filled:g} of {CONTRACTS_PER_MODEL * len(MODELS)} contracts filled; "
                                       f"trading {', '.join(self.open_models) or 'none'}")
        if not self.open_models:
            if FALLBACK_TO_ETF:
                return self.start_fallback(price, "Option order filled 0 usable contracts")
            return self.finish("Entry failed: order filled 0 usable contracts")
        self.day["traded"] = True
        self.phase = "in_trade"
        print(f"{self.tag} Bought {filled:g}x {self.contract} @ {self.entry:.2f} ({self.symbol} at {self.etf_entry:.2f})")

    def exit(self, model, reason, results, etf_now=None):
        """Sell one model's contract. If the sell fails, the model stays open and is retried next check.
        Fallback trades place no order: the exit price is the estimated option value."""
        try:
            if self.fallback:
                if etf_now is None:
                    try:
                        etf_now = etf_price(self.symbol)
                    except Exception:
                        etf_now = self.etf_entry   # no data at all: close it flat rather than leave it open
                move = etf_now / self.etf_entry - 1
                px = self.est_bid(-move if self.kind == "PUT" else move, now_et())
            else:
                px, _ = market_order(self.contract, CONTRACTS_PER_MODEL, OrderSide.SELL)
        except Exception as e:
            n = self.sell_fails[model] = self.sell_fails.get(model, 0) + 1
            if n in (1, 5) or n % 30 == 0:
                self.error(f"{model} sell ({reason}), will keep retrying", e)
            return False
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
            "fallback": self.fallback,
            "test": TEST_MODE,
        })
        save_results(results)   # save after every trade so a crash can't lose it
        print(f"{self.tag} {model} sold @ {px:.2f} ({reason})")
        return True


def main():
    results = STATE["results"] = load_results()
    now = now_et()
    try:
        init_clients()
        clock = retry(trading.get_clock, what="market clock", tries=4)
    except Exception as e:
        log_error("startup", e)
        raise SystemExit(1)
    if not clock.is_open:
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
    try:  # warn early if the account can't trade options
        acct = retry(trading.get_account, what="account")
        level = getattr(acct, "options_trading_level", None)
        if level is not None and int(level) < 2:
            log_error("startup", f"Options trading level is {level}; buying calls/puts needs level 2+. "
                                 "Enable options on the Alpaca paper account.")
    except Exception as e:
        log_error("startup: account check", e)

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
                except Exception as e:     # one bot's problem never stops the others
                    b.errors += 1
                    b.error(f"{b.phase} step", e)
                    if b.errors > 20 and b.phase in ("wait", "watch"):
                        b.finish("Stopped after repeated errors (see errors)")
            time.sleep(POLL_SECONDS)
    except BaseException as e:   # crash or cancel: still close positions and save below
        log_error("main loop", e if isinstance(e, Exception) else RuntimeError(repr(e)))
        raise
    finally:
        # Safety net: try hard to close anything still open, then record the day.
        for b in bots:
            for attempt in range(3):
                for m in list(b.open_models):
                    b.exit(m, "forced close", results)
                if not b.open_models:
                    break
                time.sleep(3)
            if b.open_models:
                b.error("forced close", f"{b.contract} still open for {', '.join(b.open_models)}. "
                                        "Close it manually in Alpaca.")
                b.day["note"] = (b.day["note"] + " | " if b.day["note"] else "") + "Position left open, check Alpaca"
            if b.phase != "done" and not b.day["note"]:
                b.day["note"] = "Run ended early"
            results["days"].append(b.day)
        save_results(results)


if __name__ == "__main__":
    main()
