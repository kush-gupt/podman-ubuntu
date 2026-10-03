# Architecture

## Goal

Ship current Podman on every non-EOL Ubuntu release via a signed apt
repository, with packaging shaped from day one for acceptance into Debian
(and therefore Ubuntu) proper.

## Data flow

```
upstream releases ──watch.yml──▶ PR (versions.json, ubuntu-versions.json)
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
                    publish-repo.sh ──▶ apt-repo branch ──▶ GitHub Pages
```

## Watermarks

Two JSON files are the only mutable state the automation reads:

- `versions.json`: podman version per track (`stable`, `v5`).
- `ubuntu-versions.json`: supported Ubuntu releases.
- `packaging/components.json`: the full dependency stack pinned per podman
  version. The single source of truth for "what builds with what".
- `manifest.json`: `(component, version)` pairs already published; drives
  skip logic.

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
  in every `debian/` dir for Ubuntu's infrastructure; a CI smoke job installs
  the debs in a fresh container per Ubuntu release.
- **Native runners**: `ubuntu-24.04` / `ubuntu-24.04-arm`, both free on public
  repos. No QEMU anywhere in the pipeline.

## Publishing model

`reprepro` manages suites named `<track>-<flat-ubuntu>` (e.g.
`stable-2404`) on a dedicated orphan `apt-repo` branch, which doubles as the
repository's history. Each publish: include new debs, prune to the newest
three versions per package per suite (GitHub Pages has a 1 GB soft limit),
`reprepro export` signs with the passphrase-less CI subkey (see
[SIGNING.md](SIGNING.md)), push the branch, deploy to Pages.

EOL'd Ubuntu releases drop out of the build matrix but their published suites
stay untouched: existing users keep working, no new builds ship.

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
