# Provenance Purge Report — Stop Lying to the Gate

**Version:** 5.37.0
**Date:** 2026-06-10
**Author:** ObliskIQ Logic Council
**Related:** `scripts/generate_ic_provenance.py`, `config/pattern_provenance.json`,
`docs/archive/PATTERN_AUGMENTATION_METHODOLOGY.md`, `ObliskIQ/tests/test_provenance_no_fabrication.py`

> Jim Simons: *"If it's not statistically significant, it doesn't exist."*

---

## 1. What was wrong

`scripts/generate_ic_provenance.py` contained an `augment_sample_size()` method
that **fabricated** the sample size used for every statistical test:

```python
# OLD (deleted):
cross_market_multiplier = 10                       # ~10 "market variants"
base_augmented = max(original_size * 10, 400)      # ×10 and floor at 400
...
min_for_tight_ci = 400  # "Yields CI width ~0.18-0.20 for moderate ICs"
return max(min(augmented, max_realistic), min_for_tight_ci)
```

This was not a methodology — it was reverse-engineering the inputs to clear our
own trust gate. The trust gate in `ICProvenance.validate()` requires a 95%
confidence-interval width `≤ 0.20`, which (via the Fisher-z transform) needs
roughly **n ≥ 250** for a moderate IC. No pattern in the library has that many
real observations, so the generator simply **manufactured** them: academic
samples of 7–124 events became 400–5040 "observations", driving every
p-value to `0.0000` and every CI artificially tight.

The result: **30 of 31 patterns reported as TRUSTED** on fabricated inputs.

`docs/archive/PATTERN_AUGMENTATION_METHODOLOGY.md` already conceded that 28/31 patterns
needed OOS revalidation with "20–50% IC inflation expected." This purge stops
the system from quietly trusting them anyway.

---

## 2. What changed

1. **Deleted `augment_sample_size()` entirely.** `sample_size` now equals the
   pattern's real observation count, taken verbatim from `PatternLibrary`
   (the documented academic sample). No multipliers. No floors.
2. **Recomputed every statistic from the real `n`** — t-statistic, two-tailed
   p-value, and Fisher-z 95% CI.
3. **Applied a Benjamini-Hochberg multiple-testing correction** across the
   whole family of 31 pattern tests (see §4).
4. **Flagged every pattern that fails the gates** with
   `requires_oos_revalidation = true`, status UNTRUSTED, and recorded the real
   `actual_observations`. Confirmed `production_mode=True` excludes all of them
   (see §5).
5. **Added a CI guard** (`test_provenance_no_fabrication.py`) that fails the
   build if any provenance `sample_size` ever again exceeds the actual
   observation count.

Both canonical provenance files are regenerated and kept identical:
`config/pattern_provenance.json` and `ObliskIQ/config/pattern_provenance.json`.
A machine-readable observation-count artifact is written to
`reports/validation/provenance_observation_counts.json`.

---

## 3. Before / After

| Metric | Before (fabricated) | After (honest) |
|---|---|---|
| Patterns with provenance | 31 | 31 |
| **TRUSTED** | **30** | **0** |
| Require OOS revalidation | 1 | **31** |
| Sample sizes (`n`) | 400 – 5040 (inflated) | **7 – 200 (real)** |
| p-values | almost all `0.0000` | real, 0.002 – 0.155 |
| Multiple-testing correction | none | Benjamini-Hochberg (FDR) |

The trusted count dropped from **30 → 0**. This is the correct behaviour, not a
regression: it is what honesty costs when no pattern has enough real
observations to clear the precision gate.

### Why not "low single digits"?

There is **no honest path to a non-zero trusted count** with the current gate.
The only patterns with `n ≥ 30` (e.g. `alt_003` at n=200, `sent_010` at n=124)
still fail the `CI width ≤ 0.20` requirement — the narrowest interval in the
entire library is `alt_003` at **0.249**. Getting any pattern to "trusted" would
require either re-inflating `n` (the fabrication we just removed) or loosening
the gate (moving the goalposts). Both are forms of lying to the gate, so we did
neither. **The gate thresholds were left untouched.**

---

## 4. Multiple-testing methodology

We test ~31 patterns against the same `p < 0.05` threshold. With 31 independent
tests at α = 0.05 you expect ≈1.5 false positives by chance alone (Harvey, Liu &
Zhu 2016). We therefore apply the **Benjamini-Hochberg** step-up procedure
(Benjamini & Hochberg 1995) to control the false discovery rate across the
family:

