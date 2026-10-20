#!/bin/bash
# Build the sandbox template with the host's Node and Claude Code (or the given version) and load it into sbx.
# Usage: build-template.sh [<claude-version>]   (prints the tag last; sbx must be installed for the load step)
# The runner calls it (config.json "template"); by hand: `pipeline template build [<version>]`.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
node_version=$(node --version | sed 's/^v//')
claude_version=${1:-$(claude --version | awk '{print $1}')}
tag="pipeline-sandbox:claude-${claude_version}-node-${node_version}"
tar="${XDG_CACHE_HOME:-$HOME/.cache}/pipeline/${tag//[:\/]/_}.tar"

docker build --build-arg "BASE=$(cat "$here/../sbx-host/base-image")" --build-arg "NODE_VERSION=${node_version}" --build-arg "CLAUDE_VERSION=${claude_version}" -t "$tag" "$here" >&2
mkdir -p "$(dirname "$tar")"
docker image save "$tag" -o "$tar"

if command -v sbx >/dev/null; then
    sbx template load "$tar" >&2
    rm -f "$tar"
else
    echo "sbx not installed: image saved to $tar; run 'sbx template load $tar' after installing" >&2
fi

echo "$tag"
