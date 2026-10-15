# podman-ubuntu

Current Podman, packaged the Debian way, for every Ubuntu release that isn't
EOL. A scheduled watcher tracks upstream Podman releases and the Ubuntu
support calendar; every new release becomes a pull request, every merged PR
becomes a signed apt repository published on GitHub Pages.

This project is not affiliated with the Podman project or Canonical.

## Install

```sh
# Trust the repository signing key (world-readable: apt verifies signatures
# as an unprivileged sandbox user, so a restrictive root umask would break
# verification with a cryptic "Unknown error executing apt-key")
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://kush-gupt.github.io/podman-ubuntu/keys/apt-signing.asc \
  | sudo tee /etc/apt/keyrings/podman-ubuntu.asc > /dev/null
sudo chmod 644 /etc/apt/keyrings/podman-ubuntu.asc

# Add the repository (pick the suite matching your Ubuntu version and track)
UBUNTU=$(lsb_release -rs | tr -d .)
sudo tee /etc/apt/sources.list.d/podman-ubuntu.sources << EOF
Types: deb
URIs: https://kush-gupt.github.io/podman-ubuntu
Suites: stable-${UBUNTU}
Components: main
Signed-By: /etc/apt/keyrings/podman-ubuntu.asc
EOF

# Pin this repository above Ubuntu's archive. Ubuntu ships some of the
# same packages (aardvark-dns, netavark), occasionally at a higher
# Debian revision of the same upstream version; without the pin, apt
# mixes Ubuntu's builds into the stack, and they install to different
# paths than podman expects. Priority 1001 also lets the pin replace
# an already-installed Ubuntu build of the same package.
sudo tee /etc/apt/preferences.d/podman-ubuntu.pref > /dev/null << EOF
Package: *
Pin: origin kush-gupt.github.io
Pin-Priority: 1001
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
   build; a deep smoke test (`scripts/smoke_test.sh`) installs the debs in
   a fresh container, upgrades them over the currently published stack,
   and exercises networking, container DNS, and a rootless run.
3. **Publish** (`scripts/publish_repo.py`): assembles the apt repository
   with `reprepro` on a dedicated `apt-repo` branch, prunes to the latest
   three versions per suite, drops suites whose Ubuntu release has gone
   EOL, records the build manifest on the `apt-repo` branch (not main),
   signs with a passphrase-less CI subkey, and deploys to GitHub Pages.

Verification does not end at publish: `verify-published` installs the
just-published stack from the live repository, a daily `repo-health`
workflow re-checks every live suite's index completeness and installs
from it, and a weekly `reproducibility` workflow rebuilds one
component on two independent runners and compares the .deb hashes.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design,
[docs/RUNBOOK.md](docs/RUNBOOK.md) for operating it, and
[docs/SIGNING.md](docs/SIGNING.md) for key management.

## Verify a download

Beyond the apt signature, every published `.deb` carries a GitHub
build provenance attestation. To verify a `.deb` you downloaded from
the repository:

```sh
gh attestation verify <file.deb> --repo kush-gupt/podman-ubuntu
```

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
