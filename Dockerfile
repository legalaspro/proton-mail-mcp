# For MCP registries that check a server by starting it, such as Glama. The image runs
# `proton-mail-mcp introspect`: it lists the tools on stdin and stdout with no Bridge, settings
# or credentials, and refuses every tool call, so it can neither read nor send mail. To use the
# server, install it on the computer that runs Bridge (docs/install.md).
#
#   docker build -t proton-mail-mcp-introspect .
#   scripts/docker_smoke.sh
#
# Every build of one commit gets the same inputs: python:3.12-slim pinned to one multi-arch index
# digest, and docker/constraints.txt pinning each runtime package to the version the test suite
# ran with. To refresh both, see "Docker image" in docs/install.md.

FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS build
WORKDIR /src
# This checkout, not PyPI, so the image lists the tools of the commit it was built from.
COPY pyproject.toml README.md LICENSE .gitignore ./
COPY docker/constraints.txt docker/constraints.txt
COPY examples/ examples/
COPY src/ src/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
        --constraint docker/constraints.txt .

FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
LABEL org.opencontainers.image.title="proton-mail-mcp introspect" \
      org.opencontainers.image.description="Lists the proton-mail-mcp tools over stdio; every tool call refuses" \
      org.opencontainers.image.source="https://github.com/legalaspro/proton-mail-mcp" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=build /opt/venv /opt/venv
RUN groupadd --gid 10001 mcp \
    && useradd --uid 10001 --gid mcp --create-home --shell /usr/sbin/nologin mcp
USER 10001:10001
WORKDIR /home/mcp
CMD ["proton-mail-mcp", "introspect"]
