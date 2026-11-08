"""The release tag of dev/release.py: sign it on the merge commit, verify it against the previous release's signers
file, push it."""
import core


def tag(directory, version, oid):
    reference = f'refs/tags/{version}'
    local = core.git_result(directory, 'rev-parse', '--verify', '--quiet', f'{reference}^{{commit}}')

    if local.returncode == 0 and local.stdout.strip() != oid:
        raise core.PipelineError(f'the local tag {version} is not on the merge commit {oid[:12]}: delete it with '
                                 f'git tag -d {version}, then rerun')

    if local.returncode != 0:
        signed = core.git_result(directory, 'tag', '-s', version, '-m', version, oid)

        if signed.returncode != 0:
            raise core.PipelineError(f'git tag -s {version}: {core.error_line(signed.stderr)}. Set git config '
                                     'gpg.format ssh and user.signingkey, then rerun')

    earlier = [release for release in core.release_tags(directory, '--merged', oid) if release != version]

    if not earlier:
        raise core.PipelineError(f'no release before {version} in the history of {oid[:12]}: runners would have no '
                                 'signers file to check it against')

    try:
        core.verify_tag(directory, version, earlier[0])
    except core.NoSignersFile as error:
        raise core.PipelineError(f'{error}, so runners there cannot check {version}: the first release this script '
                                 'can cut is the one after the release that adds the file') from None
    except core.PipelineError as error:
        raise core.PipelineError(f'{error}. Runners at {earlier[0]} would refuse it: sign with a key in its '
                                 f'{core.SIGNERS_FILE} (git config gpg.format ssh and user.signingkey), delete the '
                                 f'tag with git tag -d {version}, then rerun') from None

    core.git(directory, 'push', 'origin', reference)
    print(f'Tagged and pushed {version}')
