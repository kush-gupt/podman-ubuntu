#!/usr/bin/env bash
# Install the currently published stack for one cell from the LIVE
# Pages apt repository, using the documented README recipe (key,
# .sources file, apt pin priority 1001).
#
# Usage: install_published.sh <track> <ubuntu_version> [expected_podman_deb_version]
#
# Tracks: stable/v5 install the podman-suite meta-package. nightly
# publishes podman only (no suite package, by design), so a nightly
# cell adds the stable suite of the same Ubuntu release as the stack
# source and installs podman (the nightly version wins on version
# comparison) plus the stack components explicitly.
#
# Exit codes: 0 ok; 3 = this cell has no published podman yet (new
# suite/track), reported explicitly so callers can decide; other =
# real failure.
#
# Side effects callers rely on:
#   /tmp/podman-before.txt  the installed podman deb version
#   a basic Alpine run, so "the published stack worked before we
#   touched it" is proven, not assumed (upgrade-path smoke).
set -euo pipefail

TRACK="$1"
UVER="$2"
EXPECTED="${3:-}"
BASE="https://kush-gupt.github.io/podman-ubuntu"
ARCH="$(dpkg --print-architecture)"
FLAT="$(echo "$UVER" | tr -d .)"
SUITE="${TRACK}-${FLAT}"

index_url() { echo "$BASE/dists/$1/main/binary-$ARCH/Packages"; }
index_podman_version() {
  # No early exit in the awk: it must consume the whole stream.
  # Exiting on the match closes the pipe while curl is still
  # writing a chunked response; curl then dies with exit 23
  # (EPIPE, "failure writing output") and pipefail + set -e kill
  # the script. That race failed 1 of 30 install cells on
  # 2026-10-15 (smoke 22.04/v5/arm64, orchestrate 38776419215).
  # The `|| true` matters just as much: for a suite that has never
  # published (a new Ubuntu release's first run) the index URL is a
  # 404, curl exits 22, and pipefail would propagate that as this
  # function's status, so the caller died with exit 22 before ever
  # reaching the empty-result check that produces the documented
  # exit 3. A failed fetch means "no published version", which is
  # exactly what an empty result already means at both call sites.
  curl -fsSL "$(index_url "$1")" 2>/dev/null | awk \
    '/^Package: /{p=($2=="podman")} p && /^Version: / && !seen {print $2; seen=1}' \
    || true
}

apt-get update -qq
apt-get install -y -qq curl ca-certificates

if [ -n "$EXPECTED" ]; then
  # Pages deploys can lag a publish by a few minutes; wait until the
  # live index actually serves the version this run published.
  for i in $(seq 1 30); do
    if [ "$(index_podman_version "$SUITE")" = "$EXPECTED" ]; then
      break
    fi
    if [ "$i" = "30" ]; then
      echo "ERROR: live index for $SUITE never served podman $EXPECTED" >&2
      exit 1
    fi
    sleep 20
  done
fi

PUBLISHED="$(index_podman_version "$SUITE")"
if [ -z "$PUBLISHED" ]; then
  echo "NO-PUBLISHED-STACK: suite $SUITE has no podman in the live index yet"
  exit 3
fi

mkdir -p /etc/apt/keyrings
curl -fsSL "$BASE/keys/apt-signing.asc" -o /etc/apt/keyrings/podman-ubuntu.asc
chmod 644 /etc/apt/keyrings/podman-ubuntu.asc
{
  printf 'Types: deb\nURIs: %s\nSuites: %s\nComponents: main\nSigned-By: /etc/apt/keyrings/podman-ubuntu.asc\n' \
    "$BASE" "$SUITE"
  if [ "$TRACK" = "nightly" ]; then
    # The blank line matters: deb822 stanzas are paragraphs. Without
    # it the two stanzas parse as one and the nightly suite is lost.
    printf '\nTypes: deb\nURIs: %s\nSuites: stable-%s\nComponents: main\nSigned-By: /etc/apt/keyrings/podman-ubuntu.asc\n' \
      "$BASE" "$FLAT"
  fi
} > /etc/apt/sources.list.d/podman-ubuntu.sources
mkdir -p /etc/apt/preferences.d
printf 'Package: *\nPin: origin kush-gupt.github.io\nPin-Priority: 1001\n' \
  > /etc/apt/preferences.d/podman-ubuntu

apt-get update -qq
if [ "$TRACK" = "nightly" ]; then
  apt-get install -y -qq podman conmon crun netavark aardvark-dns passt \
    containers-common
else
  apt-get install -y -qq podman-suite
fi

INSTALLED="$(dpkg-query -W -f='${Version}' podman)"
if [ "$INSTALLED" != "$PUBLISHED" ]; then
  echo "ERROR: installed podman $INSTALLED, live $SUITE index serves $PUBLISHED" >&2
  exit 1
fi
echo "$INSTALLED" > /tmp/podman-before.txt
podman --version
podman --storage-driver=vfs run --cgroups=disabled --rm \
  docker.io/library/alpine:latest echo "published stack ok: $TRACK $UVER $ARCH"
