#!/usr/bin/python3
"""Answers the driver's questions about a Claude session transcript: `transcripts.py <session>` prints JSON
`{"exists": …, "resumable": …}`.

Claude Code writes ~/.claude/projects as 700/600 whatever the umask, so the driver runs this as the agent user
through `launch` instead of reading the agent's transcripts itself.
"""
import json
import sys
from pathlib import Path


def transcript_state(session, projects=Path.home() / '.claude/projects'):
    paths = list(projects.glob(f'*/{session}.jsonl'))
    resumable = False

    for path in paths:
        try:
            with path.open(errors='replace') as handle:
                if any(_is_user_line(line) for line in handle):
                    resumable = True
                    break
        except OSError:
            continue

    return {'exists': bool(paths), 'resumable': resumable}


def _is_user_line(line):
    try:
        return json.loads(line).get('type') == 'user'
    except (ValueError, AttributeError):
        return False


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('usage: transcripts.py <session>', file=sys.stderr)
        sys.exit(2)

    print(json.dumps(transcript_state(sys.argv[1])))
