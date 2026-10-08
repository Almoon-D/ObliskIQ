# Paper Trading Gate

**Version:** 6.16.0
**Status:** ENFORCED in code

The gate between **simulated** (paper) and **real** (IBKR write path) execution. Live order placement is
**BLOCKED** until paper trading has **PASSED** every criterion below, with the evidence each one needs.

> George Soros: "90 days minimum with real-time data, fake execution before real capital."
> Ray Dalio: "Risk constraints always enforced."

## What a pass means — and does not

The gate protects live capital from a book that is unstable, deep in drawdown, or demonstrably worse than the
world index. **It does not prove the AI has skill.** A daily Sharpe over 90–126 days has a standard error of
roughly 2 (a zero-skill strategy "passes" Sharpe > 0.5 about 40% of the time), and a coin flip clears
"win rate > 50%" 34–44% of the time at n = 30. Whether the AI adds information is decided by the AI
scoreboard's weekly forecast panel (`docs/AI_SCOREBOARD.md`), not by this gate. The report prints the Sharpe's
~95% interval so the operator sees how wide the estimate is.

**What is being validated.** The paper book runs the **AI pipeline** — Phase 2/4 decisions through the
deterministic gauntlet (ContinuityGuard, Kelly clamp, RiskEnforcer, stops). `strategy_engine_version` pins the
configured deterministic *baseline* engine and resets the clock when it changes; the baseline is not what is
traded. (Earlier versions labelled the gate as validating "the v1 engine".)

---

## The seven criteria

| # | Criterion | Rule | Source of truth |
|---|---|---|---|
| 1 | Sharpe ratio | `> 0.5` (annualised; risk-free rate **measured**) | `PaperTrader.MIN_SHARPE_RATIO` |
| 2 | Max drawdown | `< 20%` | `MAX_DRAWDOWN_PCT` |
| 3 | Win rate | `> 50%` of **closed positions**, judged only once ≥ 30 exist | `MIN_WIN_RATE` |
| 4 | Calendar days | `>= 90` since the first paper trade | `MIN_VALIDATION_DAYS` |
| 5 | Trading days | `>= 126` daily snapshots (~6 months) | `MIN_TRADING_DAYS` |
| 6 | Closed positions | `>= 30` round trips | `MIN_CLOSED_POSITIONS` |
| 7 | Excess vs **ACWI** | not demonstrably negative: the 95% block-bootstrap interval of the annualised mean daily excess return has an **upper bound above 0** | `PaperTradingGate._excess_criterion` |

The gate reuses the thresholds defined on `PaperTrader`, so the performance report and the gate can never
disagree.

### Verdict logic (`PaperTradingGate._overall_status`)

- **IN_PROGRESS** while any evidence requirement (calendar days, trading days, closed positions) is unmet. You
  cannot pass early, and a criterion that already reads `FAIL` mid-window cannot end the window early either.
- Once the evidence is in: **FAIL** if any criterion failed; **PASS** if all passed; **IN_PROGRESS** if something
  could not be measured (e.g. ACWI prices unavailable) — **never a pass on missing data**.

### How the numbers are computed (v6.16.0)

- **Risk-free rate** — the latest 3-month Treasury bill yield from FRED (`DGS3MO`). When FRED is unreachable a
  4% fallback constant is used and the report labels it (`fallback_constant`); the previous hard-coded 5% was
  neither measured nor labelled.
- **Sharpe** — mean daily excess return over `rf / 252`, divided by the *sample* standard deviation of daily
  returns, × √252. Its ~95% interval uses Lo's (2002) standard error `sqrt((1 + SR²/2) / n)`.
- **Closed positions** — round trips (first BUY to flat, net of costs), not per-ticker average legs: a name
  bought, sold and bought again is two positions; scaling in or out inside one holding is one; a partial sell
  does not close anything. Open positions are not evidence either way.
- **Excess vs ACWI** — strategy daily return minus ACWI's over the same snapshot dates, annualised, with a
  5-day moving-block bootstrap (seeded: the result does not change between two evaluations). SPY is still shown
  as a same-window total-return comparison for continuity, but it is the wrong yardstick for a global book.

---

## How the paper book behaves (v6.16.0)

The gate scores the paper book, so the book must behave like the book you would actually run:

