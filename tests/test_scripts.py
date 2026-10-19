#!/usr/bin/env python3
"""Unit tests for the repo's Python tooling and repo-data invariants.

Stdlib only (unittest), run in CI by script-tests.yml and locally with:
    python3 -m unittest discover -s tests -v

The invariant tests encode the failure that broke PR #13: versions.json
pointed at podman versions with no dependency stack in
packaging/components.json, and nothing caught it until the full build
matrix's setup job failed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_key_expiry  # noqa: E402
import check_releases  # noqa: E402
import check_watch_pr  # noqa: E402
import create_releases  # noqa: E402
import create_watch_prs  # noqa: E402
import lib  # noqa: E402
import publish_repo  # noqa: E402
import rebase_watch_prs  # noqa: E402
import verify_upstream_pins  # noqa: E402


def rel(tag, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


class LatestPerTrack(unittest.TestCase):
    def test_picks_newest_per_track(self):
        best = check_releases.latest_per_track([
            rel("v6.1.3"), rel("v6.1.4"), rel("v6.1.2"),
            rel("v5.8.9"), rel("v5.8.8"),
        ])
        self.assertEqual(best, {"stable": "6.1.4", "v5": "5.8.9"})

    def test_numeric_not_lexicographic_ordering(self):
        best = check_releases.latest_per_track([rel("v6.1.9"), rel("v6.1.10")])
        self.assertEqual(best["stable"], "6.1.10")

    def test_skips_drafts_prereleases_and_other_majors(self):
        best = check_releases.latest_per_track([
            rel("v6.2.0", prerelease=True),
            rel("v6.1.5", draft=True),
            rel("v7.0.0"),
            rel("v6.1.4"),
            rel("nightly"),
        ])
        self.assertEqual(best, {"stable": "6.1.4"})


class TestKeptCodenames(unittest.TestCase):
    def test_drops_eol_suites_keeps_current(self):
        names = ["stable-2204", "stable-2404", "v5-2604", "nightly-2404"]
        self.assertEqual(lib.kept_codenames(names, ["24.04", "26.04"]),
                         ["stable-2404", "v5-2604", "nightly-2404"])

    def test_unparseable_codenames_are_kept(self):
        names = ["weird", "stable-latest", "x-12345"]
        self.assertEqual(lib.kept_codenames(names, ["24.04"]), names)


class TestSuiteCodenames(unittest.TestCase):
    SCRIPT = (
        'case "$UBUNTU_VERSION" in\n'
        "  22.04) SUITE=jammy ;;\n"
        "  24.04) SUITE=noble ;;\n"
        '  *) echo "Unknown Ubuntu version" >&2; exit 1 ;;\n'
        "esac\n"
    )

    def test_parses_case_arms(self):
        self.assertEqual(lib.suite_codenames(self.SCRIPT),
                         {"22.04": "jammy", "24.04": "noble"})

    def test_releases_without_codename(self):
        codenames = {"22.04": "jammy", "24.04": "noble"}
        self.assertEqual(
            lib.releases_without_codename(["22.04", "26.10"], codenames),
            ["26.10"])
        self.assertEqual(
            lib.releases_without_codename(["22.04"], codenames), [])


class TestWatchPrSummary(unittest.TestCase):
    """PR titles from create_watch_prs.pr_summary must describe the
    branch's actual diff (PR #23's did not)."""

    V = {"stable": "6.1.4", "v5": "5.8.9"}
    S = {"6.1.4": {"podman": "6.1.4", "crun": "1.30.1"},
         "5.8.9": {"podman": "5.8.9", "crun": "1.30.1"}}
    U = ["22.04", "24.04", "26.04"]

    def test_ubuntu_only_change_names_the_release(self):
        summary = create_watch_prs.pr_summary(
            self.V, self.V, self.S, self.S, self.U, self.U + ["26.10"])
        self.assertEqual(summary, "ubuntu releases: +26.10")

    def test_ubuntu_removal_is_named(self):
        summary = create_watch_prs.pr_summary(
            self.V, self.V, self.S, self.S, self.U, ["24.04", "26.04"])
        self.assertEqual(summary, "ubuntu releases: -22.04")

    def test_podman_bump_shows_old_to_new(self):
        versions = {"stable": "6.1.5", "v5": "5.8.9"}
        components = dict(self.S)
        components["6.1.5"] = {"podman": "6.1.5", "crun": "1.30.1"}
        summary = create_watch_prs.pr_summary(
            self.V, versions, self.S, components, self.U, self.U)
        self.assertEqual(summary, "podman stable: 6.1.4 -> 6.1.5")

    def test_component_bump_in_current_stack(self):
        components = {"6.1.4": {"podman": "6.1.4", "crun": "1.30.2"},
                      "5.8.9": {"podman": "5.8.9", "crun": "1.30.1"}}
        summary = create_watch_prs.pr_summary(
            self.V, self.V, self.S, components, self.U, self.U)
        self.assertEqual(summary, "crun 1.30.1 -> 1.30.2 (stack 6.1.4)")

    def test_no_change_empty_summary(self):
        self.assertEqual(create_watch_prs.pr_summary(
            self.V, self.V, self.S, self.S, self.U, self.U), "")


class TestUbuntuWatch(unittest.TestCase):
    """The watcher seeds a new release's suite codename into
    prepare-source.sh so its PR arrives complete (the PR #23 gap)."""

    SCRIPT = (
        'case "$UBUNTU_VERSION" in\n'
        "  22.04) SUITE=jammy ;;\n"
        "  24.04) SUITE=noble ;;\n"
        "  26.04) SUITE=resolute ;;\n"
        '  *) echo "Unknown Ubuntu version: $UBUNTU_VERSION" >&2; '
        "exit 1 ;;\n"
        "esac\n"
    )

    def test_suite_codename_is_the_adjective(self):
        self.assertEqual(
            check_releases.suite_codename(
                {"cycle": "26.10", "codename": "Stonking Stingray"}),
            "stonking")

    def test_suite_codename_missing_is_none(self):
        self.assertIsNone(check_releases.suite_codename({"cycle": "26.10"}))
        self.assertIsNone(
            check_releases.suite_codename({"codename": "  "}))

    def test_seed_appends_in_release_order_before_fallback(self):
        out = check_releases.seed_suite_arm(self.SCRIPT, "26.10", "stonking")
        self.assertIn("  26.04) SUITE=resolute ;;\n"
                      "  26.10) SUITE=stonking ;;\n"
                      "  *)", out)

    def test_seed_inserts_between_existing_arms(self):
        out = check_releases.seed_suite_arm(self.SCRIPT, "25.04", "plucky")
        self.assertIn("  24.04) SUITE=noble ;;\n"
                      "  25.04) SUITE=plucky ;;\n"
                      "  26.04) SUITE=resolute ;;", out)

    def test_seed_is_idempotent(self):
        once = check_releases.seed_suite_arm(self.SCRIPT, "26.10", "stonking")
        self.assertEqual(
            check_releases.seed_suite_arm(once, "26.10", "stonking"), once)
        self.assertEqual(
            check_releases.seed_suite_arm(self.SCRIPT, "24.04", "noble"),
            self.SCRIPT)

    def test_seeded_arm_satisfies_the_codename_guard(self):
        out = check_releases.seed_suite_arm(self.SCRIPT, "26.10", "stonking")
        self.assertEqual(
            lib.releases_without_codename(
                ["22.04", "24.04", "26.04", "26.10"],
                lib.suite_codenames(out)),
            [])


class TestWatchGate(unittest.TestCase):
    BASE_V = {"stable": "6.1.3", "v5": "5.8.8"}
    STACK_613 = {"podman": "6.1.3", "crun": "1.30.1", "go": "1.26.1",
                 "passt": "20260925", "passt_sha": "abc"}
    BASE_S = {"6.1.3": STACK_613, "5.8.8": {"podman": "5.8.8"}}

    def _eval(self, head_v, head_s, files=("versions.json",
                                           "packaging/components.json")):
        return check_watch_pr.evaluate(self.BASE_V, head_v, self.BASE_S,
                                       head_s, list(files))

    def test_patch_bump_with_clean_seed_is_eligible(self):
        head_v = {"stable": "6.1.4", "v5": "5.8.8"}
        seeded = dict(self.STACK_613, podman="6.1.4")
        head_s = dict(self.BASE_S, **{"6.1.4": seeded})
        ok, reasons = self._eval(head_v, head_s)
        self.assertTrue(ok, reasons)

    def test_minor_bump_is_not_eligible(self):
        head_v = {"stable": "6.2.0", "v5": "5.8.8"}
        seeded = dict(self.STACK_613, podman="6.2.0")
        head_s = dict(self.BASE_S, **{"6.2.0": seeded})
        ok, _ = self._eval(head_v, head_s)
        self.assertFalse(ok)

    def test_hand_edited_seed_is_not_eligible(self):
        head_v = {"stable": "6.1.4", "v5": "5.8.8"}
        seeded = dict(self.STACK_613, podman="6.1.4", crun="1.31.0")
        head_s = dict(self.BASE_S, **{"6.1.4": seeded})
        ok, _ = self._eval(head_v, head_s)
        self.assertFalse(ok)

    def test_extra_file_is_not_eligible(self):
        head_v = {"stable": "6.1.4", "v5": "5.8.8"}
        seeded = dict(self.STACK_613, podman="6.1.4")
        head_s = dict(self.BASE_S, **{"6.1.4": seeded})
        ok, _ = self._eval(head_v, head_s,
                           files=("versions.json",
                                  "packaging/components.json", "README.md"))
        self.assertFalse(ok)

    def test_two_tracks_changed_is_not_eligible(self):
        # Both tracks bump but the seeded stacks are missing entirely.
        head_v = {"stable": "6.1.4", "v5": "5.8.9"}
        head_s = dict(self.BASE_S)
        ok, _ = self._eval(head_v, head_s)
        self.assertFalse(ok)

    def test_two_track_bundle_with_clean_seeds_is_eligible(self):
        # The common real case (PR #13): stable and v5 patch bumps land
        # in one watch PR, each with a clean seeded stack.
        head_v = {"stable": "6.1.4", "v5": "5.8.9"}
        head_s = dict(self.BASE_S)
        head_s["6.1.4"] = dict(self.STACK_613, podman="6.1.4")
        head_s["5.8.9"] = dict(self.BASE_S["5.8.8"], podman="5.8.9")
        ok, reasons = self._eval(head_v, head_s)
        self.assertTrue(ok, reasons)

    def test_note_metadata_difference_is_ignored(self):
        # components.json carries a `_note` string alongside the stacks;
        # it is documentation, not a pin stack.
        base_s = dict(self.BASE_S, _note="old wording")
        head_v = {"stable": "6.1.4", "v5": "5.8.8"}
        seeded = dict(self.STACK_613, podman="6.1.4")
        head_s = dict(self.BASE_S, **{"6.1.4": seeded},
                      _note="new wording")
        ok, reasons = check_watch_pr.evaluate(
            self.BASE_V, head_v, base_s, head_s,
            ["versions.json", "packaging/components.json"])
        self.assertTrue(ok, reasons)

    def test_is_patch_bump(self):
        self.assertTrue(check_watch_pr.is_patch_bump("6.1.3", "6.1.4"))
        self.assertFalse(check_watch_pr.is_patch_bump("6.1.3", "6.2.0"))
        self.assertFalse(check_watch_pr.is_patch_bump("6.1.3", "6.1.3"))
        self.assertFalse(check_watch_pr.is_patch_bump("6.1.3", "7.0.0"))
        self.assertFalse(check_watch_pr.is_patch_bump("6.1.3", "nightly"))


class TestParseListed(unittest.TestCase):
    """reprepro list lines are suite|component|arch: name version. The
    arch token drives the Architecture term of the stale-entry
    removefilter; dropping it (the old arch-blind code) made the arm64
    replacement delete the just-included amd64 entry."""

    def test_parses_architectures(self):
        out = ("v5-2404|main|amd64: aardvark-dns 1.16.0-1~noble\n"
               "v5-2404|main|arm64: aardvark-dns 1.16.0-1~noble\n"
               "v5-2404|main|all: containers-common 0.69.2-1~noble\n")
        self.assertEqual(
            publish_repo.parse_listed(out),
            {("1.16.0-1~noble", "amd64"), ("1.16.0-1~noble", "arm64"),
             ("0.69.2-1~noble", "all")})

    def test_ignores_noise(self):
        self.assertEqual(publish_repo.parse_listed(""), set())
        self.assertEqual(publish_repo.parse_listed("garbage line\n"),
                         set())


class TestGoModChecks(unittest.TestCase):
    def test_parse_go_directive(self):
        text = "module github.com/containers/podman/v6\n\ngo 1.25\n"
        self.assertEqual(verify_upstream_pins.parse_go_directive(text),
                         (1, 25))
        self.assertIsNone(verify_upstream_pins.parse_go_directive(
            "module x\n"))

    def test_go_pin_ok(self):
        self.assertTrue(verify_upstream_pins.go_pin_ok("1.26.1", (1, 25)))
        self.assertTrue(verify_upstream_pins.go_pin_ok("1.25.0", (1, 25)))
        self.assertFalse(verify_upstream_pins.go_pin_ok("1.24.9", (1, 25)))


class TestReleaseNotes(unittest.TestCase):
    def test_notes_cover_stack_and_suites(self):
        stack = {"podman": "6.1.4", "crun": "1.30.1", "passt_sha": "abc"}
        notes = create_releases.release_notes(
            "stable", "6.1.4", stack, ["24.04", "26.04"])
        self.assertIn("podman 6.1.4 (stable", notes)
        self.assertIn("| crun | 1.30.1 |", notes)
        self.assertIn("stable-2404", notes)
        self.assertIn("stable-2604", notes)
        self.assertNotIn("passt_sha", notes)


class TestManifestLoading(unittest.TestCase):
    def test_load_manifest_falls_back_to_seed(self):
        # Point the fetch at a dead URL: load_manifest must not raise,
        # and must fall back to main's seed manifest.json.
        saved = lib.MANIFEST_URL
        lib.MANIFEST_URL = "http://127.0.0.1:9/manifest.json"
        lib._MANIFEST_CACHE = None
        try:
            built = lib.load_manifest()
            self.assertIsInstance(built, dict)
            self.assertIn("podman", built)
        finally:
            lib.MANIFEST_URL = saved
            lib._MANIFEST_CACHE = None


class RepoInvariants(unittest.TestCase):
    def test_every_tracked_version_has_a_pin_stack(self):
        # The PR #13 regression: build_matrix.py hard-fails on this.
        pins = lib.stack_pins()
        for track, ver in lib.versions().items():
            self.assertIn(ver, pins,
                          f"podman {ver} (track {track}) has no stack pin "
                          f"in packaging/components.json")

    def test_pin_stack_podman_field_matches_its_key(self):
        for ver, stack in lib.stack_pins().items():
            if ver == "_note":
                continue
            self.assertEqual(stack.get("podman"), ver,
                             f"stack {ver}: podman field is "
                             f"{stack.get('podman')!r}")

    def test_every_stack_component_resolves(self):
        # Mirrors build_matrix.resolve_version: each component's version
        # must come from the stack (or the podman version itself).
        meta = lib.component_meta()
        for ver, stack in lib.stack_pins().items():
            if ver == "_note":
                continue
            for comp, m in meta.items():
                if m.get("version_from", "self") == "podman":
                    continue
                self.assertIn(comp, stack,
                              f"stack {ver} is missing a pin for {comp}")

    def test_suites_shape(self):
        suites = lib.suites()
        self.assertIn("ubuntu", suites)
        self.assertIn("tracks", suites)
        self.assertTrue(suites["ubuntu"])
        self.assertEqual(set(suites["tracks"]), set(lib.versions().keys()))

    def test_every_ubuntu_release_has_a_suite_codename(self):
        # The PR #23 near-miss: the watcher added Ubuntu 26.10 to
        # ubuntu-versions.json with no codename arm in
        # prepare-source.sh, whose case statement exits 1 on unknown
        # versions; merging would have failed every 26.10 build cell.
        missing = lib.releases_without_codename(lib.ubuntu_releases())
        self.assertEqual(missing, [],
                         f"releases with no suite codename in "
                         f"scripts/prepare-source.sh: {missing}")


class BuildMatrix(unittest.TestCase):
    def test_build_matrix_validates_current_repo_state(self):
        # Runs the same entry point as orchestrate's setup job; a missing
        # pin stack or unresolved component fails here in seconds.
        out = os.path.join(ROOT, "tests", ".github_output.tmp")
        env = dict(os.environ, GITHUB_OUTPUT=out)
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts",
                                              "build_matrix.py")],
                capture_output=True, text=True, env=env, cwd=ROOT)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out) as f:
                emitted = dict(l.split("=", 1) for l in f.read().splitlines())
            for key in ("level0", "level1", "suites"):
                self.assertIn(key, emitted)
                json.loads(emitted[key])
        finally:
            if os.path.exists(out):
                os.unlink(out)


