"""Curated news feeds: central banks, regulators and the issuers' filings feeds.

Reads RSS 2.0, RSS 1.0 (RDF, as the Bank of Canada and the RBA publish it) and Atom (EDGAR's
company feeds). Each item is stored once, under its id: the SHA-256 of its guid, else its link,
else its title and date, so an item two feeds carry is one row. The item's XML as received is
kept for the EventExtractor, with VADER's fallback tone of its title and summary.

A feed is the publisher's text: a body larger than ``max_body_bytes``, or one that declares a
DTD or an entity, is refused unparsed, and nothing in a feed is followed or executed. A feed
served by SEC carries ``SEC_USER_AGENT``; without it the feed is not asked.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from ...config.schema import ObliskConfig
from ...config.schema_sources import FeedConfig
from ...domain.sources import NewsItem
from ...errors import UpstreamError
from ...store import sources as source_repo
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy, get_with_retry
from ...util.timeutil import parse_instant
from ..market.plumbing import Collection, Pacer, PermanentCollectionError, failure_text
from .tone import FallbackTone

STEP = "rss"
HTTP_OK = 200
#: Declarations that let a document expand or fetch entities.
_UNSAFE = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")
_RDF_ABOUT = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"
#: The elements that hold one item, and the roots a feed has.
ITEM_TAGS = frozenset({"item", "entry"})
FEED_ROOTS = frozenset({"rss", "RDF", "feed"})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _child_text(element: ET.Element, *names: str) -> str | None:
    """The first non-blank text among the children named, in the order named."""
    for name in names:
        for child in _children(element, name):
            text = (child.text or "").strip()
            if text:
                return text
    return None


def plain(text: str | None, limit: int) -> str | None:
    """Text without markup or entities, its whitespace collapsed, cut to ``limit`` characters."""
    if text is None:
        return None
    cleaned = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()
    return cleaned[:limit] or None


def _published(element: ET.Element) -> datetime | None:
    stamp = _child_text(element, "pubDate")
    if stamp is not None:
        try:
            parsed = parsedate_to_datetime(stamp)
        except TypeError, ValueError:
            return None
        return None if parsed.tzinfo is None else parsed.astimezone(UTC)
    return parse_instant(_child_text(element, "date", "published", "updated"))


def _link(element: ET.Element) -> str | None:
    for child in _children(element, "link"):
        href = child.get("href")
        if href and child.get("rel", "alternate") == "alternate":
            return href.strip()
        text = (child.text or "").strip()
        if text:
            return text
    return None


def item_id(guid: str | None, link: str | None, title: str, published: datetime | None) -> str:
    key = guid or link or f"{title}|{published.isoformat() if published else ''}"
    return hashlib.sha256(key.encode()).hexdigest()


def parse_feed(
    content: bytes,
    *,
    feed_id: str,
    feed: FeedConfig,
    collected_at: datetime,
    max_summary_chars: int,
    tone: FallbackTone,
) -> list[NewsItem]:
    """The items of one feed body; an item without a title is left out."""
    if _UNSAFE.search(content):
        raise UpstreamError(f"{feed_id} declares a DTD or an entity; not parsed")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise UpstreamError(f"{feed_id} is not well-formed XML: {exc}") from exc
    if _local(root.tag) not in FEED_ROOTS:
        raise UpstreamError(f"{feed_id} is a {_local(root.tag)}, not an RSS or Atom feed")
    items: list[NewsItem] = []
    for element in root.iter():
        if _local(element.tag) not in ITEM_TAGS:
            continue
        title = plain(_child_text(element, "title"), max_summary_chars)
        if title is None:
            continue
        summary = plain(
            _child_text(element, "description", "summary", "content"), max_summary_chars
        )
        guid = _child_text(element, "guid", "id") or element.get(_RDF_ABOUT)
        link = _link(element)
        published = _published(element)
        items.append(
            NewsItem(
                item_id=item_id(guid, link, title, published),
                feed=feed_id,
                kind=feed.kind,
                region=feed.region,
                symbol=feed.symbol,
                guid=guid,
                link=link,
                title=title,
                summary=summary,
                published_at=published,
                collected_at=collected_at,
                raw=ET.tostring(element, encoding="unicode"),
                fallback_tone=tone.score(f"{title}. {summary or ''}"),
            )
        )
    return items


class NewsCollector:
    """Reads every configured feed and stores the items not held before."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        worker: StoreWorker,
        pacer: Pacer,
        agents: Mapping[str, str | None],
        now: Callable[[], datetime],
    ) -> None:
        self._config = settings.sources.news
        self._client = client
        self._policy = policy
        self._worker = worker
        self._pacer = pacer
        self._agents = agents
        self._now = now

    async def run(self, *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        tone = FallbackTone(self._config.tone_lexicon)
        for feed_id, feed in self._config.feeds.items():
            try:
                items = await self._pacer.retry(
                    feed_id, functools.partial(self._feed, feed_id, feed, tone)
                )
            except Exception as exc:  # one feed never stops the others
                collection.failed(feed_id, failure_text(exc))
                continue
            new = 0 if dry_run else await self._worker.run(source_repo.save_news_items, items)
            collection.succeeded(
                feed_id,
                new,
                degraded=not items,
                reason="" if items else "the feed listed no item",
                listed=len(items),
            )
            await self._pacer.pace()
        return collection

    async def _feed(self, feed_id: str, feed: FeedConfig, tone: FallbackTone) -> list[NewsItem]:
        headers: dict[str, str] | None = None
        if feed.user_agent_env is not None:
            agent = self._agents.get(feed.user_agent_env)
            if not agent:
                raise PermanentCollectionError(
                    f"{feed.user_agent_env} is unset; {feed_id} is not asked without it"
                )
            headers = {"User-Agent": agent}
        response = await get_with_retry(
            self._client, feed.url, policy=self._policy, headers=headers
        )
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"{feed_id} answered HTTP {response.status_code}")
        if len(response.content) > self._config.max_body_bytes:
            raise PermanentCollectionError(
                f"{feed_id} sent {len(response.content)} bytes, over the "
                f"{self._config.max_body_bytes} allowed; not parsed"
            )
        return parse_feed(
            response.content,
            feed_id=feed_id,
            feed=feed,
            collected_at=self._now(),
            max_summary_chars=self._config.max_summary_chars,
            tone=tone,
        )
