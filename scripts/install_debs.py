#!/usr/bin/env python3
"""Install one (track, ubuntu) cell's built debs for the smoke job.

Selects artifacts via their meta.json sidecars (written by build.yml) rather
than parsing directory names, then installs with apt so dependencies resolve
in one step. Fails loudly when nothing matches: a silent empty install would
smoke-test nothing. Stdlib only.

Usage: install_debs.py <track> <ubuntu_version>
"""
import glob
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib


def main():
    track, uver = sys.argv[1], sys.argv[2]
    debs = []
    for d, m in lib.iter_artifacts("debs"):
        if (m["track"], m["ubuntu"], m["arch"]) == (track, uver, "amd64"):
            debs += glob.glob(os.path.join(d, "*.deb"))
    if not debs:
        sys.exit(f"ERROR: no artifacts for track={track} ubuntu={uver}; "
                 f"refusing to smoke-test an empty install")

    subprocess.run(["apt-get", "update", "-qq"], check=True)
    # CA certs: the minimal ubuntu container image can't verify registry TLS
    # without them, which breaks image pulls.
    subprocess.run(["apt-get", "install", "-y", "-qq", "ca-certificates"],
                   check=True)
    # apt (not dpkg -i + install -f): resolves dependencies in one step.
    # Local debs first (newly built), then ensure the full stack is present:
    # incremental builds only produce artifacts for changed components, so
    # unchanged packages resolve from the published repo (configured by the
    # workflow) rather than the artifacts.
    subprocess.run(["apt-get", "install", "-y", "-qq"]
                   + [f"./{d}" for d in sorted(debs)], check=True)
    subprocess.run(["apt-get", "install", "-y", "-qq",
                    "podman", "passt", "conmon", "crun"],
                   check=True)


if __name__ == "__main__":
    main()
