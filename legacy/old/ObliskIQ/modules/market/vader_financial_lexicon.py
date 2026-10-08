"""
VADER Financial Lexicon Extension
=================================
Extends VADER's default lexicon with financial-specific terms.

VADER's base lexicon is excellent for general text but lacks:
- Trading slang (stonks, tendies, diamond hands)
- Market terminology (bullish, bearish, rally, crash)
- Financial sentiment modifiers (overbought, oversold)

Usage:
    from ObliskIQ.modules.market.vader_financial_lexicon import get_financial_vader

    vader = get_financial_vader()
    score = vader.polarity_scores("AAPL is looking bullish, expecting a rally")

Lexicon values follow VADER conventions:
- Positive: +0.5 to +3.5 (higher = stronger positive)
- Negative: -0.5 to -3.5 (lower = stronger negative)
- Neutral: 0

References:
- VADER paper: Hutto & Gilbert (2014)
- Financial sentiment: Loughran & McDonald (2011)
"""

from typing import Dict
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import logging

logger = logging.getLogger(__name__)


# Financial lexicon extension
# Values calibrated based on Loughran-McDonald Financial Sentiment Dictionary
# and empirical testing on financial news/social media

FINANCIAL_LEXICON: Dict[str, float] = {
    # ========================================
    # BULLISH TERMS (Positive sentiment)
    # ========================================

    # Strong bullish (+2.5 to +3.5)
    "bullish": 2.5,
    "moon": 3.0,
    "mooning": 3.0,
    "to the moon": 3.5,
    "rocket": 2.5,
    "skyrocket": 3.0,
    "skyrocketing": 3.0,
    "parabolic": 2.5,
    "breakout": 2.5,
    "squeeze": 2.0,  # Context-dependent but usually positive
    "short squeeze": 3.0,
    "gamma squeeze": 3.0,

    # Moderate bullish (+1.5 to +2.4)
    "rally": 2.0,
    "rallying": 2.0,
    "surge": 2.0,
    "surging": 2.0,
    "soar": 2.2,
    "soaring": 2.2,
    "boom": 2.0,
    "booming": 2.0,
    "outperform": 1.8,
    "outperforming": 1.8,
    "beat": 1.5,
    "beats": 1.5,
    "beating": 1.5,
    "upgrade": 2.0,
    "upgraded": 2.0,
    "buy": 1.5,
    "strong buy": 2.5,
    "accumulate": 1.5,
    "overweight": 1.5,

    # Mild bullish (+0.5 to +1.4)
    "gain": 1.0,
    "gains": 1.0,
    "gaining": 1.0,
    "rise": 1.0,
    "rising": 1.0,
    "climb": 1.0,
    "climbing": 1.0,
    "recovery": 1.2,
    "recovering": 1.2,
    "rebound": 1.2,
    "rebounding": 1.2,
    "upside": 1.0,
    "uptrend": 1.2,
    "bullrun": 2.0,
    "bull run": 2.0,
    "bull market": 1.5,
    "green": 0.8,
    "support": 0.5,  # Technical term, mildly positive

    # Reddit/WSB slang (bullish)
    "stonks": 1.5,  # Ironic but generally used positively
    "tendies": 2.0,  # Profits
    "diamond hands": 2.0,  # Holding conviction
    "ape": 1.0,  # WSB term for retail investor
    "yolo": 1.5,  # Bullish conviction
    "btfd": 2.0,  # Buy the f* dip
    "buy the dip": 1.5,

    # ========================================
    # BEARISH TERMS (Negative sentiment)
    # ========================================

    # Strong bearish (-2.5 to -3.5)
    "bearish": -2.5,
    "crash": -3.0,
    "crashing": -3.0,
    "collapse": -3.0,
    "collapsing": -3.0,
    "plunge": -2.8,
    "plunging": -2.8,
    "tank": -2.5,
    "tanking": -2.5,
    "dump": -2.5,
    "dumping": -2.5,
    "capitulation": -3.0,
    "panic": -2.5,
    "panic selling": -3.0,
    "bloodbath": -3.5,
    "carnage": -3.0,

    # Moderate bearish (-1.5 to -2.4)
    "sell": -1.5,
    "selling": -1.5,
    "sell-off": -2.0,
    "selloff": -2.0,
    "drop": -1.8,
    "dropping": -1.8,
    "fall": -1.5,
    "falling": -1.5,
    "decline": -1.5,
    "declining": -1.5,
    "downgrade": -2.0,
    "downgraded": -2.0,
    "underperform": -1.8,
    "underperforming": -1.8,
    "miss": -1.5,
    "misses": -1.5,
    "missed": -1.5,
    "underweight": -1.5,
    "reduce": -1.5,

    # Mild bearish (-0.5 to -1.4)
    "loss": -1.2,
    "losses": -1.2,
    "losing": -1.2,
    "slip": -0.8,
    "slipping": -0.8,
    "slide": -1.0,
    "sliding": -1.0,
    "weak": -1.0,
    "weakness": -1.0,
    "downside": -1.0,
    "downtrend": -1.2,
    "bear market": -1.5,
    "red": -0.8,
    "resistance": -0.5,  # Technical term, mildly negative
    "correction": -1.0,

    # Reddit/WSB slang (bearish)
    "rug pull": -3.0,
    "rugpull": -3.0,
    "bagholder": -2.0,
    "bagholding": -2.0,
    "rekt": -2.5,
    "guh": -2.5,  # Loss sound
    "paper hands": -1.5,

    # ========================================
    # RISK/UNCERTAINTY TERMS (Negative)
    # ========================================
    "risk": -0.8,
    "risky": -1.0,
    "volatile": -0.8,
    "volatility": -0.5,  # Can be opportunity
    "uncertainty": -1.0,
    "uncertain": -1.0,
    "fear": -1.5,
    "worried": -1.2,
    "concern": -1.0,
    "concerns": -1.0,
    "warning": -1.5,
    "caution": -1.0,
    "bubble": -1.5,
    "overvalued": -1.2,
    "overbought": -0.8,
    "oversold": 0.5,  # Contrarian opportunity
    "recession": -2.0,
    "inflation": -1.0,  # Context-dependent
    "stagflation": -2.0,
    "default": -2.5,
    "bankruptcy": -3.0,
    "layoff": -1.5,
    "layoffs": -1.5,

    # ========================================
    # NEUTRAL/TECHNICAL TERMS
    # ========================================
    "hold": 0.0,
    "neutral": 0.0,
    "sideways": 0.0,
    "consolidation": 0.0,
    "consolidating": 0.0,
    "flat": 0.0,
    "range-bound": 0.0,
    "mixed": 0.0,

    # ========================================
    # POSITIVE FUNDAMENTALS
    # ========================================
    "profit": 1.5,
    "profits": 1.5,
    "profitable": 1.5,
    "earnings": 0.5,  # Neutral until qualified
    "revenue": 0.3,
    "growth": 1.2,
    "growing": 1.2,
    "dividend": 1.0,
    "buyback": 1.2,
    "expansion": 1.0,
    "expanding": 1.0,
    "acquisition": 0.8,
    "merger": 0.5,
    "innovation": 1.0,
    "breakthrough": 1.5,

    # ========================================
    # NEGATIVE FUNDAMENTALS
    # ========================================
    "debt": -0.8,
    "leverage": -0.5,
    "dilution": -1.5,
    "diluting": -1.5,
    "writedown": -1.5,
    "impairment": -1.5,
    "restructuring": -1.0,
    "investigation": -1.5,
    "lawsuit": -1.2,
    "fraud": -2.5,
    "scandal": -2.0,
}


