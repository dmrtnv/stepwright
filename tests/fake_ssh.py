"""A fake `ssh-keygen` for the tests that sign and verify release tags: `fake_ssh.py <ssh-keygen arguments>`. git runs
it for `tag -s` with gpg.format ssh (`-Y sign`) and for `verify-tag` (`-Y find-principals`, `-Y verify`,
`-Y check-novalidate`). A signature's body is the signing key itself, so a verify matches it against the lines of the
allowed signers file. install() writes the `ssh-keygen` wrapper into a test's bin. Not a test module: discovery skips
it.
"""
import base64
import shlex
import sys
from pathlib import Path

TRUSTED_KEY = 'ssh-ed25519 ' + base64.b64encode(b'trusted release key').decode()
ATTACKER_KEY = 'ssh-ed25519 ' + base64.b64encode(b'attacker key').decode()
BEGIN = '-----BEGIN SSH SIGNATURE-----'
END = '-----END SSH SIGNATURE-----'


def install(bin_dir):
    wrapper = Path(bin_dir) / 'ssh-keygen'
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} '
                       '"$@"\n')
    wrapper.chmod(0o755)


def option(arguments, name):
    return arguments[arguments.index(name) + 1]


def signed_key(signature_path):
    lines = Path(signature_path).read_text().splitlines()
    body = ''.join(lines[lines.index(BEGIN) + 1:lines.index(END)]) if BEGIN in lines and END in lines else ''

    try:
        marker = base64.b64decode(body, validate=True).decode()
    except ValueError:
        return None

    return marker.removeprefix('key::') if marker.startswith('key::') else None


def principals(signers_path, key):
    """The principal of each allowed signers line whose key type and key are the signing key."""
    found = []

    for line in Path(signers_path).read_text().splitlines():
        fields = line.split()
        pairs = {' '.join(fields[index:index + 2]) for index in range(1, len(fields) - 1)}

        if fields and key in pairs:
            found.append(fields[0])

    return found


def main(arguments):
    action = option(arguments, '-Y')

    if action == 'sign':
        key = ' '.join(Path(option(arguments, '-f')).read_text().split()[:2])
        body = base64.b64encode(f'key::{key}'.encode()).decode()
        Path(arguments[-1] + '.sig').write_text(f'{BEGIN}\n{body}\n{END}\n')
        return 0

    key = signed_key(option(arguments, '-s'))

    if action == 'find-principals':
        found = principals(option(arguments, '-f'), key) if key else []
        print('\n'.join(found))
        return 0 if found else 1

    if action == 'verify':
        principal = option(arguments, '-I')

        if key and principal in principals(option(arguments, '-f'), key):
            print(f'Good "git" signature for {principal} with ED25519 key SHA256:fake')
            return 0

        print('Could not verify signature.', file=sys.stderr)
        return 255

    if action == 'check-novalidate':
        print('Good "git" signature with ED25519 key SHA256:fake')
        return 0

    print(f'fake ssh-keygen: unexpected -Y {action}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
