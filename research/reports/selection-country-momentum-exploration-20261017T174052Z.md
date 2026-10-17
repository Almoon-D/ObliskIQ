# Country momentum through UCITS, with and without a volatility-scaled stop, after Spanish tax

**EXPLORATORY — never evidence (DECISIONS D64).** Study `selection-country-momentum` (cross_section).

## Inputs

- Pre-registration: `selection-country-momentum.md`, SHA-256 `1de1557aab9a2eacafc780a616684a4a47485b45ee10ce17aa80d46d49266967`
- Lock: none (exploratory)
- Run at: 2026-10-17T17:40:52Z
- Inputs SHA-256: `855362752ecd4ffb2e86cee3ba6889c0193d24be203add3a0e6a9271d5946f06`
- Bootstrap: 10000 resamples of 12-month blocks of each monthly series, seed 20261017; Newey-West with 12 lags beside it
- alpha 0.05 (config research.decision), 2 test(s) in the confirmation run (Bonferroni), each interval at level 0.9500; target power 0.8 (config research.decision)
- Sample (development): 2001-12-01 to 2007-12-31, 73 rebalance dates (2001-12-31 to 2007-12-31); initial capital 100,000.00
- Members: 53 named, 51 with a series; vehicle etf on XETR in EUR, yearly cost 0.50%

## Outcomes

Each test's primary effect is top_group_excess: the mean of the candidate's monthly log return minus the other's, both accounts after costs and tax (the last month to the liquidation value), times 12. The interval is the block bootstrap's at the confirmation level; the Newey-West estimate and interval are beside it.

| Test | Comparison | Outcome | Estimate / yr | Interval | Newey-West | Theta | Power to detect theta | Detectable at target power | Before data: power / detectable | Estimate before tax |
|---|---|---|---|---|---|---|---|---|---|---|
| H1 country momentum | momentum minus universe | **inconclusive** (sign uncertain, low power) | 4.19% | [-3.36%, 8.01%] | 4.19% [-1.76%, 10.15%] | 2.00% | 0.101 | 8.18% | 0.383 / 3.37% | 4.91% |
| H5 volatility-scaled stop | momentum_stop minus momentum | **insufficient** (negative, low power) | -4.15% | [-6.98%, -1.77%] | -4.15% [-8.95%, 0.65%] | 1.00% | 0.111 | 3.78% | 0.344 / 1.80% | -4.63% |

- H1 country momentum: theta — two points a year of after-tax log growth over holding every member equally: the top group adds tracking error of several points a year, a turnover of several times a year and model risk, and below two points the excess is inside the uncertainty of the cost model itself (its two friction tiers differ by 0.8 % of value a trade, 1.6 % a round trip)
- H5 volatility-scaled stop: theta — one point a year of after-tax log growth over the same top group without the stop: a stop adds resting orders, trades and tax realised early, and below one point it does not pay for them

## What this test cannot conclude

- Whether the effect reaches theta: the interval holds values on both sides of it.
- Anything against the effect: the test had low power to detect theta, so an inconclusive result is not evidence that the effect is absent.
- That the effect is zero: only that an effect of theta or more is ruled out at this level.
- That the signal keeps its effect after the sample, or in a period, market or cost regime outside it.
- Anything about members the vendor does not serve beyond their measured share.
- That a portfolio of these index proxies is what the vehicles held would have earned: fund costs are a declared rate and tracking difference is not modelled.

## Adequacy checks

| Check | Passed | Detail |
|---|---|---|
| data loaded | yes | every series is in the research store |
| point in time | yes | every series is as_observed |
| survivorship | yes | at most 4.1% of the members on a rebalance date are not served (pre-registered maximum 0.05) |
| coverage | yes | 100.0% of served member-dates priced (pre-registered minimum 0.99) |
| effective observations | yes | 72 non-overlapping months (pre-registered minimum 60) |

Members lost to survivorship (named in the pre-registration, not served by the vendor), by year:

