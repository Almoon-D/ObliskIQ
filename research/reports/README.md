# Reports

`obliskiq research run` writes one report per run here: `<id>.md` for a pre-registration's
confirmation run (there is one), `<id>-exploration-<UTC stamp>.md` for an exploratory run. Each
holds the inputs' hashes, the seeds, coverage, bias notes, each outcome and its labels, power,
the robustness checks, the tax rules applied and what the test cannot conclude. A report of an
exploratory run says so at the top and is never evidence (DECISIONS D64).

| Report | Session | Outcome |
|---|---|---|
| `core-allocation.md` | 4c, the core verdict | every candidate inconclusive (sign uncertain, low power); the frontier for the owner's choice |
| `selection-data-audit.md` | 4d, the selection data audit (not a run of the harness) | H2 and H4 not evaluable with free data; H1 chosen |
| `selection-country-momentum.md` | 4d, the first selection hypothesis | H1 (country momentum) insufficient; H5 (a volatility-scaled stop) insufficient, negative |
| `selection-country-momentum-exploration-20261017T174052Z.md` | 4d, exploration on the development sample (never evidence) | H1 inconclusive; H5 insufficient |
| `core-risk-scaling.md` | 4e, total-risk scaling against static A | every candidate inconclusive (sign uncertain, low power) and outside the owner's point; B-fast insufficient against its twin; the owner's point still resolves to A |
| `core-risk-scaling-exploration-20261018T141123Z.md` | 4e, exploration on the development window (never evidence) | each comparison inconclusive |
| `global-data-audit.md` | owner request, a desk audit (not a run of the harness) | the US leans in 4c (IL, the structure check) and in H6; the blocks and variables a global adaptive core needs, their sources, spans and gaps |
| `drawdown-tolerance-implications.md` | owner request (DECISIONS D88), not a run of the harness | what the open probability and horizon of the 30 % tolerance imply: Session 4c's frontier at 30 % and an illustration from assumptions only; sets no value |