- **Sizing is a share of NAV.** `position_size_pct` is the *target weight* of net asset value (cash + positions
  at their session marks) — what `RiskEnforcer` validated. A BUY on a name already held buys only the
  *shortfall* to that weight, so repeating a BUY can never stack past the position cap; an order larger than the
  cash left is cut, never overdrawn. (Before, sizing was a share of the *cash left*, so every later position was
  undersized.)
- **Stops and targets are enforced.** The AI's stop price (or `stop_loss_pct` applied to the actual fill) and
  target are persisted per open position (`paper_exit_levels`) and enforced at the **start of every paper
  session**, before the AI is asked: through the stop → SELL (`stop_loss`), at the target → SELL (`take_profit`).
  Exits fill at the price observed at that moment plus slippage, not at the level — a gap through a stop is a
  worse fill, as in life. The system only looks twice a day, so an intraday dip that recovers between runs is
  invisible (a documented limit). An unpriced position is skipped and retried next run; each held name is priced
  once per session. A top-up keeps the tighter stop.
- **The AI sees the real book.** Phase 4's prompt gets NAV, cash, per-position weight / P&L / age / stop /
  target, recent closed trades, and each prior commitment's current state.
- **The thesis ledger follows the book.** After exits, and again after new orders, ledger entries for positions
  the paper book does not hold are closed with the reason (`stop_loss`, `take_profit`, `no_paper_position`), so
  the AI is never told it holds — and must defend — a position that does not exist.
- **USD only.** A non-USD local listing (e.g. `7203.T`) is **refused and logged**, not valued as if yen were
  dollars. ADRs and US-listed ETFs are unaffected.

### Known limitations (stated, not hidden)

- **Fill price.** Orders fill at the last price observed when the session runs. The pre-open run therefore
  fills at the previous close, not today's open; there is no queue-to-open model. Overnight gaps are not
  reflected in entry prices.
- **Costs are slightly conservative.** `net_value` charges slippage on top of a fill price that already
  includes it (both sides), a few basis points per trade — the gate errs against passing, not toward it.
- **Intraday risk is unobserved** between the two daily looks (see stops above).
- **Single venue/currency.** USD listings only; no FX P&L.

---

## The 90-day clock

- **Starts** at the **first paper trade** (`PaperTrader.record_clock_start`). Calendar days, not trading days —
  the *trading-day* requirement (126 snapshots) is a separate, stricter criterion, so the two can no longer be
  confused.
- **Persists** across restarts and mid-day crashes — the start date and the strategy-engine version live in the
  `paper_gate_meta` SQLite table; on Actions the database travels on the `ci-state` branch.
- **Resets** on any **strategy-engine version change** (`config.system.strategy_engine_version`):
  `PaperTrader.check_and_handle_version_change()` wipes paper state and restarts the clock at the next first
  trade.

---

## How it runs (hands-free)

```
exit enforcement → AI sees the real book → pipeline → Phase 4 decisions → PaperTrader.execute_signal
                 → ledger reconcile → daily snapshot → gate status → daily digest / weekly report
```

```bash
python main.py auto --paper            # or: python main.py paper
python main.py paper --paper-capital 50000
python main.py paper --paper-reset     # wipe state, restart the clock
python main.py paper-status            # pure status query: every criterion, the Sharpe interval, excess vs ACWI
```

`reports/paper/paper_week_<YYYY>-W<week>.md` is written at most once per ISO week.

---

## The enforced block (not a comment)

Live trading is gated by **two** conditions, both required:

1. **Operator opt-in** — `config.system.live_trading_enabled` (`IBKR_LIVE_TRADING_ENABLED`, default `false`).
2. **Gate PASS** — every criterion above.

Enforced in code:

- `paper_gate.assert_live_trading_allowed()` raises `LiveTradingBlockedError` unless the gate reports `PASS`.
- `IBKRClient.assert_write_allowed()` checks the opt-in flag **and** calls it.
- `IBKRClient.place_order()` calls `assert_write_allowed()` first, so the write path cannot bypass the gate.

---

## Operator schedule note

- Run twice daily, Monday–Friday, as designed (the pre-close run is skipped on NYSE early-close days).
- Expect **~6 months** before the gate can rule (126 trading days, 30 closed positions).
- Do not set `IBKR_LIVE_TRADING_ENABLED=true` until `python main.py paper-status` reports `PASS` — and remember a
  pass is capital protection, not evidence of skill.
