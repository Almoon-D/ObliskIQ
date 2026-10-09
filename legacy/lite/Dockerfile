# obliskiq-lite-mcp — the MCP server, containerised.
#
# The server ONLY. The collectors and the computation layer are separate
# entrypoints on purpose: they hold IBKR credentials, bind nothing, and run under
# a timer with no human present. Running them inside this image would put
# credentials in a process that listens on a port and couple two things with very
# different blast radii. Both are installed here so `docker exec` can trigger a
# catch-up run — run_collectors spawns obliskiq-collect as a child process —
# but the container's job is to serve.
#
# Nothing is assumed about the filesystem beyond OBLISK_DB_PATH.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip build \
 && python -m build --wheel --outdir /dist


FROM python:3.12-slim

# Never root. The server holds a position ledger; a container escape should not
# also be a root shell.
RUN useradd --create-home --uid 10001 obliskiq

COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# config.yaml owns every threshold and is read at runtime, so it is copied rather
# than baked into the wheel: an operator changes a gate by editing the file and
# restarting, not by rebuilding.
COPY config.yaml universe.yaml calendars.yaml /opt/obliskiq/

# The database lives on a volume. The image holds no state at all.
RUN mkdir -p /var/lib/obliskiq && chown obliskiq:obliskiq /var/lib/obliskiq
VOLUME ["/var/lib/obliskiq"]

USER obliskiq
WORKDIR /opt/obliskiq

ENV OBLISK_CONFIG_PATH=/opt/obliskiq/config.yaml \
    OBLISK_DB_PATH=/var/lib/obliskiq/obliskiq.db \
    OBLISK_LOG_FORMAT=json \
    OBLISK_TZ=UTC \
    OBLISK_MCP_HOST=0.0.0.0 \
    OBLISK_MCP_PORT=8000 \
    PYTHONUNBUFFERED=1

# OBLISK_MCP_AUTH_TOKEN is deliberately NOT set here. The default host binds
# beyond loopback, so the server refuses to start without one — a container that
# would have served the ledger unauthenticated fails at startup instead.

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["obliskiq-mcp"]
CMD ["http"]
