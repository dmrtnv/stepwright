# Security

## Report a vulnerability

Report it privately through GitHub:
[open a private report](https://github.com/dmrtnv/stepwright/security/advisories/new). You can also open the repo's
**Security** tab and click **Report a vulnerability**.

Don't open a public issue for a vulnerability.

## Supported versions

Fixes go into the latest tag only. Update to it as [INSTALL.md](INSTALL.md#update) says.

## What `pipeline update` trusts

`pipeline update` checks out only a release tag signed by a key in `.github/allowed_signers` as committed at the
checkout you have installed:
- It reads that file from the installed commit, never from the target tag or from the work tree. A pull request that
  edits the file changes nothing until a release that the old key signed ships it.
- On an untagged `main` commit, that commit's file is the one it trusts.
- Only an SSH signature counts. Your own gpg keyring, gpgsm or `gpg.ssh.program` never decide.
- The tag must carry its own name, so an old signed tag served under a newer name is refused.

What it does not verify:
- the fresh install (INSTALL step 2) and the manual update commands;
- the move out of v0.9: v0.9's `pipeline update` has no check, so the move into the next release is not verified.

## What stepwright protects

[Before you run it](README.md#before-you-run-it) lists what the sandbox protects and what it doesn't, what leaves
your machine and what the GitHub App can do.
