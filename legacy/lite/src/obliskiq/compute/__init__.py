"""The computation layer: the grid, the engines and the pure numerics.

Deliberately re-exports nothing. ``obliskiq-compute`` runs with
``PrivateNetwork=true`` and ``tests/test_compute_isolation.py`` refuses an
import graph that so much as NAMES an HTTP client, so this package's door stays
shut: a convenience re-export here would pull a sibling's imports in behind it,
which is how ``httpx`` reached the graph through ``util/__init__`` before
PEP 562 lazy loading closed it. Import the module you want.

SPDX-License-Identifier: AGPL-3.0-or-later
"""
