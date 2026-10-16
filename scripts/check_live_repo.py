#!/usr/bin/env python3
"""Check the LIVE published apt repository's Packages indices.

For every (track, Ubuntu, arch) cell, fetch the suite's binary index
from GitHub Pages and assert the package set is complete:

  stable / v5 : exactly the 8 stack packages (aardvark-dns, conmon,
                containers-common, crun, netavark, passt, podman,
                podman-suite), podman / podman-suite at the
                versions.json version for the track, and every other
                component at the upstream version pinned for that
                track in packaging/components.json.
  nightly     : podman only. Nightly publishes a podman snapshot build
                against the stable stack; there is no nightly
                podman-suite, by design.

This is the check that would have caught the Oct 2026 amd64 incident
the same day: publish had shipped indices holding only the two
arch-all packages, and nothing read the indices back. The component
pin assertions exist because of the PR #20 incident the same month:
the v5 pins moved in components.json and publish stayed green, but
the new component versions never reached the v5 suites, and a check
that only asserted podman version and package presence could not see
it. Run daily by health-check.yml, per-cell by orchestrate's
verify-published job, and on demand. Optional positional arguments
scope the run: [track] [ubuntu] [arch]. Stdlib only.

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


def upstream_of(debian_version):
    """Upstream part of a Debian version: 2.2.1-1~noble -> 2.2.1."""
    if "-" in debian_version:
        return debian_version.rsplit("-", 1)[0]
    return debian_version


def stack_package_pins(track, versions, all_pins):
    """{package: upstream pin} for a track's stack, or None.

    podman-suite's version tracks the podman version (its meta.json
    says version_from: podman), so its pin is the track's podman
    version. go / rust / passt_sha stack entries are toolchain or
    source selectors, not packages, and are not asserted."""
    pver = versions.get(track)
    stack = all_pins.get(pver) if pver else None
    if not stack:
        return None
    pins = {c: stack[c] for c in
            ("conmon", "crun", "netavark", "aardvark-dns", "passt",
             "containers-common") if c in stack}
    pins["podman"] = stack.get("podman", pver)
    pins["podman-suite"] = pver
    return pins


def check_index(track, ubuntu, arch, text, expected_podman, pins=None):
    """Problems for one index (empty list = complete).

    pins, when given, is {package: upstream pin} for the track (see
    stack_package_pins); every pinned package must be served at its
    pinned upstream version, with any Debian revision / distro
    suffix. (Suites carry one version per package in practice; the
    assertion accepts the pin appearing among the listed versions
    either way.)"""
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
    for name, pin in sorted((pins or {}).items()):
        if name in ("podman", "podman-suite"):
            continue  # asserted just above against versions.json
        if not any(upstream_of(v) == pin for v in pkgs.get(name, [])):
            problems.append(
                f"{suite}/{arch}: {name} does not serve pinned {pin} "
                f"(index has {pkgs.get(name, [])})")
    return problems


def fetch(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    only_track = argv[0] if len(argv) > 0 else None
    only_ubuntu = argv[1] if len(argv) > 1 else None
    only_arch = argv[2] if len(argv) > 2 else None
    versions = lib.versions()
    all_pins = lib.stack_pins()
    tracks = sorted(versions) + ["nightly"]
    problems = []
    cells = 0
    for track in tracks:
        if only_track and track != only_track:
            continue
        pins = stack_package_pins(track, versions, all_pins)
        for ubuntu in lib.ubuntu_releases():
            if only_ubuntu and ubuntu != only_ubuntu:
                continue
            for arch in lib.ARCHES:
                if only_arch and arch != only_arch:
                    continue
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
                                        versions.get(track, ""), pins)
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