def get_financial_vader() -> SentimentIntensityAnalyzer:
    """
    Get VADER analyzer with financial lexicon extension.

    Returns:
        SentimentIntensityAnalyzer with financial terms added
    """
    vader = SentimentIntensityAnalyzer()

    # Add financial lexicon
    vader.lexicon.update(FINANCIAL_LEXICON)

    logger.info(f"VADER initialized with {len(FINANCIAL_LEXICON)} financial terms")

    return vader


def get_lexicon_stats() -> Dict[str, int]:
    """
    Get statistics about the financial lexicon.

    Returns:
        Dict with positive/negative/neutral counts
    """
    positive = sum(1 for v in FINANCIAL_LEXICON.values() if v > 0)
    negative = sum(1 for v in FINANCIAL_LEXICON.values() if v < 0)
    neutral = sum(1 for v in FINANCIAL_LEXICON.values() if v == 0)

    return {
        "total_terms": len(FINANCIAL_LEXICON),
        "positive_terms": positive,
        "negative_terms": negative,
        "neutral_terms": neutral,
        "avg_positive_score": sum(v for v in FINANCIAL_LEXICON.values() if v > 0) / max(positive, 1),
        "avg_negative_score": sum(v for v in FINANCIAL_LEXICON.values() if v < 0) / max(negative, 1),
    }


# Quick test
if __name__ == "__main__":
    vader = get_financial_vader()

    test_texts = [
        "AAPL is looking bullish, expecting a rally to new highs",
        "Market crash incoming, this is going to be a bloodbath",
        "Diamond hands holding through the dip, tendies coming soon",
        "Bearish on this stock, downgrade to sell, expect further decline",
        "Stock is consolidating, trading sideways in a range",
    ]

    print("\nFinancial VADER Test Results:")
    print("=" * 60)
    for text in test_texts:
        score = vader.polarity_scores(text)
        print(f"\nText: {text[:50]}...")
        print(f"Compound: {score['compound']:.3f} | Pos: {score['pos']:.3f} | Neg: {score['neg']:.3f}")

    print("\n" + "=" * 60)
    stats = get_lexicon_stats()
    print(f"Lexicon Stats: {stats}")
