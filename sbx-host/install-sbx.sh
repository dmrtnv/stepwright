#!/bin/bash
# One-time sbx setup for your user, once root installed docker-sbx (Docker's apt repo) and put you in the kvm group.
# Idempotent. Then: `sbx login`, `pipeline template build`, `sandbox/gate.py --repo <checkout>`.
# Docs: https://docs.docker.com/ai/sandboxes/install/ · policy: https://docs.docker.com/ai/sandboxes/governance/access-controls/local/
set -euo pipefail

[ "$(id -u)" != 0 ] || { echo "run as your user, not root (sbx refuses root)" >&2; exit 1; }
command -v sbx >/dev/null || { echo "no sbx: install docker-sbx (https://docs.docker.com/ai/sandboxes/install/)" >&2; exit 1; }
id -nG | tr ' ' '\n' | grep -qx kvm ||
    { echo "not in kvm: sudo usermod -aG kvm \"\$USER\", then log in again (or newgrp kvm)" >&2; exit 1; }

export SBX_NO_TELEMETRY=1
# Every sbx call needs the capped daemon up first: one would autostart an uncapped daemon outside sbx.service.
systemctl --user start sbx.service

# Locked Down: nothing leaves a sandbox unless a per-sandbox rule allows it (the pipeline and ci-runner add them).
# Must precede every other sbx command (else an interactive first-run prompt); one-time, so a rerun fails here.
sbx policy init deny-all || echo "policy already initialized; check it is deny-all: sbx policy ls --wide" >&2

sbx version
sbx settings set ssh.agentForwardingEnabled false

# Settings apply on a daemon restart, which ends every VM: only when none runs.
if [ "$(sbx ls --json | jq '[.[] | select(.status != "stopped")] | length')" = 0 ]; then
    systemctl --user restart sbx.service
else
    echo "VMs are running: restart sbx.service once none does (pipeline status, ci-runner status)" >&2
fi

echo "next: sbx login   (Docker account, browser OAuth)" >&2
echo "then: pipeline template build && sandbox/gate.py --repo <checkout>" >&2
