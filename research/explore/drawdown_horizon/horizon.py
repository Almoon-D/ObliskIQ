"""How the probability of a drawdown at least as deep as a limit grows with the horizon, for
portfolios described only by assumed parameters (exploration, DECISIONS D64).

No market data are read: every number comes from the assumptions below, so the output
illustrates a mechanism and estimates nothing. Each profile has an annual volatility and an
arithmetic return of cash plus an assumed reward per unit of volatility. Monthly log returns are
drawn independently, either normal or Student-t (scaled to the same variance) for fat tails.
Neither draw has volatility clustering or crashes that persist across months, which real markets
have and which make deep drawdowns more likely, so the probabilities below err low.

The drawdown is measured on month-end values from the running peak, the start included.

Usage: python -I research/explore/drawdown_horizon/horizon.py
"""

import numpy as np

SEED = 20261017
PATHS = 20_000
LIMIT = 0.30
HORIZONS_YEARS = (10, 20, 30, 40, 50)
#: The assumed cash return and reward per unit of volatility, a year.
CASH = 0.015
REWARD_PER_VOLATILITY = 0.3
VOLATILITIES = (0.06, 0.10, 0.15, 0.20)
#: Degrees of freedom of the fat-tailed draw.
STUDENT_DF = 4
MONTHS_PER_YEAR = 12


def monthly_log_returns(
    rng: np.random.Generator, volatility: float, months: int, tails: str
) -> np.ndarray:
    arithmetic = CASH + REWARD_PER_VOLATILITY * volatility
    drift = (arithmetic - volatility**2 / 2) / MONTHS_PER_YEAR
    scale = volatility / np.sqrt(MONTHS_PER_YEAR)
    if tails == "normal":
        shocks = rng.standard_normal((PATHS, months))
    else:
        shocks = rng.standard_t(STUDENT_DF, (PATHS, months)) / np.sqrt(
            STUDENT_DF / (STUDENT_DF - 2)
        )
    return drift + scale * shocks


def breach_by_horizon(log_returns: np.ndarray) -> list[float]:
    wealth = np.cumsum(log_returns, axis=1)
    peak = np.maximum.accumulate(np.maximum(wealth, 0.0), axis=1)
    deepest = np.maximum.accumulate(1.0 - np.exp(wealth - peak), axis=1)
    return [
        float(np.mean(deepest[:, years * MONTHS_PER_YEAR - 1] >= LIMIT)) for years in HORIZONS_YEARS
    ]


def main() -> None:
    rng = np.random.default_rng(SEED)
    months = max(HORIZONS_YEARS) * MONTHS_PER_YEAR
    header = " | ".join(f"{years} years" for years in HORIZONS_YEARS)
    print(f"| Volatility | Assumed log growth | Tails | {header} |")
    print("|---" * (3 + len(HORIZONS_YEARS)) + "|")
    for volatility in VOLATILITIES:
        growth = CASH + REWARD_PER_VOLATILITY * volatility - volatility**2 / 2
        for tails in ("normal", "student"):
            shares = breach_by_horizon(monthly_log_returns(rng, volatility, months, tails))
            cells = " | ".join(f"{share:.1%}" for share in shares)
            print(f"| {volatility:.0%} | {growth:.2%} | {tails} | {cells} |")


if __name__ == "__main__":
    main()