if __name__ == "__main__":
    unittest.main()


class TestThreeWayMerge(unittest.TestCase):
    def test_branch_only_and_main_only_changes_combine(self):
        base = {"a": 1, "b": 2}
        ours = {"a": 1, "b": 3, "c": 4}    # main changed b, added c
        theirs = {"a": 9, "b": 2, "d": 5}  # branch changed a, added d
        self.assertEqual(
            rebase_watch_prs.three_way_merge(base, ours, theirs, "t"),
            {"a": 9, "b": 3, "c": 4, "d": 5})

    def test_both_changed_same_key_differently_conflicts(self):
        with self.assertRaises(rebase_watch_prs.RealConflict):
            rebase_watch_prs.three_way_merge(
                {"a": 1}, {"a": 2}, {"a": 3}, "t")

    def test_identical_change_on_both_sides_is_fine(self):
        self.assertEqual(
            rebase_watch_prs.three_way_merge(
                {"a": 1}, {"a": 2}, {"a": 2}, "t"), {"a": 2})


class TestScanPublished(unittest.TestCase):
    import scan_published as sp

    def test_parse_index_filenames(self):
        text = ("Package: crun\nFilename: pool/main/c/crun/crun_1.deb\n\n"
                "Package: conmon\nFilename: pool/main/c/conmon/conmon_2.deb\n")
        self.assertEqual(self.sp.parse_index_filenames(text),
                         {"pool/main/c/crun/crun_1.deb",
                          "pool/main/c/conmon/conmon_2.deb"})

    def test_summarise_sorts_worst_first(self):
        grype = {"matches": [
            {"vulnerability": {"id": "CVE-2", "severity": "Low",
                               "fix": {"versions": []}},
             "artifact": {"name": "passt", "version": "1"}},
            {"vulnerability": {"id": "CVE-1", "severity": "High",
                               "fix": {"versions": ["2.0"]}},
             "artifact": {"name": "crun", "version": "1.0"}},
        ]}
        out = self.sp.summarise(grype)
        self.assertEqual([f["id"] for f in out], ["CVE-1", "CVE-2"])
        self.assertEqual(out[0]["fixed_in"], "2.0")
        self.assertEqual(self.sp.summarise({"matches": []}), [])


