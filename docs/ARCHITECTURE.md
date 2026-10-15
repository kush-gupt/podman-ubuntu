# Architecture

## Goal

Ship current Podman on every non-EOL Ubuntu release via a signed apt
repository, with packaging shaped from day one for acceptance into Debian
(and therefore Ubuntu) proper.

## Data flow

```
upstream releases ──watch.yml──▶ PR (versions.json, ubuntu-versions.json,
                         │        packaging/components.json pins: the
                         │        watch also tracks new upstream tags
                         │        of crun, conmon, netavark, aardvark-dns,
                         │        containers-common, and passt HEAD)
                                        │
merge ──orchestrate.yml──▶ setup: build_matrix.py ──▶ matrices
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
              level0 (parallel)   level1 (after level0)   smoke test
              crun, conmon,       podman, podman-suite    install debs in
              netavark,                                  fresh container
              aardvark-dns,
              passt,
              containers-common
                    │                   │
                    └─────────┬─────────┘
                              ▼
                    publish_repo.py ──▶ apt-repo branch ──▶ GitHub Pages
```

## Watermarks

The mutable state the automation reads:

- `versions.json`: podman version per track (`stable`, `v5`).
- `ubuntu-versions.json`: supported Ubuntu releases.
- `packaging/components.json`: the full dependency stack pinned per podman
  version. The single source of truth for "what builds with what".
- `manifest.json`: `(component, version)` -> packaging hash for everything
  already published; drives skip logic. It lives on the `apt-repo` branch,
  written by the publish job atomically with the debs it describes and
  read from there by the build matrix. The copy on main is only the
  migration seed / offline fallback.

The watch workflow is the only writer of the first two; the publish job is
the only writer of the last. Nothing is ever hand-edited.

## Build model

- **Toolchain images** (`images/builder.Containerfile`, one tag per Ubuntu
  release, published to GHCR, rebuilt weekly): every build job runs inside
  the matching image, so jobs start in seconds with zero `apt-get`.
- **Real source packages**: `prepare-source.sh` fetches the upstream tag,
  vendors Go/Rust dependencies (the sbuild chroot is offline), overlays
  `packaging/<component>/debian`, stamps the changelog, and produces a `.dsc`.
- **sbuild `--chroot-mode=unshare`**: proper Debian source builds without
  maintaining chroot tarballs; the builder image itself is the build root.
- **Gates**: lintian `--fail-on error` on every build; DEP-8 smoke tests ship
  in every `debian/` dir for Ubuntu's infrastructure; a CI smoke job
  (`scripts/smoke_test.sh`) installs the debs in a fresh container per
  Ubuntu release, over the currently published stack, and exercises a
  basic run, container-to-container DNS on a custom network, and a
  rootless run (reported explicitly when the nested CI environment
  cannot support rootless, never silently skipped).
- **Native runners**: `ubuntu-24.04` / `ubuntu-24.04-arm`, both free on public
  repos. No QEMU anywhere in the pipeline.

## Publishing model

`reprepro` manages suites named `<track>-<flat-ubuntu>` (e.g.
`stable-2404`) on a dedicated orphan `apt-repo` branch, which doubles as the
repository's history. Each publish: include new debs, prune to the newest
three versions per package per suite (GitHub Pages has a 1 GB soft limit),
`reprepro export` signs with the passphrase-less CI subkey (see
[SIGNING.md](SIGNING.md)), push the branch, deploy to Pages.

When an Ubuntu release leaves `ubuntu-versions.json`, publish drops its
suites from the repository: `publish_repo.py` removes the stanzas whose
codename is no longer kept (`lib.kept_codenames`) and `reprepro
clearvanished` purges their database entries and files. Suites do not
linger with stale packages.

## Verification

- `verify-published` (orchestrate and nightly): after a publish, installs
  the stack from the live Pages repository in clean containers and runs
  a container, so signatures, indices, and the deployed files are what
  get tested, not local artifacts.
- `repo-health` (daily, plus on demand): `scripts/check_live_repo.py`
  asserts every live suite's Packages index is complete (the full
  8-package stack for stable/v5, podman for nightly), and a matrix job
  installs from the live repository and runs the deep smoke. A
  host-smoke job installs on the runner host itself (no job
  container) and runs `scripts/smoke_test.sh` with `SMOKE_HOST=1`,
  which asserts the defaults users get: overlay storage driver,
  cgroup v2, and rootless networking via pasta. A key-expiry job
  files an issue 60 days before the signing subkey expires
  (`scripts/check_key_expiry.py`). Failures go through
  `ci-failure-alert` like other CI failures.
- `reproducibility` (weekly and on packaging changes): builds one
  component twice per architecture on independent runners through
  the same builder-container path as `build.yml` and compares .deb
  sha256 hashes. The component rotates deterministically by ISO
  week over the buildable components; a mismatch produces a
  diffoscope report artifact.
- DEP-8 autopkgtests: the podman package's `debian/tests` run in CI
  (`autopkgtest --no-built-binaries ... -- null`, the null backend
  being what GitHub runners support) against the built .debs in
  orchestrate's smoke job and against the published stack in
  `script-tests.yml`, so a PR that breaks a DEP-8 test goes red.
- `vuln-scan` (weekly): collects every published .deb and scans it
  with grype; findings open a tracking issue, a clean scan closes it.
  Published .debs also carry GitHub build provenance attestations
  (orchestrate's `attest-debs` job): verify with
  `gh attestation verify <file.deb> --repo kush-gupt/podman-ubuntu`.
- The reprepro stale-replacement logic in `publish_repo.py` has a real
  reprepro integration test (`tests/test_reprepro_integration.py`),
  run by `script-tests.yml`; it encodes the manual sims that proved
  the October 2026 arch-blind and arch-all publish bugs. The same
  file drills the runbook rollback (`git revert` of a publish commit
  on the apt-repo branch) end to end.

## Debian/Ubuntu acceptance path

Podman already exists in Ubuntu (universe); the endgame is not a new package
but packaging credible enough to be adopted. Concretely:

- Every component has a real `debian/` dir: DEP-5 `copyright`, `debian/watch`
  (uscan), DEP-8 `debian/tests/`, `Standards-Version` current, lintian-clean.
- Each `packaging/<component>/` maps 1:1 to what would become that package's
  Salsa repository; proposing upstream is copying the dir over and opening a
  merge request against the Debian maintainer's repo.
- Go/Rust vendoring is done at orig-tarball creation time (in CI, where the
  network is available), so the sbuild itself is fully offline — the same
  constraint Debian buildds enforce.
- `docs/RUNBOOK.md` documents the sponsor workflow: `dput` to mentors,
  RFS bug template, and what "lintian-clean + autopkgtest-passing" evidence
  to attach.

## What Dependabot does not cover

Dependabot (grouped weekly PRs, auto-merged on green) keeps Actions and
Containerfile base images current. It does not bump the `GO_VERSION` /
`RUST_VERSION` args in the builder Containerfile; those ride the release-PR
checklist instead, since upstream's release notes state the minimums.
