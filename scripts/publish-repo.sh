#!/usr/bin/env bash
# Assemble the signed apt repository from built .debs and stage it for Pages.
#
# Env: APT_SIGNING_SUBKEY  base64 of the passphrase-less signing subkey (required)
#      TRACKS              comma-separated tracks to publish (default: all in versions.json)
#
# Layout: artifact dirs are named debs-<component>-<version>-<track>-u<ubuntu>-<arch>/
# Steps: gather -> reprepro includedeb per suite -> prune to newest 3 ->
#        sign -> commit+push apt-repo branch -> stage apt-repo-publish/ ->
#        record manifest.json on main.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

: "${APT_SIGNING_SUBKEY:?APT_SIGNING_SUBKEY is required}"

TRACKS="${TRACKS:-$(python3 -c "import json; print(','.join(json.load(open('versions.json')).keys()))")}"
IFS=',' read -ra TRACK_LIST <<< "$TRACKS"
mapfile -t UBUNTUS < <(python3 -c "import json; print('\n'.join(json.load(open('ubuntu-versions.json'))['releases']))")

flat() { echo "${1//./}"; }

# --- apt-repo branch worktree -------------------------------------------
if git show-ref --verify --quiet refs/heads/apt-repo; then
  git worktree add -f apt-repo apt-repo
elif git ls-remote --heads origin apt-repo | grep -q apt-repo; then
  git fetch origin apt-repo:apt-repo
  git worktree add -f apt-repo apt-repo
else
  git worktree add --detach apt-repo
  (cd apt-repo && git checkout --orphan apt-repo && git rm -rf . >/dev/null 2>&1 || true)
fi
APTD="$REPO_ROOT/apt-repo"
mkdir -p "$APTD/conf" "$APTD/pool" "$APTD/keys"

# --- GPG ------------------------------------------------------------------
export GNUPGHOME="$APTD/.gnupg"
mkdir -p "$GNUPGHOME" && chmod 700 "$GNUPGHOME"
echo "$APT_SIGNING_SUBKEY" | base64 -d | gpg --batch --import
FPR=$(gpg --batch --list-keys --with-colons | awk -F: '/^fpr:/ {print $10; exit}')
echo "$FPR:6:" | gpg --batch --import-ownertrust
KEYID=$(gpg --batch --list-keys --with-colons | awk -F: '/^sub:/ {print $5; exit}')

# --- conf/distributions ----------------------------------------------------
{
  for track in "${TRACK_LIST[@]}"; do
    for uv in "${UBUNTUS[@]}"; do
      suite="${track}-$(flat "$uv")"
      cat <<EOF
Origin: podman-ubuntu
Label: podman-ubuntu
Codename: $suite
Suite: $suite
Architectures: amd64 arm64
Components: main
Description: podman-ubuntu $track track for Ubuntu $uv
SignWith: $KEYID

EOF
    done
  done
} > "$APTD/conf/distributions"
printf 'verbose\n' > "$APTD/conf/options"

# --- include debs ----------------------------------------------------------
shopt -s nullglob
for dir in debs-*/; do
  # debs-<component>-<version>-<track>-u<ubuntu>-<arch>/
  base="${dir%/}"
  rest="${base#debs-}"
  arch="${rest##*-}"
  rest="${rest%-*}"
  ubuntu="${rest##*-u}"
  rest="${rest%-u*}"
  track="${rest##*-}"
  compver="${rest%-*}"
  component="${compver%-*}"
  version="${compver##*-}"
  suite="${track}-$(flat "$ubuntu")"
  for deb in "$dir"/*.deb; do
    reprepro -b "$APTD" includedeb "$suite" "$deb"
  done
  echo "included $component $version ($track, Ubuntu $ubuntu, $arch)"
done

# --- prune: keep newest 3 versions per package per suite --------------------
for track in "${TRACK_LIST[@]}"; do
  for uv in "${UBUNTUS[@]}"; do
    suite="${track}-$(flat "$uv")"
    for pkg in $(reprepro -b "$APTD" list "$suite" | awk '{print $2}' | sort -u); do
      mapfile -t vers < <(reprepro -b "$APTD" list "$suite" "$pkg" \
        | awk '{print $3}' | sort -V)
      if [ "${#vers[@]}" -gt 3 ]; then
        for old in "${vers[@]:0:${#vers[@]}-3}"; do
          reprepro -b "$APTD" remove "$suite" "$pkg=$old" || true
          echo "pruned $pkg $old from $suite"
        done
      fi
    done
  done
done

# --- sign + publish key ------------------------------------------------------
reprepro -b "$APTD" export
cp keys/apt-signing.asc "$APTD/keys/apt-signing.asc"

(cd "$APTD" && git add -A && \
  (git -c user.name="podman-ubuntu-bot" \
       -c user.email="podman-ubuntu-bot@users.noreply.github.com" \
       commit -m "apt-repo: publish $(date -u +%Y%m%d-%H%M)" || true) && \
  git push origin apt-repo)

# --- stage for Pages (exclude git metadata) ----------------------------------
rm -rf apt-repo-publish
mkdir -p apt-repo-publish
rsync -a --delete --exclude '.git' --exclude '.gnupg' "$APTD/" apt-repo-publish/

# --- record manifest on main --------------------------------------------------
python3 - <<'EOF'
import json, glob, os
manifest_path = "manifest.json"
with open(manifest_path) as f:
    manifest = json.load(f)
built = manifest.setdefault("built", {})
for d in glob.glob("debs-*/"):
    base = d.rstrip("/")
    rest = base[len("debs-"):]
    arch = rest.rsplit("-", 1)[1]
    rest = rest.rsplit("-", 1)[0]
    ubuntu = rest.rsplit("-u", 1)[1]
    rest = rest.rsplit("-u", 1)[0]
    track = rest.rsplit("-", 1)[1]
    compver = rest.rsplit("-", 1)[0]
    component = compver.rsplit("-", 1)[0]
    version = compver.rsplit("-", 1)[1]
    built.setdefault(component, {})[version] = True
with open(manifest_path, "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
EOF
git add manifest.json
(git -c user.name="podman-ubuntu-bot" \
     -c user.email="podman-ubuntu-bot@users.noreply.github.com" \
     commit -m "chore: record published builds in manifest.json" || true)
git push origin main
git worktree remove --force apt-repo

echo "Publish complete."