class TestComponentWatch(unittest.TestCase):
    def test_strips_v_prefix_and_picks_newest(self):
        rels = [rel("v2.2.0"), rel("v2.2.1"), rel("v2.1.0"),
                rel("v9.9.9", prerelease=True)]
        self.assertEqual(
            check_releases.latest_component_version(rels, "v"), "2.2.1")

    def test_crun_tags_have_no_prefix(self):
        rels = [rel("1.21"), rel("1.30.1"), rel("1.9.0")]
        self.assertEqual(
            check_releases.latest_component_version(rels, ""), "1.30.1")

    def test_container_libs_filters_to_common_tags(self):
        rels = [rel("common/v0.69.2"), rel("common/v0.70.0"),
                rel("podman/v6.0.0"), rel("image/v5.0.0")]
        self.assertEqual(
            check_releases.latest_component_version(rels, "common/v"),
            "0.70.0")

    def test_component_bump_stays_within_major_line(self):
        self.assertTrue(check_releases.component_bump_ok("1.21", "1.30.1"))
        self.assertTrue(check_releases.component_bump_ok("2.1.13", "2.2.1"))
        self.assertTrue(check_releases.component_bump_ok("0.67.2", "0.69.2"))
        self.assertFalse(check_releases.component_bump_ok("1.16.0", "2.1.0"))

    def test_upstreams_cover_the_prepare_source_components(self):
        # Every GitHub-hosted component prepare-source.sh fetches must
        # be watched (passt is git-based and handled separately).
        self.assertEqual(
            sorted(check_releases.COMPONENT_UPSTREAMS),
            ["aardvark-dns", "conmon", "containers-common", "crun",
             "netavark"])