| Year | Members (most on a date) | Not served | Largest share on a date |
|---|---|---|---|
| 2001 | 49 | RUSSIA, VENEZUELA | 4.1% |
| 2002 | 49 | RUSSIA, VENEZUELA | 4.1% |
| 2003 | 49 | RUSSIA, VENEZUELA | 4.1% |
| 2004 | 49 | RUSSIA, VENEZUELA | 4.1% |
| 2005 | 49 | RUSSIA, VENEZUELA | 4.1% |
| 2006 | 49 | RUSSIA, VENEZUELA | 4.1% |
| 2007 | 48 | RUSSIA | 2.1% |

## Portfolios after tax

| Portfolio | Liquidation value | Log growth / yr | Max drawdown | Costs | Tax paid | Trades | Stops | Sales not made | Turnover / yr |
|---|---|---|---|---|---|---|---|---|---|
| momentum | 275,935.05 | 16.92% | -20.75% | 23,296.10 | 47,738.92 | 260 | 0 | 0 | 197.98% |
| universe | 214,574.91 | 12.73% | -29.18% | 5,683.14 | 31,593.60 | 307 | 0 | 0 | 50.36% |
| momentum_stop | 215,139.35 | 12.77% | -20.78% | 51,615.95 | 30,230.97 | 583 | 166 | 0 | 500.46% |
| placebo | 237,079.49 | 14.39% | -28.20% | 22,500.62 | 36,799.32 | 331 | 0 | 0 | 215.81% |

## Portfolios before tax

The same portfolios, trading the same way, with no tax withheld, settled or paid.

| Portfolio | Liquidation value | Log growth / yr | Max drawdown | Costs | Tax paid | Trades | Stops | Sales not made |
|---|---|---|---|---|---|---|---|---|
| momentum | 331,371.75 | 19.97% | -20.75% | 24,533.03 | 0.00 | 259 | 0 | 0 |
| universe | 246,827.18 | 15.06% | -29.18% | 5,765.93 | 0.00 | 309 | 0 | 0 |
| momentum_stop | 250,951.66 | 15.34% | -20.78% | 54,070.87 | 0.00 | 566 | 166 | 0 |
| placebo | 279,725.01 | 17.15% | -28.20% | 23,277.36 | 0.00 | 301 | 0 | 0 |

## Excess year by year

Each test's monthly excess log return after tax, summed over the months that end in the year:

| Year | H1 country momentum | H5 volatility-scaled stop |
|---|---|---|
| 2002 | 13.72% | 1.33% |
| 2003 | 1.85% | -3.88% |
| 2004 | 5.07% | -7.58% |
| 2005 | 9.20% | -4.05% |
| 2006 | -9.85% | -3.62% |
| 2007 | 5.16% | -7.09% |

## Robustness

- H1 country momentum: subperiods 2008-01-01 to 2016-12-31 n/a, 2017-01-01 to 2026-09-30 n/a; break-even cost multiplier 2.544 (declared cost uncertainty 2: exceeded); placebo: see the placebo below.
- H5 volatility-scaled stop: subperiods 2008-01-01 to 2016-12-31 n/a, 2017-01-01 to 2026-09-30 n/a; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 2: **not exceeded**); placebo: not registered for this test.

Placebo:

- placebo minus universe, against H1 country momentum's theta 2.00%: estimate 1.66%, interval [-3.88%, 4.80%]; fails, as it should: its interval's lower end is below the test's theta.

## Selection across trials

- Trials of this study, this run's tests included: 2
- H1 country momentum: monthly Sharpe 0.1876 against 0.1418 expected from the best of 2 trial(s); probability 0.642
- H5 volatility-scaled stop: monthly Sharpe -0.1982 against 0.1418 expected from the best of 2 trial(s); probability 0.001

## Data

