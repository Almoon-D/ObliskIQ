"""The offline fallback tone of a news item: VADER's compound score with config's financial terms.

Used only where no extraction exists (DECISIONS D70): it needs no network and no key, and it
reads words, not events. VADER's lexicon ships with the package and is read once per run.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import warnings
from collections.abc import Mapping

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

#: vaderSentiment 3.3.2 reads its lexicons with ``codecs.open``, deprecated in Python 3.14.
_VADER_DEPRECATION = r"codecs\.open\(\) is deprecated"


class FallbackTone:
    """Scores text in [-1, 1]; ``None`` for text with nothing to read."""

    def __init__(self, lexicon: Mapping[str, float]) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message=_VADER_DEPRECATION, category=DeprecationWarning
            )
            self._analyzer = SentimentIntensityAnalyzer()
        self._analyzer.lexicon.update(lexicon)

    def score(self, text: str) -> float | None:
        if not text.strip():
            return None
        return float(self._analyzer.polarity_scores(text)["compound"])