class TestKeyExpiry(unittest.TestCase):
    # Colon output captured from the real keys/apt-signing.asc
    # (gpg --show-keys --with-colons), trimmed to the records the
    # parser reads.
    COLONS = (
        "pub:u:255:22:C64FD8BDD7A7AF10:1791039686:1854111686::u:::cSC:::::ed25519:::0:\n"
        "fpr:::::::::4A01F97F3F81FE97A40651B6C64FD8BDD7A7AF10:\n"
        "sub:u:255:22:7CE75EB1AAB75813:1791039686:1822575686:::::s:::::ed25519::\n"
        "fpr:::::::::4C597668137D76FC625D49117CE75EB1AAB75813:\n"
    )

    def test_parses_signing_subkey_expiry(self):
        fpr, expiry = check_key_expiry.parse_subkey_expiry(self.COLONS)
        self.assertEqual(fpr, "4C597668137D76FC625D49117CE75EB1AAB75813")
        # 1822575686 == 2027-10-03 (read from the real key, Oct 2026).
        self.assertEqual(expiry, 1822575686)

    def test_real_key_file_matches(self):
        fpr, expiry = check_key_expiry.read_key(
            os.path.join(ROOT, "keys", "apt-signing.asc"))
        self.assertEqual(fpr, "4C597668137D76FC625D49117CE75EB1AAB75813")
        self.assertEqual(expiry, 1822575686)

    def test_assess_thresholds(self):
        self.assertEqual(check_key_expiry.assess(None), "ok")
        self.assertEqual(check_key_expiry.assess(61), "ok")
        self.assertEqual(check_key_expiry.assess(60), "warn")
        self.assertEqual(check_key_expiry.assess(31), "warn")
        self.assertEqual(check_key_expiry.assess(30), "escalate")
        self.assertEqual(check_key_expiry.assess(0), "escalate")


