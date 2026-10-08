"""
Tests for VADER Financial Lexicon Extension
============================================

Tests the financial-specific terms added to VADER's default lexicon.
"""

import pytest

try:
    from ObliskIQ.modules.market.vader_financial_lexicon import (
        get_financial_vader,
        get_lexicon_stats,
        FINANCIAL_LEXICON,
    )
except ImportError:
    pytest.skip("vader_financial_lexicon not available", allow_module_level=True)

pytestmark = pytest.mark.unit


class TestFinancialLexicon:
    """Test financial lexicon content"""

    def test_lexicon_has_bullish_terms(self):
        """Lexicon should have bullish terms"""
        bullish_terms = ["bullish", "rally", "moon", "breakout"]
        for term in bullish_terms:
            assert term in FINANCIAL_LEXICON, f"Missing bullish term: {term}"
            assert FINANCIAL_LEXICON[term] > 0, f"{term} should have positive score"

    def test_lexicon_has_bearish_terms(self):
        """Lexicon should have bearish terms"""
        bearish_terms = ["bearish", "crash", "dump", "rug pull"]
        for term in bearish_terms:
            assert term in FINANCIAL_LEXICON, f"Missing bearish term: {term}"
            assert FINANCIAL_LEXICON[term] < 0, f"{term} should have negative score"

    def test_lexicon_has_wsb_slang(self):
        """Lexicon should have WSB slang"""
        wsb_terms = ["stonks", "tendies", "diamond hands", "ape"]
        for term in wsb_terms:
            assert term in FINANCIAL_LEXICON, f"Missing WSB term: {term}"

    def test_lexicon_value_ranges(self):
        """All values should be in valid VADER range"""
        for term, value in FINANCIAL_LEXICON.items():
            assert -4.0 <= value <= 4.0, f"{term} has invalid value {value}"

    def test_lexicon_has_fundamental_terms(self):
        """Lexicon should have fundamental analysis terms"""
        positive_fundamentals = ["profit", "growth", "dividend", "buyback"]
        negative_fundamentals = ["debt", "dilution", "fraud", "bankruptcy"]

        for term in positive_fundamentals:
            assert term in FINANCIAL_LEXICON, f"Missing positive fundamental: {term}"
            assert FINANCIAL_LEXICON[term] > 0

        for term in negative_fundamentals:
            assert term in FINANCIAL_LEXICON, f"Missing negative fundamental: {term}"
            assert FINANCIAL_LEXICON[term] < 0

    def test_lexicon_has_neutral_terms(self):
        """Lexicon should have neutral technical terms"""
        neutral_terms = ["hold", "neutral", "sideways", "consolidation"]
        for term in neutral_terms:
            assert term in FINANCIAL_LEXICON, f"Missing neutral term: {term}"
            assert FINANCIAL_LEXICON[term] == 0.0, f"{term} should be neutral (0.0)"


