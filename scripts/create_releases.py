#!/usr/bin/env python3
"""Create a GitHub Release for each published (track, podman version).

Runs after a successful publish (orchestrate.yml `releases` job). For
every track in versions.json, if no release tagged `<track>-<version>`
exists yet, create one whose notes are the pinned component stack for
that podman version. Idempotent: existing releases are left alone.
Uses the `gh` CLI (GH_TOKEN from the workflow). Stdlib only.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib


def release_notes(track, version, stack, ubuntu_releases):
    suites = ", ".join(f"{track}-{u.replace('.', '')}"
                       for u in ubuntu_releases)
    rows = "\n".join(f"| {comp} | {ver} |"
                     for comp, ver in sorted(stack.items())
                     if comp != "passt_sha")
    return f"""## podman {version} ({track} track)

Published to the apt repository for suites: {suites} (amd64 + arm64).

| Component | Version |
|---|---|
{rows}

Install or upgrade with the [README instructions](https://github.com/kush-gupt/podman-ubuntu#readme) (`sudo apt install podman-suite`).
"""


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          check=check)


def main():
    versions = lib.versions()
    stacks = lib.stack_pins()
    releases = lib.ubuntu_releases()
    for track, version in sorted(versions.items()):
        tag = f"{track}-{version}"
        if gh("release", "view", tag, check=False).returncode == 0:
            print(f"release {tag} already exists; skipping")
            continue
        notes = release_notes(track, version, stacks[version], releases)
        nf = f"/tmp/release-notes-{tag}.md"
        with open(nf, "w") as f:
            f.write(notes)
        gh("release", "create", tag, "--target", "main",
           "--title", f"podman {version} ({track})",
           "--notes-file", nf)
        print(f"created release {tag}")


if __name__ == "__main__":
    main()
