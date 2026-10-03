#!/usr/bin/env python3
"""Exit 0 if (component, version) is already recorded as published *with the
same packaging inputs*.

The manifest maps (component, version) -> {"packaging_sha": ...}, where the
sha covers packaging/<component>/ and scripts/prepare-source.sh. A
packaging-only fix (debian/rules, debian/control, ...) must trigger a
rebuild even when the upstream version is unchanged; the old version-only
key silently shipped stale debs.
"""
import argparse
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def packaging_sha(component):
    parts = []
    for rev in (f"HEAD:packaging/{component}", "HEAD:scripts/prepare-source.sh"):
        parts.append(subprocess.run(
            ["git", "rev-parse", rev],
            capture_output=True, text=True, check=True,
            cwd=REPO_ROOT).stdout.strip())
    return parts[0][:12] + parts[1][:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--component", required=True)
    ap.add_argument("--version", required=True)
    args = ap.parse_args()

    with open(os.path.join(REPO_ROOT, "manifest.json")) as f:
        built = json.load(f).get("built", {})

    entry = built.get(args.component, {}).get(args.version)
    if isinstance(entry, dict) and entry.get("packaging_sha") == packaging_sha(args.component):
        print(f"{args.component} {args.version} already published with current packaging, skipping.")
        return 0
    # Old-style `true` entries (or missing entries) always rebuild once, then
    # get re-recorded with a sha by the publish job.
    print(f"{args.component} {args.version} needs building.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
