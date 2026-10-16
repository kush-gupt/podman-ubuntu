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


def make_deb_versioned(workdir, pkg, arch, version, marker):
    src = os.path.join(workdir, f"src-{pkg}-{arch}-{version}")
    os.makedirs(os.path.join(src, "DEBIAN"), exist_ok=True)
    os.makedirs(os.path.join(src, "usr", "share", pkg), exist_ok=True)
    with open(os.path.join(src, "DEBIAN", "control"), "w") as f:
        f.write(f"Package: {pkg}\nVersion: {version}\n"
                f"Architecture: {arch}\nMaintainer: Test <t@example>\n"
                f"Section: admin\nPriority: optional\n"
                f"Description: fake package for the reprepro sim\n")
    with open(os.path.join(src, "usr", "share", pkg, "marker"), "w") as f:
        f.write(marker + "\n")
    for dirpath, _dirs, _files in os.walk(src):
        os.chmod(dirpath, 0o755)
    deb = os.path.join(workdir, f"{pkg}_{version}_{arch}.deb")
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


@unittest.skipUnless(HAVE_TOOLS and shutil.which("git"),
                     "reprepro/dpkg-deb/git not installed")
class TestRepreproRollbackDrill(unittest.TestCase):
    """Rollback drill for the runbook procedure.

    docs/RUNBOOK.md prescribes rolling back a bad publish with
    `git revert` of the publish commit on the apt-repo branch. That
    only works if the reprepro basedir is fully versioned, db/
    included (verified against the real branch: conf, db, dists,
    keys, manifest.json, pool are all tracked). This test models the
    apt-repo branch as a git repo, publishes version A, publishes
    version B over it, reverts the B commit exactly as the runbook
    says, and asserts clients see A again: the exported index files
    carry A's checksums, reprepro's own database agrees (list shows
    A only), and a fresh export does not resurrect B."""

    VERSION_A = "1.0-1~noble"
    VERSION_B = "1.1-1~noble"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reprepro-drill-")
        self.base = os.path.join(self.tmp, "repo")
        conf = os.path.join(self.base, "conf")
        os.makedirs(conf)
        with open(os.path.join(conf, "distributions"), "w") as f:
            f.write(f"Origin: test\nLabel: test\nCodename: {SUITE}\n"
                    f"Suite: {SUITE}\nArchitectures: amd64 arm64\n"
                    f"Components: main\nDescription: drill repo\n")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example")
        self.git("config", "user.name", "Rollback Drill")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.base, *args],
                              check=True, capture_output=True, text=True)

    def reprepro(self, *args):
        return subprocess.run(["reprepro", "-b", self.base, *args],
                              check=True, capture_output=True, text=True)

    def publish(self, version, marker):
        # make_deb stamps the module-level VERSION, so this class
        # builds its debs with the version-parameterised variant.
        debs = []
        for pkg, arch in (("stackpkg", "amd64"), ("stackpkg", "arm64"),
                          ("commonpkg", "all")):
            debs.append(self._make_deb_versioned(pkg, arch, version,
                                                 marker))
        for deb in debs:
            publish_repo.replace_stale(
                self.base, SUITE,
                publish_repo.deb_field(deb, "Package"),
                publish_repo.deb_field(deb, "Version"),
                publish_repo.deb_field(deb, "Architecture"))
        self.reprepro("deleteunreferenced")
        for deb in debs:
            self.reprepro("includedeb", SUITE, deb)
        self.reprepro("export")
        return debs

    def _make_deb_versioned(self, pkg, arch, version, marker):
        src = os.path.join(self.tmp, f"src-{pkg}-{arch}-{version}")
        os.makedirs(os.path.join(src, "DEBIAN"), exist_ok=True)
        os.makedirs(os.path.join(src, "usr", "share", pkg), exist_ok=True)
        with open(os.path.join(src, "DEBIAN", "control"), "w") as f:
            f.write(f"Package: {pkg}\nVersion: {version}\n"
                    f"Architecture: {arch}\nMaintainer: Test <t@example>\n"
                    f"Section: admin\nPriority: optional\n"
                    f"Description: fake package for the rollback drill\n")
        with open(os.path.join(src, "usr", "share", pkg, "marker"),
                  "w") as f:
            f.write(marker + "\n")
        for dirpath, _dirs, _files in os.walk(src):
            os.chmod(dirpath, 0o755)
        deb = os.path.join(self.tmp, f"{pkg}_{version}_{arch}.deb")
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group",
                        src, deb], check=True, capture_output=True)
        return deb

    def commit_all(self, msg):
        self.git("add", "-A")
        self.git("commit", "-qm", msg)

    def index_text(self, arch="amd64"):
        path = os.path.join(self.base, "dists", SUITE, "main",
                            f"binary-{arch}", "Packages")
        with open(path) as f:
            return f.read()

    def test_git_revert_rolls_back_a_bad_publish(self):
        import hashlib

        debs_a = self.publish(self.VERSION_A, "version-a")
        self.commit_all("apt-repo: publish A")
        # The drill's premise: reprepro's db is versioned too.
        tracked = self.git("ls-files").stdout
        self.assertIn("db/", tracked)
        digest_a = hashlib.sha256(
            open(debs_a[0], "rb").read()).hexdigest()
        self.assertIn(digest_a, self.index_text())

        self.publish(self.VERSION_B, "version-b")
        self.commit_all("apt-repo: publish B")
        self.assertIn(self.VERSION_B, self.index_text())
        self.assertNotIn(digest_a, self.index_text())

        # The runbook rollback, verbatim: revert the publish commit.
        self.git("revert", "--no-edit", "HEAD")

        # Clients see A again (index files as committed, no re-export).
        self.assertIn(digest_a, self.index_text())
        self.assertNotIn(self.VERSION_B, self.index_text())
        # The database reverted with the tree: list shows A only.
        listed = publish_repo.parse_listed(
            self.reprepro("list", SUITE, "stackpkg").stdout)
        self.assertEqual(listed, {(self.VERSION_A, "amd64"),
                                   (self.VERSION_A, "arm64")})
        # And the next publish's export cannot resurrect B.
        self.reprepro("export")
        self.assertIn(digest_a, self.index_text())
        self.assertNotIn(self.VERSION_B, self.index_text())


