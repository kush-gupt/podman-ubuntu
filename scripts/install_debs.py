#!/usr/bin/env python3
"""Install one (track, ubuntu) cell's built debs for the smoke job.

Selects artifacts via their meta.json sidecars (written by build.yml) rather
than parsing directory names, then installs with apt so dependencies resolve
in one step. Fails loudly when nothing matches: a silent empty install would
smoke-test nothing. Stdlib only.Usage: install_debs.py <track> <ubuntu_version> [arch]
"""
import glob
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib


def deb_field(deb, field):
    return subprocess.run(["dpkg-deb", "-f", deb, field],
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def select_debs(track, uver, arch, debs_dir="debs"):
    """Locally built .debs for one (track, ubuntu, arch) smoke cell.

    Artifacts built for this arch match directly. An artifact built for
    another arch also matches when every .deb in it is
    Architecture: all: build_matrix.py builds arch-independent
    components once, on amd64, but the resulting .deb installs on every
    arch. Without that second rule a change touching only an arch-all
    component (e.g. containers-common) leaves the other arches' smoke
    cells with no artifacts at all, and the run dies before publish.
    """
    debs = []
    for d, m in lib.iter_artifacts(debs_dir):
        if (m["track"], m["ubuntu"]) != (track, uver):
            continue
        found = sorted(glob.glob(os.path.join(d, "*.deb")))
        if m["arch"] == arch:
            debs += found
        elif found and all(deb_field(f, "Architecture") == "all"
                            for f in found):
            debs += found
    return debs


def main():
    track, uver = sys.argv[1], sys.argv[2]
    if len(sys.argv) > 3:
        arch = sys.argv[3]
    else:
        arch = subprocess.run(["dpkg", "--print-architecture"],
                              capture_output=True, text=True,
                              check=True).stdout.strip()
    debs = select_debs(track, uver, arch)
    if not debs:
        sys.exit(f"ERROR: no artifacts for track={track} ubuntu={uver} "
                 f"arch={arch}; refusing to smoke-test an empty install")

    # All apt calls go through lib.run_with_retries: PR #38's smoke
    # cells all died at a single-shot apt call when *.ubuntu.com DNS
    # failed on the runners for a few minutes (2026-11-11). The
    # update call also treats a "Failed to fetch" report as failure:
    # apt-get update exits 0 after a partial fetch, and the next
    # install then fails with a misleading "Unable to locate package".
    apt_attempts = int(os.environ.get("APT_INSTALL_ATTEMPTS", "5"))
    apt_delay = int(os.environ.get("APT_INSTALL_DELAY", "20"))

    def apt(*args, markers=()):
        return lib.run_with_retries(
            ["apt-get", *args], attempts=apt_attempts, delay=apt_delay,
            fail_output_markers=markers)

    apt("update", "-qq",
        markers=("Failed to fetch", "Could not resolve",
                 "Temporary failure resolving"))
    # CA certs: the minimal ubuntu container image can't verify registry TLS
    # without them, which breaks image pulls.
    apt("install", "-y", "-qq", "ca-certificates")

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
    apt("install", "-y", "-qq", "--allow-downgrades",
        *[f"./{d}" for d in sorted(debs)])
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
        apt("install", "-y", "-qq", *missing)
        verify_local_versions("after gap fill")


if __name__ == "__main__":
    main()