- Rank the raw p-values `p_(1) ≤ … ≤ p_(m)` (m = 31).
- Adjusted q-value for rank `i`: `q_(i) = min over k≥i of ( p_(k) · m / k )`.
- A pattern survives multiple testing if `q < 0.05`.

A pattern is **TRUSTED** only if it clears *all* per-pattern gates
(`n ≥ 30`, `p < 0.05`, `|IC| ≥ 0.03`, `CI width ≤ 0.20`) **and** survives BH.
Each record stores `p_value_bh_adjusted` and `passes_bh_correction`. Even before
BH, the CI-width gate already eliminates every pattern, so BH does not change the
count here — but it is now part of the honest record and the trust decision.

Bonferroni (α/m = 0.0016) was considered but is overly conservative for a
discovery library; BH is the standard FDR control cited in the methodology doc.

---

## 5. Production exclusion (verified)

With the honest provenance loaded, trust is computed dynamically by
`ICProvenance.validate()`, so the purge propagates automatically:

```
PatternLibrary(production_mode=True).get_validated_patterns()  -> []   (0)
PatternLibrary(production_mode=True).get_production_patterns()  -> []   (0)
ProvenanceRegistry.generate_audit_report()  -> trusted_count=0, trust_ratio=0.0
orchestrator._check_pattern_provenance()     -> {trusted_count: 0, untrusted_count: 31}
```

- **Orchestrator pre-check** (`_check_pattern_provenance`) reads
  `library.get_validated_patterns()` → 0 trusted, and degrades gracefully
  (it flags, it does not crash the pipeline).
- **Phase 2 / Phase 4** are AI-driven and do not consume the pattern library's
  provenance gate directly; the binding gate is `get_validated_patterns()` /
  `get_production_patterns()`, both of which now return `[]` in production mode.

No untrusted pattern can fire a production signal.

---

## 6. Per-pattern verdicts (honest)

`n` = real observations. `raw p` = two-tailed t-test on real `n`.
`BH q` = Benjamini-Hochberg adjusted. `CI w` = 95% CI width. All 31 are UNTRUSTED.

| Pattern | IC | n (before) | n (real) | raw p | BH q | CI width | Fails on |
|---|---|---|---|---|---|---|---|
| `alt_001` | +0.38 | 500 | **120** | 0.0000 | 0.0002 | 0.308 | CI width > 0.20 |
| `alt_002` | +0.45 | 500 | **85** | 0.0000 | 0.0002 | 0.343 | CI width > 0.20 |
| `alt_003` | +0.32 | 600 | **200** | 0.0000 | 0.0001 | 0.249 | CI width > 0.20 |
| `alt_004` | +0.28 | 500 | **150** | 0.0005 | 0.0040 | 0.296 | CI width > 0.20 |
| `credit_001` | +0.55 | 400 | **19** | 0.0147 | 0.0551 | 0.676 | n < 30 |
| `cross_001` | -0.41 | 5040 | **15** | 0.1291 | 0.1368 | 0.892 | n < 30 |
| `macro_001` | +0.68 | 600 | **11** | 0.0213 | 0.0551 | 0.774 | n < 30 |
| `macro_002` | +0.55 | 600 | **9** | 0.1250 | 0.1368 | 1.069 | n < 30 |
| `macro_003` | +0.72 | 600 | **7** | 0.0681 | 0.0917 | 1.027 | n < 30 |
| `macro_004` | +0.62 | 600 | **16** | 0.0104 | 0.0461 | 0.674 | n < 30 |
| `macro_005` | +0.48 | 600 | **12** | 0.1143 | 0.1312 | 0.956 | n < 30 |
| `macro_006` | +0.42 | 600 | **22** | 0.0516 | 0.0762 | 0.717 | n < 30 |
| `macro_007` | +0.38 | 600 | **34** | 0.0266 | 0.0551 | 0.588 | CI width > 0.20 |
| `macro_008` | +0.56 | 600 | **11** | 0.0732 | 0.0945 | 0.928 | n < 30 |
| `macro_009` | +0.44 | 600 | **13** | 0.1324 | 0.1368 | 0.944 | n < 30 |
| `macro_010` | +0.51 | 600 | **17** | 0.0365 | 0.0628 | 0.757 | n < 30 |
| `sent_001` | -0.42 | 2520 | **23** | 0.0460 | 0.0751 | 0.700 | n < 30 |
| `sent_002` | +0.48 | 2520 | **14** | 0.0824 | 0.1021 | 0.873 | n < 30 |
| `sent_003` | +0.36 | 2520 | **42** | 0.0192 | 0.0551 | 0.536 | CI width > 0.20 |
| `sent_004` | +0.41 | 2520 | **28** | 0.0302 | 0.0551 | 0.636 | n < 30 |
| `sent_005` | +0.39 | 2520 | **19** | 0.0988 | 0.1178 | 0.795 | n < 30 |
| `sent_006` | +0.34 | 2520 | **31** | 0.0613 | 0.0864 | 0.636 | p ≥ 0.05 |
| `sent_007` | -0.38 | 2520 | **27** | 0.0506 | 0.0762 | 0.664 | n < 30 |
| `sent_008` | +0.29 | 2520 | **56** | 0.0302 | 0.0551 | 0.484 | CI width > 0.20 |
| `sent_009` | +0.32 | 2520 | **88** | 0.0024 | 0.0125 | 0.378 | CI width > 0.20 |
| `sent_010` | +0.27 | 2520 | **124** | 0.0024 | 0.0125 | 0.328 | CI width > 0.20 |
| `tech_001` | -0.52 | 1260 | **18** | 0.0270 | 0.0551 | 0.724 | n < 30 |
| `tech_002` | +0.46 | 1260 | **11** | 0.1546 | 0.1546 | 1.024 | n < 30 |
| `tech_003` | +0.34 | 1260 | **47** | 0.0194 | 0.0551 | 0.513 | CI width > 0.20 |
| `tech_004` | +0.38 | 1260 | **33** | 0.0292 | 0.0551 | 0.598 | CI width > 0.20 |
| `tech_005` | -0.43 | 1260 | **26** | 0.0283 | 0.0551 | 0.649 | n < 30 |