@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb not installed")
class TestInstallDebsSelection(unittest.TestCase):
    """install_debs.select_debs: an arch-all .deb built on amd64 must be
    selectable by an arm64 smoke cell (build_matrix builds arch-all
    components once, on amd64), while an arch-specific .deb built for a
    different arch must not be."""

    import install_debs as idb

    def _artifact(self, root, name, meta_arch, deb_arch, pkg):
        d = os.path.join(root, name)
        os.makedirs(d)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"component": pkg, "version": "1.0",
                       "track": "stable", "ubuntu": "24.04",
                       "arch": meta_arch}, f)
        src = os.path.join(root, f"src-{name}")
        os.makedirs(os.path.join(src, "DEBIAN"))
        with open(os.path.join(src, "DEBIAN", "control"), "w") as f:
            f.write(f"Package: {pkg}\nVersion: 1.0-1\n"
                    f"Architecture: {deb_arch}\n"
                    f"Maintainer: Test <t@example>\n"
                    f"Description: fake package\n")
        deb = os.path.join(d, f"{pkg}_1.0-1_{deb_arch}.deb")
        for dirpath, _dirs, _files in os.walk(src):
            os.chmod(dirpath, 0o755)
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group",
                        src, deb], check=True, capture_output=True)
        return deb

    def test_arch_all_foreign_build_matches_specific_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = os.path.join(td, "debs")
            os.makedirs(root)
            all_deb = self._artifact(root, "debs-cc-1-stable-u24.04-amd64",
                                     "amd64", "all", "containers-common")
            self._artifact(root, "debs-crun-1-stable-u24.04-amd64",
                           "amd64", "amd64", "crun")
            arm_deb = self._artifact(root, "debs-crun-1-stable-u24.04-arm64",
                                     "arm64", "arm64", "crun")
            got = self.idb.select_debs("stable", "24.04", "arm64", root)
            self.assertIn(all_deb, got)
            self.assertIn(arm_deb, got)
            self.assertEqual(len(got), 2)
            got_amd = self.idb.select_debs("stable", "24.04", "amd64", root)
            self.assertEqual(len(got_amd), 2)
            self.assertEqual(self.idb.select_debs("v5", "24.04", "arm64",
                                                 root), [])


