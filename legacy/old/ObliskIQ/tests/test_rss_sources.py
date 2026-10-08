"""RSS source hygiene: dead endpoints are noise (a failed fetch per feed, every run)."""

import pytest

from modules.market.regional_sectoral_config import RegionalSectoralConfig

pytestmark = pytest.mark.unit


def test_no_decommissioned_reuters_feeds():
    """Reuters shut its public RSS (feeds.reuters.com) in 2020; the host does not resolve."""
    config = RegionalSectoralConfig()
    urls = [url for region in config.get_all_regions() for url in config.get_rss_sources(region)]

    assert urls  # the config is not empty
    assert not [u for u in urls if "feeds.reuters.com" in u]


def test_every_region_still_has_sources():
    config = RegionalSectoralConfig()
    assert all(config.get_rss_sources(region) for region in config.get_all_regions())
