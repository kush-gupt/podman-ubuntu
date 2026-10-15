#!/usr/bin/env python3
"""Integration test: publish_repo's stale-replacement against a REAL
reprepro repository.

The two publish bugs of Oct 2026 were both proven in manual local
reprepro sims and then fixed, but the sims were never encoded:

  1. Arch-blind replacement: suite_has matched (package, version)
     under any architecture and removefilter had no Architecture
     term, so the arm64 pass deleted the just-included amd64 entry.
     Every amd64 index shipped with only the arch-all packages.
  2. Arch-all replacement: `reprepro list` prints an arch-all
     package once per suite binary arch (never "all"), so filtering
     removefilter on a recorded token matched nothing for arch-all
     packages, removefilter still exited 0, and includedeb then
     collided with the stale pool file on checksums.

This test builds tiny fake debs (stackpkg for amd64 + arm64,
commonpkg for arch all), publishes them, re-publishes the SAME
versions with different bytes through publish_repo.replace_stale
(the exact function publish uses), and asserts all three entries
survive with the NEW bytes' checksums in the exported index.

Requires reprepro and dpkg-deb; skipped (loudly) when absent. The
script-tests workflow installs reprepro so CI always runs it.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import publish_repo  # noqa: E402

SUITE = "stable-2404"
VERSION = "1.0-1~noble"

HAVE_TOOLS = all(shutil.which(t) for t in ("reprepro", "dpkg-deb"))


def make_deb(workdir, pkg, arch, marker):
    src = os.path.join(workdir, f"src-{pkg}-{arch}")
    os.makedirs(os.path.join(src, "DEBIAN"), exist_ok=True)
    os.makedirs(os.path.join(src, "usr", "share", pkg), exist_ok=True)
    with open(os.path.join(src, "DEBIAN", "control"), "w") as f:
        f.write(f"Package: {pkg}\nVersion: {VERSION}\n"
                f"Architecture: {arch}\nMaintainer: Test <t@example>\n"
                f"Section: admin\nPriority: optional\n"
                f"Description: fake package for the reprepro sim\n")
    with open(os.path.join(src, "usr", "share", pkg, "marker"), "w") as f:
        f.write(marker + "\n")
    for dirpath, _dirs, _files in os.walk(src):
        os.chmod(dirpath, 0o755)
    deb = os.path.join(workdir, f"{pkg}_{VERSION}_{arch}.deb")
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", src, deb],
                   check=True, capture_output=True)
    return deb


@unittest.skipUnless(HAVE_TOOLS, "reprepro/dpkg-deb not installed")
class TestRepreproStaleReplacement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reprepro-sim-")
        self.base = os.path.join(self.tmp, "repo")
        conf = os.path.join(self.base, "conf")
        os.makedirs(conf)
        with open(os.path.join(conf, "distributions"), "w") as f:
            f.write(f"Origin: test\nLabel: test\nCodename: {SUITE}\n"
                    f"Suite: {SUITE}\nArchitectures: amd64 arm64\n"
                    f"Components: main\n"
                    f"Description: sim repo\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reprepro(self, *args):
        return subprocess.run(["reprepro", "-b", self.base, *args],
                              check=True, capture_output=True, text=True)

    def include(self, deb):
        self.reprepro("includedeb", SUITE, deb)

    def listed(self, pkg):
        out = self.reprepro("list", SUITE, pkg).stdout
        return publish_repo.parse_listed(out)

    def publish_round(self, marker):
        """One full publish pass in publish_repo's group order:
        replace stale, then include. Groups are per (arch), with
        the arch-all package in its own group, same as publish."""
        debs = [make_deb(self.tmp, "stackpkg", "amd64", marker),
                make_deb(self.tmp, "stackpkg", "arm64", marker),
                make_deb(self.tmp, "commonpkg", "all", marker)]
        for deb in debs:
            pkg = publish_repo.deb_field(deb, "Package")
            dver = publish_repo.deb_field(deb, "Version")
            darch = publish_repo.deb_field(deb, "Architecture")
            publish_repo.replace_stale(self.base, SUITE, pkg, dver, darch)
        self.reprepro("deleteunreferenced")
        for deb in debs:
            self.include(deb)
        return debs

    def test_republish_same_version_keeps_both_arches_and_arch_all(self):
        self.publish_round("round-one")
        self.assertEqual(self.listed("stackpkg"),
                         {(VERSION, "amd64"), (VERSION, "arm64")})
        # Arch-all lists once per suite binary arch, never "all".
        self.assertEqual(self.listed("commonpkg"),
                         {(VERSION, "amd64"), (VERSION, "arm64")})

        round2 = self.publish_round("round-two-different-bytes")
        self.assertEqual(self.listed("stackpkg"),
                         {(VERSION, "amd64"), (VERSION, "arm64")})
        self.assertEqual(self.listed("commonpkg"),
                         {(VERSION, "amd64"), (VERSION, "arm64")})

        # The exported index must carry round two's checksums, i.e.
        # the replacement actually landed, not just the listing.
        self.reprepro("export")
        import hashlib
        for deb in round2:
            darch = publish_repo.deb_field(deb, "Architecture")
            idx_arch = "amd64" if darch == "all" else darch
            idx = os.path.join(self.base, "dists", SUITE, "main",
                               f"binary-{idx_arch}", "Packages")
            with open(idx) as f:
                text = f.read()
            digest = hashlib.sha256(open(deb, "rb").read()).hexdigest()
            self.assertIn(digest, text,
                          f"{deb} (round two) not in exported index")


class TestCheckLiveRepo(unittest.TestCase):
    import check_live_repo as clr

    GOOD = "".join(
        f"Package: {p}\nVersion: {v}\nArchitecture: {a}\n\n"
        for p, v, a in [
            ("aardvark-dns", "2.1.0-1~noble", "amd64"),
            ("conmon", "2.2.1-1~noble", "amd64"),
            ("containers-common", "0.69.2-1~noble", "all"),
            ("crun", "1.30.1-1~noble", "amd64"),
            ("netavark", "2.1.0-1~noble", "amd64"),
            ("passt", "20260925-1~noble", "amd64"),
            ("podman", "6.1.4-1~noble", "amd64"),
            ("podman-suite", "6.1.4-1~noble", "all"),
        ])

    def test_complete_stable_index_passes(self):
        self.assertEqual(
            self.clr.check_index("stable", "24.04", "amd64",
                                 self.GOOD, "6.1.4"), [])

    def test_gutted_amd64_index_fails(self):
        # The Oct 2026 incident shape: only the arch-all packages left.
        gutted = "".join(
            f"Package: {p}\nVersion: {v}\nArchitecture: all\n\n"
            for p, v in [("containers-common", "0.69.2-1~noble"),
                         ("podman-suite", "6.1.4-1~noble")])
        problems = self.clr.check_index("stable", "24.04", "amd64",
                                        gutted, "6.1.4")
        self.assertTrue(any("missing" in p for p in problems))

    def test_wrong_podman_version_fails(self):
        old = self.GOOD.replace("6.1.4-1~noble", "6.1.3-1~noble")
        problems = self.clr.check_index("stable", "24.04", "amd64",
                                        old, "6.1.4")
        self.assertTrue(any("versions.json" in p for p in problems))

    def test_nightly_needs_only_podman(self):
        text = "Package: podman\nVersion: 7.0.0~git20261012.abc-1~noble\n\n"
        self.assertEqual(
            self.clr.check_index("nightly", "24.04", "arm64", text, ""), [])
        self.assertTrue(
            self.clr.check_index("nightly", "24.04", "arm64", "", ""))


if __name__ == "__main__":
    unittest.main()
