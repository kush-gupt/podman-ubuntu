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
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_key_expiry  # noqa: E402
import check_releases  # noqa: E402
import check_watch_pr  # noqa: E402
import create_releases  # noqa: E402
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
