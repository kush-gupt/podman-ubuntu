#!/usr/bin/env python3
"""Watch upstream Podman releases and the Ubuntu support calendar.

Compares against versions.json / ubuntu-versions.json (the watermarks).
On change, updates the files and emits GitHub Actions outputs so the
workflow can open a single PR. Idempotent: no change, no output.
Stdlib only.
"""

import json
import os
import sys
import urllib.request
from datetime import date, datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_FILE = os.path.join(REPO_ROOT, "versions.json")
UBUNTU_FILE = os.path.join(REPO_ROOT, "ubuntu-versions.json")
PR_BODY = "/tmp/watch-pr-body.md"

PODMAN_API = "https://api.github.com/repos/containers/podman/releases?per_page=100"
UBUNTU_EOL_API = "https://endoflife.date/api/ubuntu.json"


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "podman-ubuntu-watch",
            **(
                {"Authorization": f"Bearer {os.environ['GH_TOKEN']}"}
                if url.startswith("https://api.github.com") and os.environ.get("GH_TOKEN")
                else {}
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def latest_per_track(releases):
    """Newest non-prerelease tag per major track (6.x stable, 5.x maintenance)."""
    best = {}
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel.get("tag_name", "")
        if not tag.startswith("v"):
            continue
        ver = tag[1:]
        major = ver.split(".")[0]
        if major not in ("5", "6"):
            continue
        track = "stable" if major == "6" else "v5"
        if track not in best or _ver_key(ver) > _ver_key(best[track]):
            best[track] = ver
    return best


def _ver_key(ver):
    parts = []
    for p in ver.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def supported_ubuntu():
    data = get_json(UBUNTU_EOL_API)
    today = date.today().isoformat()
    out = []
    for cycle in data:
        eol = cycle.get("eol")
        if eol is False or (isinstance(eol, str) and eol > today):
            out.append(cycle["cycle"])
    return sorted(out)


def main():
    with open(VERSIONS_FILE) as f:
        versions = json.load(f)
    with open(UBUNTU_FILE) as f:
        ubuntu = json.load(f)

    changes = []

    podman = latest_per_track(get_json(PODMAN_API))
    for track, ver in sorted(podman.items()):
        if versions.get(track) != ver:
            changes.append(f"podman {track}: {versions.get(track)} -> {ver}")
            versions[track] = ver

    supported = supported_ubuntu()
    if ubuntu.get("releases") != supported:
        changes.append(
            f"ubuntu set: {ubuntu.get('releases')} -> {supported}"
        )
        ubuntu["releases"] = supported

    out = os.environ.get("GITHUB_OUTPUT")
    if not changes:
        if out:
            with open(out, "a") as f:
                f.write("changed=false\n")
        print("No changes.")
        return

    with open(VERSIONS_FILE, "w") as f:
        json.dump(versions, f, indent=2)
        f.write("\n")
    with open(UBUNTU_FILE, "w") as f:
        json.dump(ubuntu, f, indent=2)
        f.write("\n")

    summary = "; ".join(changes)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    with open(PR_BODY, "w") as f:
        f.write(
            "Automated upstream watch found changes:\n\n"
            + "".join(f"- {c}\n" for c in changes)
            + "\n## Release checklist (podman bumps)\n\n"
            "- [ ] Fill the dependency stack pins for the new podman version "
            "in `packaging/components.json` from the upstream release notes "
            "(crun, conmon, netavark, aardvark-dns, passt, containers-common)\n"
            "- [ ] If the release notes require a newer Go/Rust toolchain, "
            "bump `GO_VERSION`/`RUST_VERSION` in `images/builder.Containerfile`\n"
            "- [ ] Merging this PR triggers the build matrix.\n"
        )

    if out:
        with open(out, "a") as f:
            f.write("changed=true\n")
            f.write(f"summary={summary}\n")
            f.write(f"stamp={stamp}\n")
    print("Changed:", summary)


if __name__ == "__main__":
    sys.exit(main())
