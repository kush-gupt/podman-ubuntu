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

import check_releases  # noqa: E402
import check_watch_pr  # noqa: E402
import create_releases  # noqa: E402
import lib  # noqa: E402
import publish_repo  # noqa: E402
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