class TestFinancialVADER:
    """Test enhanced VADER analyzer"""

    @pytest.fixture
    def vader(self):
        return get_financial_vader()

    def test_bullish_text_positive(self, vader):
        """Bullish text should score positive"""
        text = "This stock is extremely bullish, expecting a major rally"
        score = vader.polarity_scores(text)["compound"]
        assert score > 0.3, f"Expected positive score, got {score}"

    def test_bearish_text_negative(self, vader):
        """Bearish text should score negative"""
        text = "Market crash incoming, this is going to be a bloodbath"
        score = vader.polarity_scores(text)["compound"]
        assert score < -0.3, f"Expected negative score, got {score}"

    def test_wsb_bullish_positive(self, vader):
        """WSB bullish slang should score positive"""
        text = "Diamond hands holding, tendies incoming to the moon"
        score = vader.polarity_scores(text)["compound"]
        assert score > 0.2, f"Expected positive score, got {score}"

    def test_wsb_bearish_negative(self, vader):
        """WSB bearish slang should score negative"""
        text = "Total rug pull, all bagholders got rekt"
        score = vader.polarity_scores(text)["compound"]
        assert score < -0.3, f"Expected negative score, got {score}"

    def test_neutral_text_neutral(self, vader):
        """Neutral text should score near zero"""
        text = "Stock is consolidating in a sideways range"
        score = vader.polarity_scores(text)["compound"]
        assert -0.3 < score < 0.3, f"Expected neutral score, got {score}"

    def test_mixed_text(self, vader):
        """Mixed sentiment should surface both positive and negative components.

        Use an additive construction ("bearish crash, bullish recovery") rather
        than a contrastive one ("Despite the crash ... bullish ..."): VADER
        intentionally resolves contrastive clauses toward the dominant clause,
        which zeroes the minority bucket and is not what this test is checking.
        """
        text = "bearish crash, bullish recovery"
        score = vader.polarity_scores(text)
        # Should have both positive and negative components
        assert score["pos"] > 0
        assert score["neg"] > 0

    def test_financial_term_detection(self, vader):
        """Specific financial terms should be detected"""
        # Test that financial terms actually change the score
        base_text = "AAPL stock"
        base_score = vader.polarity_scores(base_text)["compound"]

        bullish_text = "AAPL stock bullish rally"
        bullish_score = vader.polarity_scores(bullish_text)["compound"]

        bearish_text = "AAPL stock bearish crash"
        bearish_score = vader.polarity_scores(bearish_text)["compound"]

        assert bullish_score > base_score, "Bullish terms should increase score"
        assert bearish_score < base_score, "Bearish terms should decrease score"

    def test_upgrade_downgrade_asymmetry(self, vader):
        """Upgrade and downgrade should have opposite effects"""
        upgrade_text = "Analyst upgrade to buy"
        downgrade_text = "Analyst downgrade to sell"

        upgrade_score = vader.polarity_scores(upgrade_text)["compound"]
        downgrade_score = vader.polarity_scores(downgrade_text)["compound"]

        assert upgrade_score > 0, "Upgrade should be positive"
        assert downgrade_score < 0, "Downgrade should be negative"

    def test_reddit_specific_slang(self, vader):
        """Reddit-specific slang should be recognized"""
        # YOLO = bullish conviction
        yolo_text = "YOLO into TSLA"
        yolo_score = vader.polarity_scores(yolo_text)["compound"]
        assert yolo_score > 0, "YOLO should be positive"

        # Ape = retail investor (positive in WSB context)
        ape_text = "Ape strong together"
        ape_score = vader.polarity_scores(ape_text)["compound"]
        assert ape_score > 0, "Ape should be positive"

    def test_contrarian_signal(self, vader):
        """Oversold should be mildly positive (contrarian opportunity)"""
        text = "RSI shows oversold conditions"
        score = vader.polarity_scores(text)["compound"]
        # Oversold is calibrated as +0.5 (buying opportunity)
        assert score >= 0, "Oversold should not be negative"


class TestLexiconStats:
    """Test lexicon statistics"""

    def test_stats_structure(self):
        """Stats should have expected keys"""
        stats = get_lexicon_stats()
        expected_keys = [
            "total_terms",
            "positive_terms",
            "negative_terms",
            "neutral_terms",
            "avg_positive_score",
            "avg_negative_score",
        ]
        for key in expected_keys:
            assert key in stats, f"Missing key: {key}"

    def test_stats_values(self):
        """Stats values should be reasonable"""
        stats = get_lexicon_stats()
        assert stats["total_terms"] > 100, "Should have 100+ terms"
        assert stats["positive_terms"] > 30, "Should have 30+ positive terms"
        assert stats["negative_terms"] > 30, "Should have 30+ negative terms"

    def test_stats_math(self):
        """Stats should add up correctly"""
        stats = get_lexicon_stats()
        total = stats["positive_terms"] + stats["negative_terms"] + stats["neutral_terms"]
        assert total == stats["total_terms"], "Term counts should sum to total"


class TestVADERIntegrationWithFinancial:
    """Test that financial VADER works with sentiment modules"""

    def test_vader_is_enhanced(self):
        """Financial VADER should have more terms than base VADER"""
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        base_vader = SentimentIntensityAnalyzer()
        financial_vader = get_financial_vader()

        # Financial vader should have all base terms plus financial terms
        assert len(financial_vader.lexicon) > len(base_vader.lexicon)

        # Specifically check for financial terms
        assert "bullish" in financial_vader.lexicon
        assert "bullish" not in base_vader.lexicon

    def test_backward_compatibility(self):
        """Financial VADER should still work on general text"""
        vader = get_financial_vader()

        # Standard positive text (uses base VADER words)
        text = "This is great and wonderful"
        score = vader.polarity_scores(text)["compound"]
        assert score > 0, "Should handle standard positive words"

        # Standard negative text
        text = "This is terrible and horrible"
        score = vader.polarity_scores(text)["compound"]
        assert score < 0, "Should handle standard negative words"

    def test_negation_still_works(self):
        """Negation handling should still work with financial terms"""
        vader = get_financial_vader()

        positive = "The stock is bullish"
        negated = "The stock is not bullish"

        pos_score = vader.polarity_scores(positive)["compound"]
        neg_score = vader.polarity_scores(negated)["compound"]

        assert pos_score > 0, "Positive should be positive"
        assert neg_score < pos_score, "Negation should reduce score"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
