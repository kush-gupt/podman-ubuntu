# Runbook

## A new podman release landed upstream

1. The watch workflow opens a PR within 6 hours. Review it.
2. Fill the component pins in `packaging/components.json` from the upstream
   release notes (crun, conmon, netavark, aardvark-dns, passt,
   containers-common). Check the notes for new Go/Rust minimums; bump
   `GO_VERSION`/`RUST_VERSION` in `images/builder.Containerfile` if needed.
3. Merge. The build matrix fans out automatically; Pages updates when the
   publish job finishes.

## A build cell failed

1. Open the failed `build-component` run; the sbuild log is in the job output.
2. Common causes:
   - **Missing build-dep**: add it to `Build-Depends` in the component's
     `debian/control`. (The builder image carries the union of all deps, so
     a missing `Build-Depends` only shows up here — this is intentional, it
     keeps the packaging honest for Debian.)
   - **Upstream build system change**: fix `debian/rules`, usually an
     override target.
   - **Vendoring failure**: upstream restructured modules; adjust
     `prepare-source.sh`.
3. Push the fix to `main`; only the failed cells rebuild (skip logic reads
   `manifest.json`).

## The Ubuntu set changed

The watcher opens the PR (new interim release, or one going EOL). Review and
merge. New releases get builder images built automatically by `builder.yml`;
EOL'd releases stop building but keep their published suites.

## Lintian gate failed

Warnings are reported, errors fail. Fix the packaging; do not add lintian
overrides without a comment explaining why upstream acceptance would also
accept it. The goal is zero overrides.

## apt repository looks wrong

The full history is the `apt-repo` branch: `git log apt-repo`, and
`reprepro -b <checkout> list <suite>` shows exactly what's published. To
roll back a bad publish: `git revert` the publish commit on `apt-repo` and
push; Pages redeploys from the branch.

## Signing key rotation

See [SIGNING.md](SIGNING.md). Short version: generate a new subkey,
replace the `APT_SIGNING_SUBKEY` secret, replace `keys/apt-signing.asc`,
merge. Old signatures remain verifiable against the old public key, which
stays in git history.

## Proposing to Debian/Ubuntu (sponsor workflow)

1. Pick one component, e.g. `packaging/podman`.
2. `git archive` the `debian/` dir into a clone of the Salsa packaging repo
   (or start a new one following DEP-14: `debian/latest` branch).
3. Build with `sbuild` locally; confirm lintian-clean and autopkgtest-pass.
4. Upload to mentors.debian.net with `dput`; file an RFS bug against
   `sponsorship-requests` with the checklist from the Debian wiki.
5. Once in Debian unstable, Ubuntu syncs it automatically (sync requests for
   interim Ubuntu releases if needed).
