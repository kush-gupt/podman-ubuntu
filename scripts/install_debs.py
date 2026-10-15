#!/usr/bin/env python3
"""Install one (track, ubuntu) cell's built debs for the smoke job.

Selects artifacts via their meta.json sidecars (written by build.yml) rather
than parsing directory names, then installs with apt so dependencies resolve
in one step. Fails loudly when nothing matches: a silent empty install would
smoke-test nothing. Stdlib only.

Usage: install_debs.py <track> <ubuntu_version> [arch]
"""
import glob
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib


def main():
    track, uver = sys.argv[1], sys.argv[2]
    if len(sys.argv) > 3:
        arch = sys.argv[3]
    else:
        arch = subprocess.run(["dpkg", "--print-architecture"],
                              capture_output=True, text=True,
                              check=True).stdout.strip()
    debs = []
    for d, m in lib.iter_artifacts("debs"):
        if (m["track"], m["ubuntu"], m["arch"]) == (track, uver, arch):
            debs += glob.glob(os.path.join(d, "*.deb"))
    if not debs:
        sys.exit(f"ERROR: no artifacts for track={track} ubuntu={uver} "
                 f"arch={arch}; refusing to smoke-test an empty install")

    subprocess.run(["apt-get", "update", "-qq"], check=True)
    # CA certs: the minimal ubuntu container image can't verify registry TLS
    # without them, which breaks image pulls.
    subprocess.run(["apt-get", "install", "-y", "-qq", "ca-certificates"],
                   check=True)

    def deb_field(deb, field):
        return subprocess.run(["dpkg-deb", "-f", deb, field],
                              capture_output=True, text=True,
                              check=True).stdout.strip()

    def installed_version(pkg):
        r = subprocess.run(["dpkg-query", "-W", "-f=${Version}", pkg],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def verify_local_versions(stage):
        # The smoke test must test the builds it was given. Fail loudly
        # if anything (a repo candidate, an Ubuntu revision of the same
        # upstream version) replaced a local package.
        for d in debs:
            pkg, ver = deb_field(d, "Package"), deb_field(d, "Version")
            got = installed_version(pkg)
            if got != ver:
                sys.exit(f"ERROR: {stage}: {pkg} installed version is "
                         f"{got}, expected the local build {ver}")

    # apt (not dpkg -i + install -f): resolves dependencies in one step.
    # --allow-downgrades: the smoke job's upgrade path installs the
    # published stack first (from the pin-1001 repo), then installs
    # these local files over it. Same version, but a local file's
    # implicit pin (100) is below the installed origin's, so apt
    # calls the reinstall a downgrade and refuses without the flag.
    # verify_local_versions below remains the guard: every local
    # package must end up at exactly the local build's version.
    subprocess.run(["apt-get", "install", "-y", "-qq",
                    "--allow-downgrades"]
                   + [f"./{d}" for d in sorted(debs)], check=True)
    verify_local_versions("after local install")
    # Fill gaps only: incremental builds only produce artifacts for
    # changed components, so a stack package with no local artifact
    # resolves from the published repo (configured by the workflow).
    # Naming only the missing packages matters: naming an installed one
    # invites apt to "upgrade" it to a repo or Ubuntu candidate, which
    # silently swaps the build under test (Ubuntu's aardvark-dns
    # 1.16.0-3 on 26.04 did exactly that to our 1.16.0-1~resolute), and
    # holding the installed ones instead makes apt abort the whole
    # transaction when its resolution wants to change one.
    stack = ["podman", "passt", "conmon", "crun", "aardvark-dns"]
    missing = [p for p in stack if installed_version(p) is None]
    if missing:
        subprocess.run(["apt-get", "install", "-y", "-qq"] + missing,
                       check=True)
        verify_local_versions("after gap fill")


if __name__ == "__main__":
    main()
