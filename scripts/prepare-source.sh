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

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$REPO_ROOT/build"
SRC="$BUILD/src"
ART="$BUILD/artifacts"
mkdir -p "$SRC" "$ART"

export DEBFULLNAME="Kush Gupta"
export DEBEMAIL="kushalgupta@gmail.com"

# component -> "repo_url|tag_prefix|kind"
# kind: go | rust | c | config | meta
declare -A UPSTREAM=(
  [podman]="https://github.com/containers/podman|v|go"
  [crun]="https://github.com/containers/crun|v|c"
  [conmon]="https://github.com/containers/conmon|v|c"
  [netavark]="https://github.com/containers/netavark|v|rust"
  [aardvark-dns]="https://github.com/containers/aardvark-dns|v|rust"
  [passt]="https://passt.top/passt|v|c" # snapshot tarballs; verify URL pattern on first run
  [containers-common]="https://github.com/containers/common|v|config"
  [podman-suite]="local||meta"
)

info="${UPSTREAM[$COMPONENT]:-}"
if [ -z "$info" ]; then
  echo "Unknown component: $COMPONENT" >&2
  exit 1
fi
IFS='|' read -r REPO_URL TAG_PREFIX KIND <<< "$info"

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
  tag="${TAG_PREFIX}${VERSION}"
  echo "Fetching $REPO_URL @ $tag"
  curl -fsSL "$REPO_URL/archive/refs/tags/$tag.tar.gz" \
    | tar -xz -C "$SRC"
  # Normalize the extracted top-level dir name.
  extracted=$(ls -dt "$SRC"/*/ | head -1)
  rm -rf "$workdir" && mv "$extracted" "$workdir"

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
cur_ver=$(dpkg-parsechangelog -S Version 2>/dev/null || echo none)
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