@unittest.skipUnless(HAVE_TOOLS, "reprepro/dpkg-deb not installed")
class TestRepreproPoolCompleteness(unittest.TestCase):
    """The PR #20 scenario, encoded: a component pin moves in ONE
    track to a version already built (and pooled) under another
    track. The build matrix creates no new cells for it (manifest
    skip is keyed without track), so publish must complete the
    target suite from the pool or the bump silently never ships,
    which is exactly what happened to the v5 suites."""

    STABLE = "stable-2404"
    V5 = "v5-2404"
    OLD = "1.0-1~noble"
    NEW = "2.0-1~noble"
    COMMON_OLD = "2.0-1~noble"
    COMMON_NEW = "3.0-1~noble"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reprepro-pool-")
        self.base = os.path.join(self.tmp, "repo")
        conf = os.path.join(self.base, "conf")
        os.makedirs(conf)
        with open(os.path.join(conf, "distributions"), "w") as f:
            for suite in (self.STABLE, self.V5):
                f.write(f"Origin: test\nLabel: test\nCodename: {suite}\n"
                        f"Suite: {suite}\nArchitectures: amd64 arm64\n"
                        f"Components: main\nDescription: sim repo\n\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reprepro(self, *args):
        return subprocess.run(["reprepro", "-b", self.base, *args],
                              check=True, capture_output=True, text=True)

    def include(self, suite, deb):
        self.reprepro("includedeb", suite, deb)

    def listed(self, suite, pkg):
        out = self.reprepro("list", suite, pkg).stdout
        return publish_repo.parse_listed(out)

    def test_pool_completeness_fills_other_track_suite(self):
        # Publish round 1 (stable only): stackpkg NEW + commonpkg
        # COMMON_NEW land in the stable suite and therefore the pool.
        for arch in ("amd64", "arm64"):
            self.include(self.STABLE, make_deb_versioned(
                self.tmp, "stackpkg", arch, self.NEW, "new"))
        self.include(self.STABLE, make_deb_versioned(
            self.tmp, "commonpkg", "all", self.COMMON_NEW, "new-common"))
        # The v5 suite's current state: the OLD pins.
        for arch in ("amd64", "arm64"):
            self.include(self.V5, make_deb_versioned(
                self.tmp, "stackpkg", arch, self.OLD, "old"))
        self.include(self.V5, make_deb_versioned(
            self.tmp, "commonpkg", "all", self.COMMON_OLD, "old-common"))

        # Publish round 2 for v5 with NO new artifacts: completeness
        # must source both pins from the pool.
        missing = publish_repo.ensure_stack(
            self.base, self.V5,
            {"stackpkg": "2.0", "commonpkg": "3.0"})
        self.assertEqual(missing, [])
        # includedeb replaces the suite's previous version, so the
        # completed suite lists exactly the new pins (matching the
        # live repo, where suites carry one version per package).
        self.assertEqual(self.listed(self.V5, "stackpkg"),
                         {(self.NEW, "amd64"), (self.NEW, "arm64")})
        self.assertEqual(self.listed(self.V5, "commonpkg"),
                         {(self.COMMON_NEW, "amd64"),
                          (self.COMMON_NEW, "arm64")})
        # The stable suite is untouched by the v5 completion.
        self.assertEqual(self.listed(self.STABLE, "stackpkg"),
                         {(self.NEW, "amd64"), (self.NEW, "arm64")})
        # A pin that exists nowhere must be reported, not shipped
        # around: the caller turns this into a failed publish.
        self.assertEqual(
            publish_repo.ensure_stack(self.base, self.V5,
                                      {"ghostpkg": "9.9"}),
            [(self.V5, "ghostpkg", "9.9")])


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

    PINS = {"conmon": "2.2.1", "crun": "1.30.1",
            "containers-common": "0.69.2", "netavark": "2.1.0",
            "aardvark-dns": "2.1.0", "passt": "20260925",
            "podman": "6.1.4", "podman-suite": "6.1.4"}

    def test_matching_pins_pass(self):
        self.assertEqual(
            self.clr.check_index("stable", "24.04", "amd64",
                                 self.GOOD, "6.1.4", self.PINS), [])

    def test_stale_component_pins_fail(self):
        # The PR #20 shape: podman and the package set are right, but
        # the suite still serves the pre-bump component versions.
        stale = (self.GOOD
                 .replace("2.2.1-1~noble", "2.1.13-1~noble")
                 .replace("1.30.1-1~noble", "1.21-1~noble")
                 .replace("0.69.2-1~noble", "0.67.2-1~noble"))
        problems = self.clr.check_index("v5", "22.04", "arm64",
                                        stale, "6.1.4", self.PINS)
        for pkg in ("conmon", "crun", "containers-common"):
            self.assertTrue(
                any(pkg in p and "pinned" in p for p in problems),
                f"no pin problem reported for {pkg}: {problems}")

    def test_pin_matches_any_listed_version_and_any_suffix(self):
        # Older versions may linger next to the pin (prune keeps 3),
        # and the suffix follows the Ubuntu release (~jammy here).
        text = self.GOOD.replace("-1~noble", "-1~jammy")
        text = text.replace(
            "Package: crun\nVersion: 1.30.1-1~jammy",
            "Package: crun\nVersion: 1.21-1~jammy")
        text += "Package: crun\nVersion: 1.30.1-1~jammy\n\n"
        self.assertEqual(
            self.clr.check_index("stable", "22.04", "amd64",
                                 text, "6.1.4", self.PINS), [])

    def test_upstream_of(self):
        self.assertEqual(self.clr.upstream_of("2.2.1-1~noble"), "2.2.1")
        self.assertEqual(self.clr.upstream_of("20261014-1~jammy"),
                         "20261014")
        self.assertEqual(self.clr.upstream_of("1.30.1"), "1.30.1")

    def test_stack_package_pins_shape(self):
        versions = {"v5": "5.8.9", "stable": "6.1.4"}
        stacks = {"5.8.9": {"conmon": "2.2.1", "crun": "1.30.1",
                            "containers-common": "0.69.2",
                            "netavark": "1.16.0", "aardvark-dns": "1.16.0",
                            "passt": "20261014",
                            "passt_sha": "8ba41320",
                            "go": "1.26.1", "rust": "1.85.0",
                            "podman": "5.8.9"}}
        pins = self.clr.stack_package_pins("v5", versions, stacks)
        self.assertEqual(pins["podman"], "5.8.9")
        self.assertEqual(pins["podman-suite"], "5.8.9")
        self.assertEqual(pins["passt"], "20261014")
        for tool in ("go", "rust", "passt_sha"):
            self.assertNotIn(tool, pins)
        self.assertIsNone(
            self.clr.stack_package_pins("nightly", versions, stacks))


if __name__ == "__main__":
    unittest.main()
