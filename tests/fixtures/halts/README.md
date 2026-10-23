# Halt feed fixtures

`nasdaq_trader_halts.xml`: `https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts`, read 2026-10-23
from a cloud session (RSS 2.0 with a byte-order mark and the `ndaq:` fields). Four of the eighteen
items kept: two Nasdaq halts of 22 October (one with milliseconds in its time), an NYSE American
(`AMEX`) halt and Sinovac's halt of 2019 (a time without milliseconds); `numItems` edited to match.
No item showed a resumption that day; the resumption case is written inline in the tests.
