"""
Enhanced RSS News Aggregator v3.3.3
POC (B1): Regional Coverage with Sectoral Classification

NEW FEATURES:
- Regional RSS sources (NA, Europe, Asia)
- Automatic regional classification
- Sectoral classification from content
- Feeds data into SentimentMatrix
- Multi-language support (Phase A)
- **v3.3.3: VADER Sentiment** (better financial news analysis)

USAGE:
    from modules.market.rss_news_enhanced import RSSNewsEnhanced

    rss = RSSNewsEnhanced(sentiment_matrix=matrix)

    # Fetch regional news
    result = await rss.fetch_regional_news()

    # Get specific region
    result = await rss.fetch_news_by_region(Region.EUROPE)
"""

import feedparser
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
import re
import logging

# Import financial-enhanced VADER
from ObliskIQ.modules.market.vader_financial_lexicon import get_financial_vader

# Import regional/sectoral components
from ObliskIQ.modules.market.regional_sectoral_config import (
    Region,
    regional_sectoral_config,
)
from ObliskIQ.modules.market.sentiment_matrix import SentimentMatrix

logger = logging.getLogger(__name__)


class RSSNewsEnhanced:
    """
    Enhanced RSS news aggregator with regional/sectoral classification

    POC Version (B1):
    - 3 regions: NA, Europe, Asia
    - 6 sectors via keyword classification
    - Feeds SentimentMatrix automatically
    """

    def __init__(
        self, sentiment_matrix: Optional[SentimentMatrix] = None, auto_feed_matrix: bool = True
    ):
        """
        Initialize enhanced RSS news aggregator

        Args:
            sentiment_matrix: Optional SentimentMatrix to feed data into
            auto_feed_matrix: Whether to automatically add sentiments to matrix
        """
        self.sentiment_matrix = sentiment_matrix
        self.auto_feed_matrix = auto_feed_matrix
        self.config = regional_sectoral_config

        # v3.3.4: Initialize VADER sentiment analyzer with financial lexicon
        # Financial lexicon adds: bullish, bearish, rally, crash, etc.
        self.vader = get_financial_vader()

        # Regional RSS sources from config
        self.feeds_by_region = {
            region: self.config.get_rss_sources(region) for region in self.config.get_all_regions()
        }

        # Note: VADER has built-in keyword weighting, so these are now optional
        # Keeping them for backward compatibility and extra signal
        self.bullish_keywords = {
            "rally",
            "surge",
            "climb",
            "gain",
            "rise",
            "soar",
            "jump",
            "beat",
            "strong",
            "growth",
            "profit",
            "optimistic",
            "bullish",
            "upgrade",
            "buyback",
            "dividend",
            "expansion",
            "acquisition",
            "recovery",
        }

        self.bearish_keywords = {
            "fall",
            "drop",
            "decline",
            "loss",
            "plunge",
            "sink",
            "miss",
            "weak",
            "recession",
            "inflation",
            "bearish",
            "downgrade",
            "layoff",
            "bankruptcy",
            "debt",
            "risk",
            "concern",
            "warning",
            "slowdown",
            "crash",
            "crisis",
        }

        # Ticker pattern
        self.ticker_pattern = re.compile(r"\$([A-Z]{1,5})\b")

    async def fetch_regional_news(self, max_hours_old: int = 24, max_per_feed: int = 50) -> Dict:
        """
        Fetch news from all regional sources

        Returns comprehensive analysis by region and sector
        """
        loop = asyncio.get_event_loop()

        # Fetch all regions in parallel
        tasks = []
        for region in self.config.get_all_regions():
            task = loop.run_in_executor(
                None, self._fetch_region_news, region, max_hours_old, max_per_feed
            )
            tasks.append((region, task))

        results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

        # Combine results
        all_articles = []
        regional_stats = {}

        for (region, _), result in zip(tasks, results):
            if isinstance(result, BaseException):
                logger.error(f"Failed to fetch {region.value} news: {result}")
                regional_stats[region.value] = {"status": "error", "error": str(result)}
            elif isinstance(result, dict) and result:
                all_articles.extend(result["articles"])
                regional_stats[region.value] = {
                    "status": "success",
                    "article_count": str(len(result["articles"])),
                }

        # Classify and aggregate
        classified = self._classify_articles(all_articles)
        aggregated = self._aggregate_sentiments(classified)

        # Feed to matrix
        matrix_points_added = 0
        if self.sentiment_matrix and self.auto_feed_matrix:
            matrix_points_added = self._feed_to_matrix(classified)

        return {
            "status": "success",
            "timestamp": datetime.now().isoformat(),
            "total_articles": len(all_articles),
            "matrix_points_added": matrix_points_added,
            "regional_stats": regional_stats,
            "regional_breakdown": aggregated["by_region"],
            "sectoral_breakdown": aggregated["by_sector"],
            "overall_sentiment": aggregated["overall"],
        }

    async def fetch_all_feeds(self, max_hours_old: int = 24, max_per_feed: int = 50) -> Dict:
        """
        Fetch news from all regional RSS feeds.

        Backward-compatible method that returns data in format expected by
        Phase1Intelligence.gather_market_news().

        Args:
            max_hours_old: Maximum age of articles to fetch
            max_per_feed: Maximum articles per feed

        Returns:
            Dict with 'articles' key containing list of articles
        """
        loop = asyncio.get_event_loop()

        # Fetch all regions in parallel
        tasks = []
        for region in self.config.get_all_regions():
            task = loop.run_in_executor(
                None, self._fetch_region_news, region, max_hours_old, max_per_feed
            )
            tasks.append((region, task))

        results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

        # Collect all articles
        all_articles = []
        regional_stats = {}

        for (region, _), result in zip(tasks, results):
            if isinstance(result, BaseException):
                logger.warning(f"Failed to fetch {region.value} feeds: {result}")
                regional_stats[region.value] = {"status": "error", "count": 0}
            elif isinstance(result, dict) and result.get("articles"):
                all_articles.extend(result["articles"])
                regional_stats[region.value] = {
                    "status": "success",
                    "count": len(result["articles"]),
                }
            else:
                regional_stats[region.value] = {"status": "empty", "count": 0}

        # Calculate overall sentiment from articles
        overall_sentiment = 0.0
        if all_articles:
            sentiments = [a.get("combined_sentiment", 0) for a in all_articles]
            overall_sentiment = sum(sentiments) / len(sentiments)

        return {
            "status": "success",
            "articles": all_articles,
            "total_articles": len(all_articles),
            "timestamp": datetime.now().isoformat(),
            "overall_sentiment": round(overall_sentiment, 3),
            "regional_stats": regional_stats,
        }

    def _fetch_region_news(self, region: Region, max_hours_old: int, max_per_feed: int) -> Dict:
        """Fetch news for a specific region (synchronous)"""

        feeds = self.feeds_by_region.get(region, [])

        if not feeds:
            return {"region": region.value, "articles": []}

        all_articles = []

        for feed_url in feeds:
            try:
                feed = feedparser.parse(feed_url)

                if not feed.entries:
                    continue

                for entry in feed.entries[:max_per_feed]:
                    # Parse publish time
                    pub_date = self._parse_date(entry)
                    if not pub_date:
                        continue

                    hours_old = (datetime.now() - pub_date).total_seconds() / 3600

                    # Skip if too old
                    if hours_old > max_hours_old:
                        continue

                    # Get content
                    title = entry.get("title", "")
                    summary = entry.get("summary", entry.get("description", ""))

                    # Clean HTML tags
                    summary = re.sub(r"<[^>]+>", "", summary)

                    # Combined text
                    text = title + " " + summary

                    # Calculate sentiment
                    sentiment = self._analyze_sentiment(title, summary)

                    # Extract tickers
                    tickers = self._extract_tickers(text)

                    # Keyword-based sentiment boost
                    keyword_sentiment = self._keyword_sentiment(text)

                    # Recency weight
                    recency_weight = self._recency_weight(hours_old)

                    all_articles.append(
                        {
                            "source_region": region,
                            "title": title,
                            "summary": summary[:500],
                            "text": text,
                            "url": entry.get("link", ""),
                            "published": pub_date.isoformat(),
                            "hours_old": round(hours_old, 1),
                            "recency_weight": recency_weight,
                            "sentiment_polarity": sentiment["polarity"],
                            "keyword_sentiment": keyword_sentiment,
                            "combined_sentiment": (sentiment["polarity"] + keyword_sentiment) / 2,
                            "tickers": tickers,
                        }
                    )

            except Exception as e:
                logger.warning(f"Failed to fetch feed {feed_url}: {e}")
                continue

        return {"region": region.value, "articles": all_articles}

    def _parse_date(self, entry) -> Optional[datetime]:
        """Parse publish date from feed entry"""
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            from time import mktime

            return datetime.fromtimestamp(mktime(entry.published_parsed))
        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
            from time import mktime

            return datetime.fromtimestamp(mktime(entry.updated_parsed))
        else:
            return None

    def _analyze_sentiment(self, title: str, summary: str) -> Dict:
        """Analyze sentiment using VADER (v3.3.2)"""
        text = title + " " + summary

        # VADER returns multiple scores, we use compound (overall sentiment)
        scores = self.vader.polarity_scores(text)

        return {
            "polarity": round(scores["compound"], 3),  # -1 to +1
            "subjectivity": 0.0,  # VADER doesn't provide subjectivity, set to 0
        }

    def _keyword_sentiment(self, text: str) -> float:
        """Calculate sentiment based on keyword matching"""
        text_lower = text.lower()

        bullish_count = sum(1 for kw in self.bullish_keywords if kw in text_lower)
        bearish_count = sum(1 for kw in self.bearish_keywords if kw in text_lower)

        total_keywords = bullish_count + bearish_count
        if total_keywords == 0:
            return 0.0

        # Normalize to -1 to +1 range
        sentiment = (bullish_count - bearish_count) / total_keywords
        return round(sentiment, 3)

    def _extract_tickers(self, text: str) -> List[str]:
        """Extract ticker symbols from text"""
        matches = self.ticker_pattern.findall(text)

        # Filter common non-tickers
        common_words = {"A", "I", "CEO", "CFO", "US", "UK", "EU", "GDP", "CPI"}
        tickers = [t for t in matches if t not in common_words and len(t) <= 5]

        return list(set(tickers))

    def _recency_weight(self, hours_old: float) -> float:
        """Calculate recency weight (exponential decay)"""
        import math

        return round(math.exp(-hours_old / 24), 3)

    def _classify_articles(self, articles: List[Dict]) -> List[Dict]:
        """Classify articles by region and sector"""

        classified = []

        for article in articles:
            text = article["text"]

            # Region classification
            # First, use source region as hint
            article["source_region"]

            # But also analyze content for regional keywords
            # (e.g., WSJ might write about European markets)
            content_region = self.config.classify_region(text)

            # If content strongly suggests different region, use that
            # Otherwise, trust source region
            region = content_region

            # Sector classification
            tickers = article["tickers"]
            sectors = set()

            if tickers:
                for ticker in tickers:
                    sector = self.config.get_sector_for_ticker(ticker)
                    sectors.add(sector)
            else:
                sector = self.config.classify_sector(text)
                sectors.add(sector)

            # Create entry for each sector
            for sector in sectors:
                classified.append(
                    {
                        "region": region,
                        "sector": sector,
                        "sentiment": article["combined_sentiment"],
                        "title": article["title"],
                        "recency_weight": article["recency_weight"],
                        "timestamp": datetime.fromisoformat(article["published"]),
                    }
                )

        return classified

    def _aggregate_sentiments(self, classified: List[Dict]) -> Dict:
        """Aggregate sentiments by region and sector"""

        # By region
        by_region = {}
        for region in self.config.get_all_regions():
            region_articles = [c for c in classified if c["region"] == region]

            if region_articles:
                # Weight by recency
                total_weight = sum(a["recency_weight"] for a in region_articles)
                weighted_sentiment = (
                    sum(a["sentiment"] * a["recency_weight"] for a in region_articles)
                    / total_weight
                    if total_weight > 0
                    else 0
                )

                sentiment_score = (weighted_sentiment + 1) * 50

                by_region[region.value] = {
                    "sentiment_score": round(sentiment_score, 1),
                    "raw_sentiment": round(weighted_sentiment, 3),
                    "article_count": len(region_articles),
                    "trend": (
                        "bullish"
                        if sentiment_score > 60
                        else "bearish" if sentiment_score < 40 else "neutral"
                    ),
                }

        # By sector
        by_sector = {}
        for sector in self.config.get_all_sectors():
            sector_articles = [c for c in classified if c["sector"] == sector]

            if sector_articles:
                # Weight by recency
                total_weight = sum(a["recency_weight"] for a in sector_articles)
                weighted_sentiment = (
                    sum(a["sentiment"] * a["recency_weight"] for a in sector_articles)
                    / total_weight
                    if total_weight > 0
                    else 0
                )

                sentiment_score = (weighted_sentiment + 1) * 50

                by_sector[sector.value] = {
                    "sentiment_score": round(sentiment_score, 1),
                    "raw_sentiment": round(weighted_sentiment, 3),
                    "article_count": len(sector_articles),
                    "trend": (
                        "bullish"
                        if sentiment_score > 60
                        else "bearish" if sentiment_score < 40 else "neutral"
                    ),
                }

        # Overall
        if classified:
            total_weight = sum(c["recency_weight"] for c in classified)
            overall_sentiment = (
                sum(c["sentiment"] * c["recency_weight"] for c in classified) / total_weight
                if total_weight > 0
                else 0
            )

            overall_score = (overall_sentiment + 1) * 50

            overall = {
                "sentiment_score": round(overall_score, 1),
                "raw_sentiment": round(overall_sentiment, 3),
                "article_count": len(classified),
                "trend": (
                    "bullish"
                    if overall_score > 60
                    else "bearish" if overall_score < 40 else "neutral"
                ),
            }
        else:
            overall = {"sentiment_score": 50, "raw_sentiment": 0.0, "trend": "neutral"}

        return {"by_region": by_region, "by_sector": by_sector, "overall": overall}

    def _feed_to_matrix(self, classified: List[Dict]) -> int:
        """Feed classified sentiments to SentimentMatrix"""
        if not self.sentiment_matrix:
            return 0

        count = 0
        for c in classified:
            try:
                self.sentiment_matrix.add_sentiment(
                    region=c["region"],
                    sector=c["sector"],
                    sentiment=c["sentiment"],
                    source="rss",
                    timestamp=c["timestamp"],
                    weight=c["recency_weight"],
                )
                count += 1
            except Exception as e:
                logger.warning(f"Failed to add sentiment to matrix: {e}")
                continue

        return count

    async def fetch_news_by_region(self, region: Region, max_hours_old: int = 24) -> Dict:
        """Fetch news for a specific region"""

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self._fetch_region_news, region, max_hours_old, 50
        )

        if result and result["articles"]:
            classified = self._classify_articles(result["articles"])

            # Feed to matrix
            matrix_points_added = 0
            if self.sentiment_matrix and self.auto_feed_matrix:
                matrix_points_added = self._feed_to_matrix(classified)

            # Aggregate by sector
            by_sector = {}
            for sector in self.config.get_all_sectors():
                sector_articles = [c for c in classified if c["sector"] == sector]

                if sector_articles:
                    avg_sentiment = sum(c["sentiment"] for c in sector_articles) / len(
                        sector_articles
                    )
                    sentiment_score = (avg_sentiment + 1) * 50

                    by_sector[sector.value] = {
                        "sentiment_score": round(sentiment_score, 1),
                        "raw_sentiment": round(avg_sentiment, 3),
                        "article_count": len(sector_articles),
                    }

            return {
                "status": "success",
                "region": region.value,
                "article_count": len(result["articles"]),
                "matrix_points_added": matrix_points_added,
                "sectoral_breakdown": by_sector,
            }

        return {"status": "no_data", "region": region.value}