class TestSuiteDependencyFloors(unittest.TestCase):
    """podman-suite must floor every component at a shipped stack
    version. Unversioned Depends are satisfied by Ubuntu's older
    distro components on any host that already has them, which left
    podman 6.1.4 running with netavark 1.4.0 (unparsable network
    options), crun 1.14.1 and conmon 2.1.10 on the first real host
    install (repo-health host-smoke, 2026-10-15). Clean containers
    never showed it: with nothing preinstalled, apt pulls the whole
    stack from this repo anyway."""

    def test_suite_template_floors_every_component(self):
        path = os.path.join(ROOT, "packaging", "podman-suite",
                            "debian.in", "control")
        with open(path) as f:
            text = f.read()
        for comp in ("conmon", "crun", "netavark", "aardvark-dns",
                     "passt", "containers-common"):
            self.assertRegex(
                text, rf"{comp} \(>= [0-9][^)]*@SUITE@\)",
                f"podman-suite Depends must floor {comp}")
        self.assertIn("podman (= @PODMAN_VERSION@-1~@SUITE@)", text)


class TestForceRebuildPlumbing(unittest.TestCase):
    """force_rebuild must reach build.yml's skip check, not just the
    setup matrix. Run 38833598382 (2026-10-16) dispatched with
    force_rebuild=true on an unchanged tree: setup emitted every
    cell, every build self-skipped as already published, smoke
    downloaded zero artifacts and failed all 12 cells."""

    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts)) as f:
            return f.read()

    def test_build_workflow_declares_and_honors_force(self):
        build = self._read(".github", "workflows", "build.yml")
        self.assertIn("force:", build)
        self.assertIn('inputs.force', build)

    def test_orchestrate_passes_force_to_both_levels(self):
        orch = self._read(".github", "workflows", "orchestrate.yml")
        self.assertEqual(
            orch.count("force: ${{ inputs.force_rebuild || false }}"), 2)


