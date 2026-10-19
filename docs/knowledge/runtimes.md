# Runtimes

The runtimes and platforms ObliskIQ runs on, as their publishers state them: releases, support
windows and what each supports. Which version ObliskIQ runs, and why, is `docs/DECISIONS.md` D96;
the Client Portal Gateway's own Java facts are in `ibkr.md`.

Tag: `[DOC 2026-10-19]` = read on the publisher's page on that date. A support window stated as
"at least" is a floor, not an end date.

## Python

- 3.15.0 was released on 2026-10-09; its bugfix releases run to 3.15.12 (2028-10-03) and source
  security releases to about October 2031 (PEP 790). 3.14 is supported to 2030-10 (PEP 745); its
  latest release is 3.14.8 (2026-09-30) (python.org/downloads). `[DOC 2026-10-19]`
- Wheels for Python 3.15 on PyPI, for the locked dependency set (`uv pip compile` with
  `--only-binary :all:` for `x86_64-manylinux_2_28` and `aarch64-manylinux_2_28`): every package
  resolves to a wheel except **PyYAML 6.0.3** (released 2025-09-25, no cp315 wheel; it builds from
  its sdist), and on aarch64 **grimp 3.17** (import-linter's graph library, a development tool),
  which ships cp315 wheels for x86_64 and i686 only. `[DOC 2026-10-19]`

## Java

- Eclipse Temurin builds an LTS release "as long as the corresponding upstream source is actively
  maintained": Java 11 and 17 at least to October 2027, Java 21 at least to December 2029,
  Java 25 at least to September 2031 (adoptium.net/support). `[DOC 2026-10-19]`
- Ubuntu's OpenJDK packages (packages.ubuntu.com, `-jre-headless`), current versions per release:

  | Release | 11 | 17 | 21 | 25 | `default-jre` |
  |---|---|---|---|---|---|
  | 22.04 | 11.0.32.1 (main) | 17.0.20.1 (universe) | 21.0.12.1 (universe) | 25.0.4.1 (universe) | 11 |
  | 24.04 | 11.0.32.1 (universe) | 17.0.20.1 (main) | 21.0.12.1 (main) | 25.0.4.1 (universe) | 21 |
  | 26.04 | 11.0.32.1 (universe) | 17.0.20.1 (universe) | 21.0.12.1 (universe) | 25.0.4.1 (main) | 25 |

  `[DOC 2026-10-19]`
- Debian 12 (bookworm) `default-jre-headless` is OpenJDK 17 (packages.debian.org).
  `[DOC 2026-10-19]`

## Ubuntu

- End of standard security maintenance: 22.04 LTS May 2027, 24.04 LTS May 2029, 26.04 LTS May
  2031; Ubuntu Pro extends each by five years (ubuntu.com/about/release-cycle). `[DOC 2026-10-19]`

## Playwright and Chromium

- Releases: 1.56.0 (2025-11-11), 1.57.0 (2025-12-09), 1.58.0 (2026-01-30), 1.59.0 (2026-04-29),
  1.60.0 (2026-05-18), 1.61.0 (2026-06-29), 1.62.0 (2026-07-31), 1.63.0 (2026-09-15), 1.64.0
  (2026-10-13) (pypi.org). `[DOC 2026-10-19]`
- System requirements: Debian 12 and 13, Ubuntu 22.04, 24.04 and 26.04, x86-64 or arm64
  (playwright.dev/python/docs/intro). Ubuntu 26.04 is supported from 1.61; 1.63 dropped Ubuntu
  20.04; from 1.57 Chromium comes as Chrome for Testing builds, headless mode using
  `chrome-headless-shell` (release notes). `[DOC 2026-10-19]`

## uv

- 0.13.1 was released on 2026-10-14; 0.12.23 on 2026-10-03 (pypi.org). `[DOC 2026-10-19]`
