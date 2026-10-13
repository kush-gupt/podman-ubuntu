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
import lib  # noqa: E402


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
