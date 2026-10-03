# podman-ubuntu

Current Podman, packaged the Debian way, for every Ubuntu release that isn't
EOL. A scheduled watcher tracks upstream Podman releases and the Ubuntu
support calendar; every new release becomes a pull request, every merged PR
becomes a signed apt repository published on GitHub Pages.

This project is not affiliated with the Podman project or Canonical.

## Install

```sh
# Trust the repository signing key
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://kush-gupt.github.io/podman-ubuntu/keys/apt-signing.asc \
  | sudo tee /etc/apt/keyrings/podman-ubuntu.asc > /dev/null

# Add the repository (pick the suite matching your Ubuntu version and track)
UBUNTU=$(lsb_release -rs | tr -d .)
sudo tee /etc/apt/sources.list.d/podman-ubuntu.sources << EOF
Types: deb
URIs: https://kush-gupt.github.io/podman-ubuntu
Suites: stable-${UBUNTU}
Components: main
Signed-By: /etc/apt/keyrings/podman-ubuntu.asc
EOF

sudo apt update
sudo apt install -y podman-suite
```

Tracks: `stable` (latest Podman 6.x), `v5` (Podman 5.x maintenance line),
`nightly` (weekly build of upstream `main`). Suites are named
`<track>-<ubuntu>`, e.g. `stable-2404`, `v5-2604`, `nightly-2204`.

## How it works

Three moving parts, each with exactly one source of truth:

1. **Watch** (`.github/workflows/watch.yml`, every 6h): polls the upstream
   Podman releases API and the Ubuntu support calendar. New podman release or
   a change in the supported-Ubuntu set opens a pull request bumping
   `versions.json` / `ubuntu-versions.json`. The full dependency stack for
   each podman version is pinned in `packaging/components.json`; the PR
   checklist asks a human to fill the pins from the upstream release notes
   before merge. CI fails the PR if pins are missing.
2. **Build** (`.github/workflows/orchestrate.yml`): fans out a build DAG over
   the `ubuntu x arch x track` matrix. Components build in dependency order
   (crun, conmon, netavark, aardvark-dns, passt, containers-common in
   parallel, then podman) inside pre-baked toolchain Containerfiles, as real
   source packages via `sbuild --chroot-mode=unshare`. Lintian gates every
   build; a smoke test installs the debs in a fresh container.
3. **Publish** (`scripts/publish-repo.sh`): assembles the apt repository with
   `reprepro` on a dedicated `apt-repo` branch, prunes to the latest three
   versions per suite, signs with a passphrase-less CI subkey, and deploys to
   GitHub Pages.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design,
[docs/RUNBOOK.md](docs/RUNBOOK.md) for operating it, and
[docs/SIGNING.md](docs/SIGNING.md) for key management.

## Design goals

- **Maintainability**: one file to change per release, declarative tooling
  throughout, Dependabot on all CI dependencies with auto-merge.
- **Performance**: native amd64/arm64 runners (no emulation), pre-baked
  toolchain images, language-level build caches, and skip logic so released
  versions are never rebuilt.
- **Upstream-ready**: every component carries a real `debian/` directory
  (DEP-5 copyright, `debian/watch`, DEP-8 autopkgtests, lintian-clean) so the
  packaging can one day be proposed to Debian/Ubuntu proper. Each
  `packaging/<component>/` maps to what would become that package's Salsa
  repository.

## License

This repository's own files (workflows, scripts, Containerfiles, packaging)
are MIT licensed — see [LICENSE](LICENSE). The `.deb` packages built from it
carry their upstream licenses, documented in each `debian/copyright` file
(Podman/Conmon/Netavark: Apache-2.0; crun/passt: GPL-2.0-or-later).
