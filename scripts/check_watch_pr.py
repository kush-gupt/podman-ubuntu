#!/usr/bin/env python3
"""Decide whether a watch PR may auto-merge without human review.

Eligible only when ALL of these hold (anything else is left for a
human, by design):

  * the PR touches only versions.json and packaging/components.json;
  * every changed track is a patch-level bump (same major.minor,
    higher patch); watch PRs often bundle the stable and v5 bumps
    released in the same window, so any number of tracks may change;
  * packaging/components.json gains exactly the new versions' stacks
    and loses/changes nothing;
  * each new stack is byte-for-byte its track's previous stack with
    only the `podman` field replaced (what check_releases.py seeds;
    any human edit to a seeded stack disqualifies the PR).

Prints `eligible=true|false` (for $GITHUB_OUTPUT) plus the reasons.
Used by watch-auto-merge.yml. Stdlib only.
"""
import argparse
import json
import os
import re
import subprocess
import sys

ALLOWED_FILES = {"versions.json", "packaging/components.json"}
VERSION_KEY = re.compile(r"^\d+\.\d+\.\d+$")


def stack_keys(stacks):
    """Only version-shaped keys are stacks (components.json also
    carries metadata like `_note`)."""
    return {k for k in stacks if VERSION_KEY.match(k)}


def parse_version(v):
    try:
        parts = tuple(int(x) for x in v.split("."))
    except ValueError:
        return None
    return parts if len(parts) == 3 else None


def is_patch_bump(old, new):
    o, n = parse_version(old), parse_version(new)
    return bool(o and n and o[0] == n[0] and o[1] == n[1] and n[2] > o[2])


def evaluate(base_v, head_v, base_s, head_s, changed_files):
    """-> (eligible: bool, reasons: list[str])"""
    reasons = []
    extra = set(changed_files) - ALLOWED_FILES
    if extra:
        reasons.append(f"touches files beyond the watch set: {sorted(extra)}")
    changed = [t for t in head_v if head_v.get(t) != base_v.get(t)]
    if not changed:
        reasons.append("no track version changed")
        return False, reasons
    bumps = {}
    for track in changed:
        old, new = base_v[track], head_v[track]
        bumps[track] = (old, new)
        if not is_patch_bump(old, new):
            reasons.append(
                f"{track}: {old} -> {new} is not a patch-level bump")
    added = stack_keys(head_s) - stack_keys(base_s)
    removed = stack_keys(base_s) - stack_keys(head_s)
    expected_added = {new for _old, new in bumps.values()}
    if added != expected_added:
        reasons.append(f"components.json added stacks {sorted(added)}, "
                       f"expected exactly {sorted(expected_added)}")
    if removed:
        reasons.append(f"components.json removed stacks {sorted(removed)}")
    for k in stack_keys(base_s):
        if k in head_s and head_s[k] != base_s[k]:
            reasons.append(f"components.json modified existing stack {k}")
    for track, (old, new) in bumps.items():
        if old not in base_s:
            reasons.append(f"no base stack for previous version {old}")
        elif new in head_s:
            expected = dict(base_s[old])
            expected["podman"] = new
            if head_s[new] != expected:
                diff = [k for k in expected
                        if head_s[new].get(k) != expected.get(k)]
                reasons.append(
                    f"{track}: seeded stack differs from the previous "
                    f"stack beyond the podman pin: {diff}")
    if reasons:
        return False, reasons
    summary = "; ".join(f"{t}: patch bump {o} -> {n}"
                        for t, (o, n) in sorted(bumps.items()))
    return True, [f"{summary}; seeded stacks are the previous stacks "
                  "with only the podman pin changed"]


def git_show(ref, path):
    out = subprocess.run(["git", "show", f"{ref}:{path}"],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def load_worktree(path):
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main",
                    help="git ref holding the pre-PR state")
    args = ap.parse_args()
    # Compare against the merge base, not the ref's tip: main may have
    # moved since the branch forked, and its changes are not the PR's.
    mb = subprocess.run(["git", "merge-base", args.base, "HEAD"],
                        capture_output=True, text=True)
    base = mb.stdout.strip() or args.base
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        capture_output=True, text=True, check=True)
    changed_files = [l for l in out.stdout.splitlines() if l.strip()]
    eligible, reasons = evaluate(
        git_show(base, "versions.json"), load_worktree("versions.json"),
        git_show(base, "packaging/components.json"),
        load_worktree("packaging/components.json"),
        changed_files)
    for r in reasons:
        print(r)
    print(f"eligible={'true' if eligible else 'false'}")


if __name__ == "__main__":
    main()
