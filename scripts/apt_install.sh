#!/usr/bin/env bash
# apt-get update + install with retries, for CI steps that bootstrap
# packages from the Ubuntu archive.
#
# Usage: apt_install.sh <package> [<package>...]
#   Extra apt-get install flags may be passed the same way; everything
#   after the script name goes to `apt-get install -y`.
#
# Why this exists: PR #38 (orchestrate run 43038773268, script-tests
# run 43038772967, 2026-11-11 ~14:28 UTC). DNS resolution for
# *.ubuntu.com failed on GitHub-hosted runners for a few minutes --
# host jobs and job containers, amd64 and arm64, at least five Azure
# regions -- while github.com and the artifact service resolved fine
# in the same jobs seconds earlier. Every apt step in the repo was a
# single-shot `apt-get update -qq && apt-get install`, so one brief
# resolver outage failed all 16 smoke cells, the dep8 and reprepro
# jobs, and (via pr-gate) the whole PR. The same commit passed
# unchanged on re-run a few hours later, and the identical steps had
# passed the night before: nothing in the PR caused it, but nothing
# in the repo absorbed it either.
#
# Two apt behaviours make a naive retry loop insufficient, so both
# are handled here:
#   - `apt-get update` exits 0 when some indexes fail to fetch ("Some
#     index files failed to download. They have been ignored, or old
#     ones used instead."), so the failure only surfaces one step
#     later as the misleading "Unable to locate package curl" /
#     "has no installation candidate" from install. An update whose
#     output reports a failed fetch therefore counts as a failed
#     attempt here, not a success.
#   - `apt-get install` exits 100 when a .deb download fails (the
#     reprepro job's failure mode), which a plain exit-code retry
#     does cover.
#
# Attempts and delay are bounded (defaults: 5 attempts, 20s apart,
# so at most ~80s of waiting plus apt's own timeouts) and can be
# overridden for tests via APT_INSTALL_ATTEMPTS / APT_INSTALL_DELAY.
set -uo pipefail

ATTEMPTS="${APT_INSTALL_ATTEMPTS:-5}"
DELAY="${APT_INSTALL_DELAY:-20}"

if [ "$#" -eq 0 ]; then
  echo "usage: apt_install.sh <package> [<package>...]" >&2
  exit 2
fi

attempt=1
while [ "$attempt" -le "$ATTEMPTS" ]; do
  log="$(mktemp)"
  if apt-get update -qq >"$log" 2>&1; then
    update_rc=0
  else
    update_rc=$?
  fi
  cat "$log"
  if [ "$update_rc" -ne 0 ] \
      || grep -Eq "Failed to fetch|Could not resolve|Temporary failure resolving|Network is unreachable|Connection (failed|timed out)" "$log"; then
    echo "apt_install: apt-get update attempt $attempt/$ATTEMPTS did not fetch every index (rc=$update_rc)" >&2
  elif apt-get install -y -qq "$@"; then
    rm -f "$log"
    exit 0
  else
    echo "apt_install: apt-get install attempt $attempt/$ATTEMPTS failed (rc=$?)" >&2
  fi
  rm -f "$log"
  if [ "$attempt" -lt "$ATTEMPTS" ]; then
    sleep "$DELAY"
  fi
  attempt=$((attempt + 1))
done

echo "ERROR: apt-get update + install of '$*' failed after $ATTEMPTS attempts" >&2
exit 1
