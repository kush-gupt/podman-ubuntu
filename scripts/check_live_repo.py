#!/usr/bin/env python3
"""Check the LIVE published apt repository's Packages indices.

For every (track, Ubuntu, arch) cell, fetch the suite's binary index
from GitHub Pages and assert the package set is complete:

  stable / v5 : exactly the 8 stack packages (aardvark-dns, conmon,
                containers-common, crun, netavark, passt, podman,
                podman-suite), and podman / podman-suite at the
                versions.json version for the track.
  nightly     : podman only. Nightly publishes a podman snapshot build
                against the stable stack; there is no nightly
                podman-suite, by design.

This is the check that would have caught the Oct 2026 amd64 incident
the same day: publish had shipped indices holding only the two
arch-all packages, and nothing read the indices back. Run daily by
health-check.yml and on demand. Stdlib only.

Exit 0 = every cell complete; 1 = at least one problem (all problems
are printed before exiting).
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

BASE = os.environ.get(
    "LIVE_REPO_BASE", "https://kush-gupt.github.io/podman-ubuntu")
STACK_PACKAGES = {
    "aardvark-dns", "conmon", "containers-common", "crun", "netavark",
    "passt", "podman", "podman-suite",
}


def parse_packages(text):
    """Packages index text -> {package: [versions]}."""
    out = {}
    pkg = None
    for line in text.splitlines():
        if line.startswith("Package: "):
            pkg = line.split(":", 1)[1].strip()
        elif line.startswith("Version: ") and pkg:
            out.setdefault(pkg, []).append(line.split(":", 1)[1].strip())
            pkg = None
    return out


def check_index(track, ubuntu, arch, text, expected_podman):
    """Problems for one index (empty list = complete)."""
    flat = ubuntu.replace(".", "")
    suite = f"{track}-{flat}"
    pkgs = parse_packages(text)
    problems = []
    if track == "nightly":
        if "podman" not in pkgs:
            problems.append(f"{suite}/{arch}: nightly podman missing "
                            f"(index has {sorted(pkgs)})")
        return problems
    missing = STACK_PACKAGES - set(pkgs)
    extra = set(pkgs) - STACK_PACKAGES
    if missing:
        problems.append(f"{suite}/{arch}: missing {sorted(missing)}")
    if extra:
        problems.append(f"{suite}/{arch}: unexpected {sorted(extra)}")
    for name in ("podman", "podman-suite"):
        # Older versions may legitimately linger (prune keeps the
        # newest 3); the current versions.json version must be served.
        if not any(v.startswith(expected_podman + "-1~")
                   for v in pkgs.get(name, [])):
            problems.append(
                f"{suite}/{arch}: {name} does not serve versions.json "
                f"{expected_podman} (index has {pkgs.get(name, [])})")
    return problems


def fetch(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8")


def main():
    versions = lib.versions()
    tracks = sorted(versions) + ["nightly"]
    problems = []
    cells = 0
    for track in tracks:
        for ubuntu in lib.ubuntu_releases():
            for arch in lib.ARCHES:
                flat = ubuntu.replace(".", "")
                url = (f"{BASE}/dists/{track}-{flat}/main/"
                       f"binary-{arch}/Packages")
                cells += 1
                try:
                    text = fetch(url)
                except Exception as e:
                    problems.append(f"{track}-{flat}/{arch}: "
                                    f"fetch failed: {e}")
                    continue
                problems += check_index(track, ubuntu, arch, text,
                                        versions.get(track, ""))
    for p in problems:
        print(f"PROBLEM: {p}")
    if problems:
        print(f"live repo check FAILED: {len(problems)} problem(s) "
              f"across {cells} indices")
        return 1
    print(f"live repo check OK: all {cells} indices complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
