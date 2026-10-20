#!/bin/bash
# Creates the rulesets the pipeline relies on (prove-gate checks them): the default branch takes only a PR with a
# code-owner approval and the given CI checks; everyone but admins may push only agent/** branches and no tags.
# Needs a public repo, or GitHub Pro/Team for a private one. Usage: rulesets.sh <owner>/<repo> <check> [<check>…]
set -euo pipefail

[ $# -ge 2 ] || { echo "usage: $0 <owner>/<repo> <required check> [<check>…]" >&2; exit 2; }
repo=$1
shift
# integration 15368 is GitHub Actions: only its runs count as the check.
checks=$(printf '%s\n' "$@" | jq -R '{context: ., integration_id: 15368}' | jq -s -c .)
admin='[{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]'
locked='[{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "creation"}, {"type": "update"}]'

create() {
    gh api -X POST "repos/$repo/rulesets" --input - --jq '"created ruleset \(.name) (\(.id))"'
}

create <<JSON
{"name": "main", "target": "branch", "enforcement": "active",
 "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}],
 "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
 "rules": [{"type": "deletion"}, {"type": "non_fast_forward"},
  {"type": "pull_request", "parameters": {"allowed_merge_methods": ["merge", "squash", "rebase"],
   "dismiss_stale_reviews_on_push": true, "require_code_owner_review": true, "require_last_push_approval": true,
   "required_approving_review_count": 1, "required_review_thread_resolution": false}},
  {"type": "required_status_checks", "parameters": {"do_not_enforce_on_create": false,
   "strict_required_status_checks_policy": false, "required_status_checks": $checks}}]}
JSON

create <<JSON
{"name": "agent-only", "target": "branch", "enforcement": "active", "bypass_actors": $admin,
 "conditions": {"ref_name": {"include": ["~ALL"], "exclude": ["~DEFAULT_BRANCH", "refs/heads/agent/**"]}},
 "rules": $locked}
JSON

create <<JSON
{"name": "agent-only-tag", "target": "tag", "enforcement": "active", "bypass_actors": $admin,
 "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}}, "rules": $locked}
JSON