| Series | Vendor | Point in time | Downloaded | SHA-256 | Source |
|---|---|---|---|---|---|
| msci_australia_eur.level | msci | as_observed | 2026-10-17T16:52:50Z | `7a013b47d9e89cbbd5ecdd18598c1fc92ea639fb367e09f9dd03d178164e1296` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=903600&index_variant=NETR&currency_symbol=EUR |
| msci_austria_eur.level | msci | as_observed | 2026-10-17T16:52:53Z | `e732eb75550aa618a7342aa76cd9b92f18397fb6982899291ff4e571cdf3f213` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=904000&index_variant=NETR&currency_symbol=EUR |
| msci_belgium_eur.level | msci | as_observed | 2026-10-17T16:52:56Z | `f981cbecc67549e7176977fd3377ff3846e2661b8d3cae27f3e73e9af0082d85` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=905600&index_variant=NETR&currency_symbol=EUR |
| msci_canada_eur.level | msci | as_observed | 2026-10-17T16:53:01Z | `47cf31df3efb0895ff10f5be3b147568084ea9f55e55e3be71988c3c14cc3501` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=912400&index_variant=NETR&currency_symbol=EUR |
| msci_denmark_eur.level | msci | as_observed | 2026-10-17T16:53:02Z | `5ba3df103ef80d384d24277d61cac01341452e76b3d47c40828a42c4862fc790` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=920800&index_variant=NETR&currency_symbol=EUR |
| msci_finland_eur.level | msci | as_observed | 2026-10-17T16:53:07Z | `5a270e5ae85e0ab85031b72b9e0f1629753fbddcf49e02fa9112972aa0c1e659` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=924600&index_variant=NETR&currency_symbol=EUR |
| msci_france_eur.level | msci | as_observed | 2026-10-17T16:53:10Z | `dae449238b5341ae73629fabd14785268049c0f355707dc2e56efeab67a6de5e` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=925000&index_variant=NETR&currency_symbol=EUR |
| msci_germany_eur.level | msci | as_observed | 2026-10-17T16:53:12Z | `03e8a26666177e6e930bab0d33a8eb5d40b6f2102943822a5b14596b11c0941b` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=928000&index_variant=NETR&currency_symbol=EUR |
| msci_hong_kong_eur.level | msci | as_observed | 2026-10-17T16:53:14Z | `b52076c0933c784ebe4a7545bef2f1842e8e892704d488c180a0cf8644b9d100` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=934400&index_variant=NETR&currency_symbol=EUR |
| msci_ireland_eur.level | msci | as_observed | 2026-10-17T16:53:16Z | `e766716e55829f5cd7c107def534860a630f109b04f92a692f5277ada8e3e203` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=937200&index_variant=NETR&currency_symbol=EUR |
| msci_israel_eur.level | msci | as_observed | 2026-10-17T16:53:22Z | `5554e669b21f4deb66c9e4e3760353785a998046f5c3a534d17e7a67163559ff` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=300400&index_variant=NETR&currency_symbol=EUR |
| msci_italy_eur.level | msci | as_observed | 2026-10-17T16:53:25Z | `417896b43e3f9c56865ebbe119f428e171168a2ac411bf2606a38a70a2019ceb` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=938000&index_variant=NETR&currency_symbol=EUR |
| msci_japan_eur.level | msci | as_observed | 2026-10-17T16:53:27Z | `8a01367dd0d1a57eb5a4f97f8e120c0b7fb9fd4fc9f26a7476f4cddc3b40896f` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=939200&index_variant=NETR&currency_symbol=EUR |
| msci_netherlands_eur.level | msci | as_observed | 2026-10-17T16:53:30Z | `e5e6b2a6d808f2951ab647ac4f7cd7075d39fd7aa587de835e765882e3cb4665` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=952800&index_variant=NETR&currency_symbol=EUR |
| msci_new_zealand_eur.level | msci | as_observed | 2026-10-17T16:53:32Z | `13cf466c384f99568bbfead0149e042c435043b2cddf9059bf637db881e684ab` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=955400&index_variant=NETR&currency_symbol=EUR |
| msci_norway_eur.level | msci | as_observed | 2026-10-17T16:53:34Z | `9663f441492a5688e000b455f2186721b520e15834edded6e72cdda869dd2da7` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=957800&index_variant=NETR&currency_symbol=EUR |
| msci_portugal_eur.level | msci | as_observed | 2026-10-17T16:53:37Z | `1e9d9bd5f77c90d30736bf5857b5e870025091bcfd4daf8f553ea5f6e578ea27` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=962000&index_variant=NETR&currency_symbol=EUR |
| msci_singapore_eur.level | msci | as_observed | 2026-10-17T16:53:39Z | `95cb0d3c3a248af758025ea5569856971ab3b7c034578c223daa4a430eb0aa84` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=998100&index_variant=NETR&currency_symbol=EUR |
| msci_spain_eur.level | msci | as_observed | 2026-10-17T16:53:40Z | `3c31acd96e88c75b2e13be371662321ae192b0abbd03cff545048bad902b698a` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=972400&index_variant=NETR&currency_symbol=EUR |
| msci_sweden_eur.level | msci | as_observed | 2026-10-17T16:53:42Z | `6ed8069e1ede1c4893a812aa8df72f32d56ed0065033d49fcd36bf49c823c9c5` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=975200&index_variant=NETR&currency_symbol=EUR |
| msci_switzerland_eur.level | msci | as_observed | 2026-10-17T16:53:44Z | `55fbf1177dbb4e1668e0b278c702e3017897489bf91a8c869384e9ad85b3b390` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=975600&index_variant=NETR&currency_symbol=EUR |
| msci_united_kingdom_eur.level | msci | as_observed | 2026-10-17T16:53:46Z | `77f8dd432a66d206ea391ab524fcee296f2b7cc3686091e86aee454a6e7b79a9` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=982600&index_variant=NETR&currency_symbol=EUR |
| msci_usa_eur.level | msci | as_observed | 2026-10-17T16:53:48Z | `791f0bcafe5b8674b77ca007f2763d71ee4237dabd74e5f4160c3f1c79e5530c` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=984000&index_variant=NETR&currency_symbol=EUR |
| msci_brazil_eur.level | msci | as_observed | 2026-10-17T16:53:50Z | `8e4a520fc80690098b17a781dfd673517dbeab33c3a8bd4d5032ba226c74c727` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=907600&index_variant=NETR&currency_symbol=EUR |
| msci_chile_eur.level | msci | as_observed | 2026-10-17T16:53:52Z | `95e98d1469627e9b9e103de03a87d8a7f4de8e82e08514f379a94ddade961e05` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=915200&index_variant=NETR&currency_symbol=EUR |
| msci_china_eur.level | msci | as_observed | 2026-10-17T16:53:54Z | `98ea1073ae3e3af172d02cafd13d53d4e822c81702ffd6232b947ec909c0bf6a` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=302400&index_variant=NETR&currency_symbol=EUR |
| msci_colombia_eur.level | msci | as_observed | 2026-10-17T16:53:56Z | `e3a239de86b9c3a385ff351e2f82a084ae7202d62471abdec7cd0c7823b13dd7` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=917000&index_variant=NETR&currency_symbol=EUR |
| msci_czech_republic_eur.level | msci | as_observed | 2026-10-17T16:53:58Z | `02e28361117568327e1430a166851f93761461ddd634b12ba8634a2acb916b34` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=920000&index_variant=NETR&currency_symbol=EUR |
| msci_egypt_eur.level | msci | as_observed | 2026-10-17T16:54:00Z | `73e4cd7aaee330dc9f0a8254a5166698a5589f1998d9c81871fbd87dd14188d2` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=105766&index_variant=NETR&currency_symbol=EUR |
| msci_greece_eur.level | msci | as_observed | 2026-10-17T16:54:05Z | `d6cb10932f09e4356d88fb6cecb5f78129efe52dd67978e9a32cdf5184a2bfc5` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=930000&index_variant=NETR&currency_symbol=EUR |
| msci_hungary_eur.level | msci | as_observed | 2026-10-17T16:54:06Z | `e94c4ec504013e769944d0f5e8f8ef544bf8e23eb52d0133ed24a4f5989bd690` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=934800&index_variant=NETR&currency_symbol=EUR |
| msci_india_eur.level | msci | as_observed | 2026-10-17T16:54:08Z | `9024f6490e31d7368451f66d885b0fae2a34d4d18631928e9a46ba2891860b78` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=935600&index_variant=NETR&currency_symbol=EUR |
| msci_indonesia_eur.level | msci | as_observed | 2026-10-17T16:54:10Z | `20bee2ce2ebb94a5bfb3029e5306b35f0dcf2f277736474805161b767f4d1e43` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=105767&index_variant=NETR&currency_symbol=EUR |
| msci_korea_eur.level | msci | as_observed | 2026-10-17T16:54:13Z | `5c78bc85daa4e9c5ef25c67d7c3888a089b5fe36f7b9f3a66329f648577ee0bb` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=941000&index_variant=NETR&currency_symbol=EUR |
| msci_malaysia_eur.level | msci | as_observed | 2026-10-17T16:54:15Z | `bacee5f45d1940334c6fa6931579a1e89dcfe376487ae9595c3eccd2bdccd542` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=105768&index_variant=NETR&currency_symbol=EUR |
| msci_mexico_eur.level | msci | as_observed | 2026-10-17T16:54:17Z | `9c2570de1b95df161422a5a3e7299f23a3624c45ad927f62d3b08d0246d7a6b1` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=848400&index_variant=NETR&currency_symbol=EUR |
| msci_peru_eur.level | msci | as_observed | 2026-10-17T16:54:19Z | `0cdd5f5fabb76a968000bf6ea8ed3cf3910a51ffc45d7255179e063e8143f7c4` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=960400&index_variant=NETR&currency_symbol=EUR |
| msci_philippines_eur.level | msci | as_observed | 2026-10-17T16:54:22Z | `def4338419cb0d4cefbfb3e84503bcc0bc4d79246c2e6943e4f88387bd8453fa` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=860800&index_variant=NETR&currency_symbol=EUR |
| msci_poland_eur.level | msci | as_observed | 2026-10-17T16:54:24Z | `c0d45d222e250823dad8d4d5df47060233915d21e421800fddb727e0b3c98fc9` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=961600&index_variant=NETR&currency_symbol=EUR |
| msci_south_africa_eur.level | msci | as_observed | 2026-10-17T16:54:26Z | `a1095dad3c35c2cdbe70b9411fa63639abb60d5f4422652338133530d009e651` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=971000&index_variant=NETR&currency_symbol=EUR |
| msci_taiwan_eur.level | msci | as_observed | 2026-10-17T16:54:28Z | `a44162f587cef88e9b1e942406c60e9dde063742b833eed342ba547218bee233` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=915800&index_variant=NETR&currency_symbol=EUR |
| msci_thailand_eur.level | msci | as_observed | 2026-10-17T16:54:30Z | `fb44e5e31d1a5348f989f4d61640485beacac917acd777f34a3fc76a2ee917d9` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=105769&index_variant=NETR&currency_symbol=EUR |
| msci_turkey_eur.level | msci | as_observed | 2026-10-17T16:54:33Z | `e126b440a7ef328e779536f4f3e90ed7ab26f53d09e0a06288c7d627ed09eba0` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=979200&index_variant=NETR&currency_symbol=EUR |
| msci_qatar_eur.level | msci | as_observed | 2026-10-17T16:54:35Z | `d426c336e5da65b80e2eace021bf605ce0b9369d7c0c780cf957a8ab7466f665` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=133715&index_variant=NETR&currency_symbol=EUR |
| msci_united_arab_emirates_eur.level | msci | as_observed | 2026-10-17T16:54:37Z | `1be548e3528963d6e61718cdc5633687d6daacb7ea111e7e50b9b5cb082cf404` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=133717&index_variant=NETR&currency_symbol=EUR |
| msci_saudi_arabia_eur.level | msci | as_observed | 2026-10-17T16:54:41Z | `606f586443a84b2d3d8d1de22bf88f8e3702b0b9d919905be6dc8342ce87995c` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=705405&index_variant=NETR&currency_symbol=EUR |
| msci_kuwait_eur.level | msci | as_observed | 2026-10-17T16:54:43Z | `0387b754248f10b9667845833ba45887d2b164b793d557d560caecbea477dbd1` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=133713&index_variant=NETR&currency_symbol=EUR |
| msci_argentina_eur.level | msci | as_observed | 2026-10-17T16:54:44Z | `73b2bc1538da32c24f5179e3c71752711289a9096de98b7a9c3258602fcd71e7` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=903200&index_variant=NETR&currency_symbol=EUR |
| msci_pakistan_eur.level | msci | as_observed | 2026-10-17T16:54:47Z | `c0c7e0d041e78daceb162411fe40b084bf7fc2cc54fa3571e60347009673d9b5` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=958600&index_variant=NETR&currency_symbol=EUR |
| msci_jordan_eur.level | msci | as_observed | 2026-10-17T16:54:49Z | `751d400dc67066e2142bbb357c1d3c1d2b68c01dccccf7cbdb0f48843efdc564` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=940000&index_variant=NETR&currency_symbol=EUR |
| msci_morocco_eur.level | msci | as_observed | 2026-10-17T16:54:51Z | `637cc5a8dfcd4a5802048d8e7b62401f95e444b800b5ed236303101374ca6444` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=105765&index_variant=NETR&currency_symbol=EUR |