**Failure breakdown:** 19 patterns fail on `n < 30`; 11 fail on `CI width > 0.20`
(they have `n ≥ 30` but too few observations for the precision gate); 1 fails on
`p ≥ 0.05` (`sent_006`).

---

## 7. Important caveat: no fresh out-of-sample backtest was run here

The honest path is to derive `n` and IC from a **real walk-forward run on real
warehouse data** via the canonical backtester
(`modules/testing/walk_forward_backtester.py`, wired by
`ObliskIQ/scripts/generate_ic_provenance.py`). In this environment that run
**cannot be performed**: the DataWarehouse is unpopulated (no database file
exists) and the market-data library (`yfinance`) is not installed.

Consequently:

- `sample_size` reflects the **documented academic observation count** — the
  only real, non-fabricated count available — not a fresh OOS backtest.
- `ic_point_estimate` is the **documented academic IC**, not a re-derived OOS
  IC. (Re-derivation would very likely *lower* it 20–50% per the methodology
  doc.)
- **This is exactly why every pattern is flagged `requires_oos_revalidation`.**

No number here is inflated; several are simply "as good as we can honestly state
without market data," and the flags say so.

---

## 8. How a pattern earns trust back

1. Populate the DataWarehouse with real price/macro history.
2. Run `ObliskIQ/scripts/generate_ic_provenance.py --all` (canonical
   walk-forward backtester) to produce **real OOS** `n`, IC, and p-values.
3. A pattern becomes TRUSTED only if, on that real OOS data, it clears every
   gate (`n ≥ 30`, `p < 0.05`, `|IC| ≥ 0.03`, `CI width ≤ 0.20`) **and** survives
   the Benjamini-Hochberg correction.
4. The CI guard (`test_provenance_no_fabrication.py`) ensures `sample_size`
   never exceeds the real observation count recorded in the backtest artifact.

If the gates themselves are judged too strict for a discovery library (the
`CI width ≤ 0.20` requirement is the binding one), that is a separate, explicit
decision to be made in `ICProvenance.validate()` — in the open, not by
back-dooring the sample size.

---

## 9. Reproducing this report

```bash
# Regenerate honest provenance (no warehouse needed; uses documented real n)
python ObliskIQ/scripts/generate_ic_provenance.py --all

# Audit
python ObliskIQ/scripts/audit_ic_provenance.py

# Guard test
pytest ObliskIQ/tests/test_provenance_no_fabrication.py -m "unit or critical"
```

---

## References

- Benjamini, Y. & Hochberg, Y. (1995). *Controlling the False Discovery Rate.*
- Harvey, C., Liu, Y. & Zhu, H. (2016). *…and the Cross-Section of Expected Returns.*
- Bailey, D. et al. (2014). *Pseudo-Mathematics and Financial Charlatanism.*
- McLean, R. D. & Pontiff, J. (2016). *Does Academic Research Destroy Stock Return Predictability?*
