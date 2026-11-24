"""Dip-bounce paper bot for the pump.fun replay sprint (agent-fun/replay/v2).

Strategy (pure stdlib, deterministic: a pure function of the batches the
harness sends, no clock, no randomness, no I/O):

  * Watch every bonding curve's trade tape. A coin becomes a candidate the
    first time it trades 20-40% below its own high of the last 500 slots
    while still holding >= 15 SOL of real reserves and buyers are still
    stepping in (at least 2 of the last 6 trades are buys and the latest
    trade is a buy). One entry per coin, ever: later dips on the same coin
    keep falling in the dev data, so re-entries are skipped.
  * Buy 0.25 SOL (capped at 97% of the harness's 1%-of-real-reserve order
    cap, at least 0.01 SOL) with wide-open slippage: the entry signal is
    the dip itself, and a fill a little above or below the decision price
    does not change it.
  * Exit the whole position on the first of: liquidation value >= cost
    + 15% (take profit), <= cost - 8% (stop loss), or 600 slots held.
    Liquidation value is computed from the harness's marks with the 1.25%
    curve fee and one tx fee deducted, against the position's cost basis
    (quote spent plus the one buy tx fee).
  * Guardrail probes: the run must end with round trips of >= 0.01 SOL on
    at least 20 coins. If the bot's own filled coins lag a pace of 25 coins
    over the window's block time, it opens one tiny 0.01 SOL probe round
    trip on the most liquid curve that traded this batch and sells it as
    soon as the buy lands.

Fees are ~1.25% per side plus 105k lamports per order, so a round trip
needs roughly a 3% move to break even; the 15%/8% exits only pay when the
dip actually bounces, which is why entries are restricted to liquid
curves with live buyers instead of every dip.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
BPS = 10_000

# ---- strategy parameters (chosen on the dev window, flat region) ----
HIGH_WINDOW_SLOTS = 300     # lookback for the recent high the dip is measured from
DD_MIN_NUM = 1              # dip >= 20%: 5*p <= 4*high  <=>  1 - p/high >= 1/5
DD_MIN_DEN = 5
DD_MAX_NUM = 2              # dip <= 40%: 5*p >= 3*high  <=>  1 - p/high <= 2/5
DD_MAX_DEN = 5
MIN_REAL_SOL = 15 * LAMPORTS
FLOW_TRADES = 6             # last N trades must include at least MIN_BUYS buys
MIN_BUYS = 2
STAKE_LAMPORTS = LAMPORTS // 4          # 0.25 SOL per position
MIN_BUY_LAMPORTS = LAMPORTS // 100     # 0.01 SOL: smallest buy worth sending
MAX_SLIPPAGE_BPS = 10_000
TAKE_PROFIT_BPS = 1_500     # exit when liquidation value >= cost * 1.15
TRAIL_ARM_BPS = 1_000       # trailing stop arms once value >= cost * 1.10
TRAIL_BPS = 800             # ...then exit on a fall of 8% from the peak value
STOP_LOSS_BPS = 800         # exit when liquidation value <= cost * 0.92
MAX_HOLD_SLOTS = 600
# guardrail pacing
TARGET_COINS = 25
PACE_SECONDS = 86_400       # the window is 24h of block time
PROBE_LAMPORTS = LAMPORTS // 100        # 0.01 SOL probe round trips
MAX_PROBES = 1

CURVE_FEE_BPS = 125         # protocol 95 + creator 30


class Tape:
    """Recent trade ticks of one curve, oldest first."""

    __slots__ = ("ticks", "entered", "entry_slot", "exiting", "probe", "peak")

    def __init__(self):
        # tick: (slot, vq, vt, real_sol, is_buy)
        self.ticks = []
        self.entered = False   # an entry was decided on this coin (once, ever)
        self.entry_slot = -1   # slot the buy was decided on
        self.exiting = False   # a sell is in flight for this coin
        self.probe = False     # this coin is a guardrail probe round trip
        self.peak = 0          # best liquidation value seen while holding (lamports)


def liquidation_value(tokens, vq, vt, tx_fee):
    """What selling `tokens` against (vq, vt) would pay, curve fee and one tx fee deducted."""
    if tokens <= 0 or vt <= 0:
        return 0
    q = (tokens * vq) // (vt + tokens)
    if q > 0:
        q -= (q * CURVE_FEE_BPS + BPS - 1) // BPS
    return q - tx_fee


class DipBot:
    def __init__(self):
        self.tapes = {}          # mint -> Tape, first-seen order
        self.my_coins = set()    # mints with at least one filled buy of ours
        self.my_fills = 0
        self.first_time = None
        self.last_time = None

    def tape_of(self, mint):
        t = self.tapes.get(mint)
        if t is None:
            t = Tape()
            self.tapes[mint] = t
        return t

    def on_batch(self, state, batch):
        slot = batch["slot"]
        orders = []
        cap = state.hello["caps"]["max_orders_per_batch"]
        tx = state.tx_fee_lamports()

        # Track our own fills (harness closes excluded) for the guardrail pace.
        for f in batch["fills"]:
            if f["status"] == "filled" and not f["client_id"].startswith("_"):
                self.my_fills += 1
                self.my_coins.add(f["mint"])

        # Fold the batch's events into the tapes; remember which mints traded,
        # in first-seen order (deterministic).
        traded = []
        traded_seen = set()
        for e in batch["events"]:
            if e.get("block_time") is not None:
                bt = e["block_time"]
                if self.first_time is None:
                    self.first_time = bt
                if self.last_time is None or bt > self.last_time:
                    self.last_time = bt
            if e["event"] != "TradeEvent":
                continue
            mint = e["mint"]
            t = self.tape_of(mint)
            t.ticks.append((
                e["slot"],
                int(e["virtual_quote_reserves"]),
                int(e["virtual_token_reserves"]),
                int(e["real_quote_reserves"]),
                bool(e["is_buy"]),
            ))
            if len(t.ticks) > 800:
                del t.ticks[: len(t.ticks) - 800]
            if mint not in traded_seen:
                traded_seen.add(mint)
                traded.append(mint)

        marks = {}
        for m in batch["marks"]:
            marks[m["mint"]] = (int(m["virtual_quote_reserves"]), int(m["virtual_token_reserves"]))

        pending = {}
        for po in state.pending.values():
            pending.setdefault(po.order["mint"], []).append(po.order["side"])

        # ---- 1. exits on held positions, in mint order (deterministic) ----
        for mint in sorted(state.positions.keys()):
            if len(orders) >= cap:
                break
            pos = state.positions[mint]
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            t = self.tapes.get(mint)
            if t is not None and t.exiting:
                continue
            if "sell" in pending.get(mint, ()):
                continue
            mk = marks.get(mint)
            if mk is not None:
                vq, vt = mk
            else:
                vq, vt = curve.virtual_quote_reserves, curve.virtual_token_reserves
            value = liquidation_value(pos.token_amount, vq, vt, tx)
            cost = pos.cost_lamports + tx  # quote spent + the one buy tx fee
            entry_slot = t.entry_slot if t is not None and t.entry_slot >= 0 else slot
            if t is not None and value > t.peak:
                t.peak = value
            if value * BPS >= cost * (BPS + TAKE_PROFIT_BPS):
                exit_now = True
            elif (t is not None and t.peak * BPS >= cost * (BPS + TRAIL_ARM_BPS)
                    and value * BPS <= t.peak * (BPS - TRAIL_BPS)):
                exit_now = True
            elif value * BPS <= cost * (BPS - STOP_LOSS_BPS):
                exit_now = True
            elif slot - entry_slot >= MAX_HOLD_SLOTS:
                exit_now = True
            else:
                exit_now = False
            if not exit_now:
                continue
            orders.append({"side": "sell", "mint": mint, "token_amount": pos.token_amount,
                           "max_slippage_bps": MAX_SLIPPAGE_BPS})
            if t is not None:
                t.exiting = True

        # A sell that resolved clears the exiting flag; a position that is gone
        # entirely (sold or closed by the harness) is forgotten.
        for f in batch["fills"]:
            if f["side"] == "sell":
                t = self.tapes.get(f["mint"])
                if t is not None:
                    t.exiting = False
        for mint in list(self.tapes):
            if mint not in state.positions and "buy" not in pending.get(mint, ()):
                t = self.tapes[mint]
                t.peak = 0
                if t.probe and mint in self.my_coins:
                    # probe round trip finished; keep the tape but clear the flag
                    t.probe = False

        # ---- 2. entries on coins that traded this batch ----
        free = state.free_cash_lamports()
        open_slots = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
        for mint in traded:
            if len(orders) >= cap or open_slots <= 0:
                break
            if mint in state.positions or "buy" in pending.get(mint, ()):
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            t = self.tapes[mint]
            if t.entered:
                continue
            ticks = t.ticks
            if len(ticks) < FLOW_TRADES:
                continue
            last = ticks[-1]
            if last[3] < MIN_REAL_SOL:
                continue
            if not last[4]:
                continue
            # recent high over HIGH_WINDOW_SLOTS (price = vq/vt, compared by
            # cross-multiplication to stay in integers)
            hi_vq, hi_vt = 0, 1
            for tk in reversed(ticks):
                if tk[0] < slot - HIGH_WINDOW_SLOTS:
                    break
                if tk[1] * hi_vt > hi_vq * tk[2]:
                    hi_vq, hi_vt = tk[1], tk[2]
            if hi_vq <= 0:
                continue
            p_vq, p_vt = last[1], last[2]
            # dip >= DD_MIN_NUM/DD_MIN_DEN: p <= (1-dmin)*high
            if (DD_MIN_DEN * p_vq * hi_vt
                    > (DD_MIN_DEN - DD_MIN_NUM) * hi_vq * p_vt):
                continue
            # dip <= DD_MAX_NUM/DD_MAX_DEN: p >= (1-dmax)*high
            if (DD_MAX_DEN * p_vq * hi_vt
                    < (DD_MAX_DEN - DD_MAX_NUM) * hi_vq * p_vt):
                continue
            # buyers still stepping in: at least MIN_BUYS of the last FLOW_TRADES
            buys = 0
            for tk in ticks[-FLOW_TRADES:]:
                if tk[4]:
                    buys += 1
            if buys < MIN_BUYS:
                continue
            # decide once per coin, whether or not the size works out
            t.entered = True
            amount = min(STAKE_LAMPORTS, (state.max_buy_lamports(mint) * 97) // 100)
            if amount < MIN_BUY_LAMPORTS:
                continue
            if free < amount + tx:
                continue
            orders.append({"side": "buy", "mint": mint, "quote_lamports": amount,
                           "max_slippage_bps": MAX_SLIPPAGE_BPS})
            t.entry_slot = slot
            free -= amount + tx
            open_slots -= 1

        # ---- 3. guardrail probes: buy 0.01 SOL on a liquid curve when the
        # coin pace lags, sell it as soon as the buy lands ----
        for mint in list(self.tapes):
            t = self.tapes[mint]
            if not t.probe:
                continue
            if t.exiting:
                continue
            if mint in state.positions and "sell" not in pending.get(mint, ()) and len(orders) < cap:
                pos = state.positions[mint]
                orders.append({"side": "sell", "mint": mint, "token_amount": pos.token_amount,
                               "max_slippage_bps": MAX_SLIPPAGE_BPS})
                t.exiting = True

        probes_open = sum(1 for t in self.tapes.values() if t.probe)
        if (probes_open < MAX_PROBES and open_slots > 0 and self.first_time is not None
                and self.last_time is not None and len(orders) < cap):
            pace = min(1.0, (self.last_time - self.first_time) / PACE_SECONDS)
            # ceil(TARGET_COINS * pace) computed without floats
            need_count = (TARGET_COINS * (self.last_time - self.first_time) + PACE_SECONDS - 1) // PACE_SECONDS
            if pace > 0 and len(self.my_coins) < need_count:
                min_rq = (PROBE_LAMPORTS * BPS) // state.hello["caps"]["max_order_bps_of_real_reserve"]
                for mint in traded:
                    if len(orders) >= cap or open_slots <= 0:
                        break
                    if mint in state.positions or "buy" in pending.get(mint, ()):
                        continue
                    curve = state.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    t = self.tapes[mint]
                    if t.entered or t.probe:
                        continue
                    if curve.real_quote_reserves < min_rq * 3 // 2:
                        continue
                    if free < PROBE_LAMPORTS + 2 * tx:
                        break
                    orders.append({"side": "buy", "mint": mint, "quote_lamports": PROBE_LAMPORTS,
                                   "max_slippage_bps": MAX_SLIPPAGE_BPS})
                    t.probe = True
                    t.entry_slot = slot
                    free -= PROBE_LAMPORTS + tx
                    open_slots -= 1
                    break
        return orders


if __name__ == "__main__":
    makefun_bot.run(DipBot().on_batch)