class TestInstallPublished(unittest.TestCase):
    """install_published.sh's documented contract: exit 3 when the
    cell's suite has no published podman yet. PR #23's smoke cells
    proved a brand-new suite instead died with curl's exit 22: the
    missing index (HTTP 404) failed index_podman_version's pipeline
    under pipefail before the empty-result check could run."""

    def test_missing_index_exits_3_not_curl_22(self):
        with tempfile.TemporaryDirectory() as td:
            stub = os.path.join(td, "bin")
            os.makedirs(stub)
            # curl: every fetch 404s, like a suite that never published.
            with open(os.path.join(stub, "curl"), "w") as f:
                f.write("#!/bin/sh\nexit 22\n")
            with open(os.path.join(stub, "apt-get"), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            with open(os.path.join(stub, "dpkg"), "w") as f:
                f.write("#!/bin/sh\necho amd64\n")
            for name in ("curl", "apt-get", "dpkg"):
                os.chmod(os.path.join(stub, name), 0o755)
            env = dict(os.environ,
                       PATH=stub + os.pathsep + os.environ["PATH"])
            proc = subprocess.run(
                ["bash", os.path.join(ROOT, "scripts",
                                      "install_published.sh"),
                 "stable", "26.10"],
                capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
            self.assertIn("NO-PUBLISHED-STACK", proc.stdout)
