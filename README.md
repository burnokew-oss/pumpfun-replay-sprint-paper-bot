# Pump.fun Replay Sprint — Paper Bot

[![make.fun](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/burnokew-oss/pumpfun-replay-sprint-paper-bot/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/burnokew-oss/pumpfun-replay-sprint-paper-bot/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

A paper-trading bot for the [Pump.fun Replay Sprint](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122) bounty on [make.fun](https://make.fun). It trades 10 SOL of paper money on a 24-hour replay of pump.fun bonding-curve activity, speaking the `agent-fun/replay/v2` protocol (NDJSON over stdin/stdout).

## Strategy: dip-bounce

Most pump.fun coins pump in their create slot and then bleed out; chasing
momentum and buying fresh launches both lose money after fees on the dev
window. The one repeatable edge found in the dev data is the **bounce after
a sharp dip on a liquid curve**:

1. **Watch** every curve's trade tape.
2. **Enter once per coin**, the first time it trades 20–40% below its own
   high of the last 300 slots, while it still holds ≥ 15 SOL of real
   reserves and buyers are still stepping in (≥ 2 of the last 6 trades are
   buys and the latest trade is a buy). Later dips on the same coin keep
   falling in the dev data, so re-entries are skipped.
3. **Buy** 0.25 SOL (capped at 97% of the harness's 1%-of-real-reserve
   order cap, at least 0.01 SOL).
4. **Exit whole** on the first of: liquidation value ≥ cost + 15%
   (take profit), a trailing stop once value ≥ cost + 10% (exit on an 8%
   fall from the peak), ≤ cost − 8% (stop loss), or 600 slots held.
   Liquidation value is computed from the harness's marks with the 1.25%
   curve fee and one tx fee deducted.
5. **Guardrail probes**: if the bot's filled coins lag a pace of 25 coins
   over the window, it opens one tiny 0.01 SOL probe round trip on the
   most liquid curve that traded, so the run still clears the
   ≥ 20-coins / ≥ 0.01-SOL round-trip bar.

Fees are ~1.25% per side plus 105k lamports per order, so a round trip
needs roughly a 3% move to break even.

## Running

Pure Python 3.13 standard library, no dependencies:

```sh
python3 main.py   # reads hello/batch/done on stdin, writes one reply per batch on stdout
```

`main.py` vendors the kit's `agent-fun/replay/v2` shim at
`vendor/makefun_bot.py` and adds it to `sys.path` itself (the grader runs
`python3 -P -s /bot/main.py`).

## Grading locally

```sh
node <kit>/cli/makefun.mjs grade-local --kit <kit> --bot . --driver process
```

## License

MIT — see [LICENSE](LICENSE).