- Coverage: 100.00% of served member-dates priced
- Without data: msci_saudi_arabia_eur.level
- Entering after the start: msci_qatar_eur.level, msci_united_arab_emirates_eur.level, msci_kuwait_eur.level
- Ending before the horizon: none
- A universe listed today cannot show members that disappeared before it was listed; the report measures what the list can show.

## Bias notes

- An index, not a fund - no fund costs or tracking difference (a study deducts the vehicle's expense ratio).
- MSCI Argentina (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Australia (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Austria (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Belgium (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Brazil (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Canada (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Chile (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI China (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Colombia (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Czech Republic (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Denmark (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Egypt (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Finland (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI France (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Germany (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Greece (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Hong Kong (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Hungary (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI India (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Indonesia (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Ireland (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Israel (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Italy (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Japan (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Jordan (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Korea (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Kuwait (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Malaysia (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Mexico (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Morocco (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Netherlands (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI New Zealand (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Norway (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Pakistan (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Peru (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Philippines (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Poland (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Portugal (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Qatar (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Saudi Arabia (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Singapore (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI South Africa (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Spain (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Sweden (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Switzerland (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Taiwan (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Thailand (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI Turkey (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI USA (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI United Arab Emirates (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI United Kingdom (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI index levels are published at the end of each calculation day; MSCI may restate history without vintages.
- Every member is an index proxy priced as one kind of vehicle, its yearly cost deducted day by day; a member without such a vehicle at a date is held as if it had one.
- Trades fill at the rebalance date's close, moved by the cost model's spread and slippage.
- A protective stop is watched on daily closes and fills at the first close at or below it: a gap through the stop is borne in full, and a touch inside a session that recovers by the close is not seen. The cash from a stop waits for the next rebalance date.
- A year's tax is paid at the first rebalance date on or after its payment date; the profile's current rules and rate table are applied to every simulated year.
- Losses still carried forward at the horizon are worth nothing.
- The interval resamples the realised monthly series in blocks; the tax a resampled history would have paid is not re-simulated.

Unverified figures the costs rest on: `commission:europe_eur`, `friction_assumption`, `market_cap_unknown`, `tax_not_modelled`.

## Tax rules applied

- policy es_irpf; lots fifo; homogeneity same_isin_or_issuer_and_index
- negative balances carried forward 4 years, offsetting up to 0.25 of the other component (LIRPF art. 49 (BOE-A-2006-20764, consolidated text read 2026-10-16))
- savings rate table for tax years 2025 to 2026, verified 2026-10-16 (LIRPF arts. 66.1 and 76 as amended by Ley 7/2024 disp. final 7 (BOE-A-2024-26694); BOE consolidated text read 2026-10-16)
- tax paid on 06-30 of the year 1 after the tax year (Orden HAC/277/2026 art. 7.1 (BOE-A-2026-7041), filing and payment 8 April to 30 June 2026 for tax year 2025)
- foreign-tax credit at the average effective rate (LIRPF art. 80 (BOE-A-2006-20764, consolidated text read 2026-10-16))
- withholding at source US 0.15 (secondary: Spain–United States tax convention of 22 February 1990, art. 10.2.b, as amended by the 2013 protocol in force 27 November 2019; secondary sources read 2026-10-16)
- two-month rule: a loss is deferred by a homogeneous purchase within 2 months (listed) or 12 months (unlisted) of the sale (LIRPF art. 33.5.f and g)
- ETF switches are sales; fund switches are deferred only if the broker supports them (supported: False; LIRPF art. 94.1.a)
- swap partners differ in issuer: True, in tracked index: True

Not tax advice.
