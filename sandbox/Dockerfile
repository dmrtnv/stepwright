# Pipeline sandbox template: Docker's claude-code-docker template (its label start-docker=true gives each VM its own dockerd) + the host's Node and Claude Code versions.
# Built by build-template.sh (never by hand: it passes the host versions and loads the image into sbx).
# BASE: ../sbx-host/base-image (one digest for every sbx template on this box).
ARG BASE
FROM ${BASE}

ARG NODE_VERSION
ARG CLAUDE_VERSION

USER root
RUN set -eux; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz"; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" | grep " node-v${NODE_VERSION}-linux-x64.tar.gz\$" | sha256sum -c -; \
    tar -xzf "node-v${NODE_VERSION}-linux-x64.tar.gz" -C /usr/local --strip-components=1 --no-same-owner; \
    rm "node-v${NODE_VERSION}-linux-x64.tar.gz"; \
    /usr/local/bin/corepack enable --install-directory /usr/local/bin; \
    /usr/local/bin/node --version | grep -qx "v${NODE_VERSION}"

USER agent
RUN set -eux; \
    curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}"; \
    /home/agent/.local/bin/claude --version | grep -q "^${CLAUDE_VERSION} "

# One CLI version everywhere: the runner upgrades by rebuilding this image, never inside a sandbox.
ENV DISABLE_AUTOUPDATER=1 \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0
