# ObliskIQ: the service, the cockpit and the Client Portal Gateway, in one image.
#
# Built from the repository root:  docker build -f deploy/Dockerfile -t obliskiq .
# Releases publish it for linux/amd64 and linux/arm64 as ghcr.io/almoon-d/obliskiq:<version>.
#
# One image, two containers on the machine: `obliskiq serve` (the bot; with transport `gateway`
# it installs, starts and logs in IBKR's pinned Client Portal Gateway as its own child process,
# docs/DEPLOY.md, DECISIONS D175) and `obliskiq cockpit serve` (the MCP cockpit and the Telegram
# bot; no broker session). The image holds no state and no secret: the store, the settings audit,
# the Gateway's install and the owner's settings live on the machine, mounted at /var/lib/obliskiq
# and /etc/obliskiq/owner.
#
# The base is the OS D96 chose (Ubuntu 26.04). Python is the uv-managed 3.14 the project locks;
# Java is a Temurin build of the major version `ibkr.gateway.java_major_versions` lists; Chromium
# is the headless shell the locked Playwright installs, which `launch(headless=True)` runs for the
# Gateway's login.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# The uv the CI uses (ci.yml).
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

# Temurin 11: the Java the Gateway has run on in T1 (docs/knowledge/ibkr.md).
FROM eclipse-temurin:11-jre-noble AS java

FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/obliskiq/.venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PYTHONUNBUFFERED=1

# sqlite3 for the backup's online copy and integrity check; zstd to compress it; tini so signals
# reach the service and zombies are reaped; ca-certificates for every TLS call.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates sqlite3 zstd tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=java /opt/java/openjdk /opt/java/openjdk

WORKDIR /opt/obliskiq

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock README.md LICENSE .python-version ./
RUN uv python install 3.14 \
 && uv sync --locked --no-dev --no-install-project

# The checkout the service reads beside its package: the configuration's expert defaults, the
# research registry and pre-registrations (the scoreboard and the panel read them).
COPY src ./src
COPY config ./config
COPY research ./research
RUN uv sync --locked --no-dev \
 && .venv/bin/playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

# Never root: the service holds the store and the paper login.
RUN groupadd --system --gid 10001 obliskiq \
 && useradd --system --uid 10001 --gid obliskiq --home-dir /var/lib/obliskiq obliskiq \
 && mkdir -p /var/lib/obliskiq /etc/obliskiq/owner \
 && chown obliskiq:obliskiq /var/lib/obliskiq /etc/obliskiq/owner

ENV PATH=/opt/obliskiq/.venv/bin:$PATH \
    OBLISK_CONFIG_DIR=/opt/obliskiq/config \
    OBLISK_OWNER_FILE=/etc/obliskiq/owner/owner.yaml \
    OBLISK_DATA_DIR=/var/lib/obliskiq \
    OBLISK_IBKR_GATEWAY_JAVA_HOME=/opt/java/openjdk \
    OBLISK_LOG_FORMAT=json

USER obliskiq
VOLUME ["/var/lib/obliskiq"]

# Healthy while the service's heartbeat is fresh; the cockpit's container overrides it.
HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
    CMD ["obliskiq", "health"]

ENTRYPOINT ["/usr/bin/tini", "--", "obliskiq"]
CMD ["serve"]
