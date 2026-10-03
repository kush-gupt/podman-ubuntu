#!/usr/bin/env bash
# Prepare a Debian source package for one component.
# Usage: prepare-source.sh <component> <version> <ubuntu_version>
#
# Downloads the upstream tag, vendors language dependencies (network is
# available in this step; the sbuild chroot is offline), overlays
# packaging/<component>/debian, stamps the changelog, and produces a .dsc
# ready for sbuild. Leaves everything under build/.
set -euo pipefail

COMPONENT="$1"
VERSION="$2"
UBUNTU_VERSION="$3"
# Optional 4th arg: component-specific extra data (passt snapshot sha).
EXTRA="${4:-}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$REPO_ROOT/build"
SRC="$BUILD/src"
ART="$BUILD/artifacts"
mkdir -p "$SRC" "$ART"

export DEBFULLNAME="Kush Gupta"
export DEBEMAIL="kushalgupta@gmail.com"

# Per-component source location. TARBALL is the upstream archive URL;
# SUBDIR (optional) selects a subdirectory of the extracted tree as the
# real source root (container-libs hosts several Go modules in one repo).
TARBALL=""
SUBDIR=""
KIND=""
case "$COMPONENT" in
  podman)          TARBALL="https://github.com/containers/podman/archive/refs/tags/v${VERSION}.tar.gz"; KIND=go ;;
  crun)            TARBALL="https://github.com/containers/crun/archive/refs/tags/${VERSION}.tar.gz"; KIND=c ;; # no v prefix
  conmon)          TARBALL="https://github.com/containers/conmon/archive/refs/tags/v${VERSION}.tar.gz"; KIND=c ;;
  netavark)        TARBALL="https://github.com/containers/netavark/archive/refs/tags/v${VERSION}.tar.gz"; KIND=rust ;;
  aardvark-dns)    TARBALL="https://github.com/containers/aardvark-dns/archive/refs/tags/v${VERSION}.tar.gz"; KIND=rust ;;
  passt)
    [ -n "$EXTRA" ] || { echo "passt requires the snapshot sha as \$4" >&2; exit 1; }
    TARBALL="https://passt.top/passt/snapshot/passt-${EXTRA}.tar.xz"; KIND=c ;;
  containers-common)
    # go.podman.io/common lives in podman-container-tools/container-libs.
    TARBALL="https://github.com/podman-container-tools/container-libs/archive/refs/tags/common/v${VERSION}.tar.gz"
    SUBDIR="common"; KIND=config ;;
  podman-suite)    KIND=meta ;;
  *) echo "Unknown component: $COMPONENT" >&2; exit 1 ;;
esac

workdir="$SRC/${COMPONENT}-${VERSION}"
rm -rf "$workdir"
mkdir -p "$workdir"

if [ "$COMPONENT" = "podman-suite" ]; then
  # Meta-package generated from a template; no upstream source.
  mkdir -p "$workdir/debian"
  sed "s/@PODMAN_VERSION@/${VERSION}/g" \
    "$REPO_ROOT/packaging/podman-suite/debian.in/control" > "$workdir/debian/control"
  for f in rules changelog copyright; do
    cp "$REPO_ROOT/packaging/podman-suite/debian.in/$f" "$workdir/debian/$f"
  done
  mkdir -p "$workdir/debian/source" "$workdir/debian/tests"
  echo "3.0 (native)" > "$workdir/debian/source/format"
  cp "$REPO_ROOT/packaging/podman-suite/debian.in/tests-control" \
     "$workdir/debian/tests/control"
else
  echo "Fetching $TARBALL"
  case "$TARBALL" in
    *.tar.xz) TARFLAGS="-xJ" ;;
    *)        TARFLAGS="-xz" ;;
  esac
  # Extract into a dedicated temp dir so the top-level entry is
  # unambiguous (never confused with $workdir itself).
  tmpdir=$(mktemp -d)
  curl -fsSL "$TARBALL" | tar "$TARFLAGS" -C "$tmpdir"
  topcount=$(ls -A "$tmpdir" | wc -l)
  if [ "$topcount" -ne 1 ]; then
    echo "unexpected tarball layout ($topcount top-level entries)" >&2
    ls -A "$tmpdir" >&2
    exit 1
  fi
  srcpath="$tmpdir/$(ls -A "$tmpdir")"
  if [ -n "$SUBDIR" ]; then
    srcpath="${srcpath}/${SUBDIR}"
  fi
  [ -e "$srcpath" ] || { echo "missing $srcpath after extract" >&2; exit 1; }
  rm -rf "$workdir" && mv "$srcpath" "$workdir"
  rm -rf "$tmpdir"

  # Vendor language dependencies now; the sbuild chroot is offline.
  case "$KIND" in
    go)
      (cd "$workdir" && go mod vendor)
      ;;
    rust)
      (cd "$workdir" && cargo vendor >/dev/null)
      mkdir -p "$workdir/.cargo"
      cat > "$workdir/.cargo/config.toml" <<'EOF'
[source.crates-io]
replace-with = "vendored-sources"
[source.vendored-sources]
directory = "vendor"
EOF
      ;;
  esac

  # Overlay the debian/ directory (the file API does not preserve the
  # executable bit, so enforce it here).
  cp -r "$REPO_ROOT/packaging/$COMPONENT/debian" "$workdir/debian"
  chmod +x "$workdir/debian/rules"
fi

# Stamp the changelog for this exact version (no-op if already correct).
cur_ver=$(dpkg-parsechangelog -l"$workdir/debian/changelog" -S Version 2>/dev/null || echo none)
if [ "$cur_ver" != "${VERSION}-1" ]; then
  (cd "$workdir" && dch --newversion "${VERSION}-1" \
    --distribution unstable --urgency medium \
    "New upstream release ${VERSION}.")
fi

# Build the source package (.dsc + .orig.tar.gz).
(cd "$workdir" && dpkg-buildpackage -S -uc -us -d)

# Move source artifacts where sbuild expects them.
mv "$SRC"/*.dsc "$SRC"/*.orig.tar.gz "$SRC"/*.debian.tar.* "$ART/" 2>/dev/null || true
echo "Prepared ${COMPONENT} ${VERSION} in $ART"
ls "$ART"
