# Session 4e data checks (Step 1)

The script behind Session 4e's data checks (`docs/knowledge/research-sources.md`, DECISIONS D93).
It is exploration (DECISIONS D64): it reads each series' metadata, span, duplicates, impossible
values, continuity on the weekday calendar and alignment with MSCI's calculation days, and lists
one-day level changes beyond a splice threshold by date. It computes no return, volatility,
correlation or drawdown, so the sources and span chosen rest on these checks alone.

```
export OBLISK_DATA_DIR=<a fresh directory>
obliskiq research data load ecb_fx ff3_market kf_developed_daily aqr_century_market \
  msci_world_eur msci_em_eur ecb_eonia ecb_estr bbk_bund_04y bbk_bund_05y bbk_bund_16y \
  bbk_bund_17y euro_cash bund_zero_05y bund_zero_17y tip ief gld crsox
uv run python research/explore/risk_scaling_data/checks.py > results/checks.json
```

`results/checks.json` is the output of 2026-10-18. Vendors revise and extend their files, so a
later run may differ in its last dates and counts.
