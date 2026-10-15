#!/usr/bin/env python3
"""Watch upstream Podman releases, the stack components' upstream
releases, and the Ubuntu support calendar.

Compares against versions.json / ubuntu-versions.json (the watermarks)
and the current stacks in packaging/components.json. On change,
updates the files and emits GitHub Actions outputs so the workflow
can open a single PR. Idempotent: no change, no output.
Stdlib only (passt's upstream is a plain git repo, read with git).
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import date, datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_FILE = os.path.join(REPO_ROOT, "versions.json")
UBUNTU_FILE = os.path.join(REPO_ROOT, "ubuntu-versions.json")
COMPONENTS_FILE = os.path.join(REPO_ROOT, "packaging", "components.json")
PR_BODY = "/tmp/watch-pr-body.md"

PODMAN_API = "https://api.github.com/repos/containers/podman/releases?per_page=100"
UBUNTU_EOL_API = "https://endoflife.date/api/ubuntu.json"

# Component upstream mapping, mirroring scripts/prepare-source.sh's
# tarball URLs. Value: (github repo, tag prefix to strip). crun tags
# carry no "v"; container-libs tags its common releases "common/vX".
# passt is handled separately: it has no version tags at all, the
# stack pins a commit SHA from https://passt.top/passt plus the
# snapshot date of that commit.
COMPONENT_UPSTREAMS = {
    "crun": ("containers/crun", ""),
    "conmon": ("containers/conmon", "v"),
    "netavark": ("containers/netavark", "v"),
    "aardvark-dns": ("containers/aardvark-dns", "v"),
    "containers-common": ("podman-container-tools/container-libs",
                          "common/v"),
}
PASST_GIT_URL = "https://passt.top/passt"


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "podman-ubuntu-watch",
            **(
                {"Authorization": f"Bearer {os.environ['GH_TOKEN']}"}
                if url.startswith("https://api.github.com") and os.environ.get("GH_TOKEN")
                else {}
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def latest_per_track(releases):
    """Newest non-prerelease tag per major track (6.x stable, 5.x maintenance)."""
    best = {}
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel.get("tag_name", "")
        if not tag.startswith("v"):
            continue
        ver = tag[1:]
        major = ver.split(".")[0]
        if major not in ("5", "6"):
            continue
        track = "stable" if major == "6" else "v5"
        if track not in best or _ver_key(ver) > _ver_key(best[track]):
            best[track] = ver
    return best


def _ver_key(ver):
    parts = []
    for p in ver.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def latest_component_version(releases, prefix):
    """Newest non-prerelease release version for one component, with
    the repo's tag prefix ("v", "common/v", "") stripped. Releases
    whose tag does not carry the prefix belong to a different
    component sharing the repo (container-libs also tags podman/v*)
    and are ignored."""
    best = None
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel.get("tag_name", "")
        if prefix:
            if not tag.startswith(prefix):
                continue
            ver = tag[len(prefix):]
        else:
            ver = tag
        if not ver or not ver[0].isdigit():
            continue
        if best is None or _ver_key(ver) > _ver_key(best):
            best = ver
    return best


def component_bump_ok(pin, latest):
    """A component watch bump must stay within the pin's major line.

    Stacks pin the versions from their podman release's notes, and
    tracks deliberately sit on different majors (the v5 stack pins
    netavark/aardvark-dns 1.16 while stable pins 2.1). Proposing a
    cross-major rewrite of a maintenance stack is not a watch bump,
    it is a track decision for a human to make unprompted. Within a
    major line, the bump is proposed and the PR's checklist + full
    build/smoke matrix + human review are the gate."""
    return pin.split(".")[0] == latest.split(".")[0]


def latest_components():
    """{component: latest upstream version} for the GitHub-hosted
    components. Network: one releases call per upstream repo."""
    out = {}
    for comp, (repo, prefix) in COMPONENT_UPSTREAMS.items():
        ver = latest_component_version(
            get_json(f"https://api.github.com/repos/{repo}/releases"
                     "?per_page=100"), prefix)
        if ver:
            out[comp] = ver
    return out


def passt_head():
    """(sha, snapshot date YYYYMMDD) of passt upstream HEAD, or None
    when upstream is unreachable (the watch then just skips passt;
    a passt outage must not block podman/component bumps)."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", PASST_GIT_URL, "HEAD"],
            capture_output=True, text=True, timeout=60, check=True)
        sha = out.stdout.split()[0]
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(
                ["git", "clone", "--quiet", "--filter=blob:none",
                 "--no-checkout", PASST_GIT_URL, td + "/passt"],
                capture_output=True, timeout=120, check=True)
            date_out = subprocess.run(
                ["git", "-C", td + "/passt", "log", "-1",
                 "--format=%cd", "--date=format:%Y%m%d", sha],
                capture_output=True, text=True, timeout=60, check=True)
        return sha, date_out.stdout.strip()
    except Exception as e:
        print(f"passt watch skipped: {e}")
        return None


def supported_ubuntu():
    data = get_json(UBUNTU_EOL_API)
    today = date.today().isoformat()
    out = []
    for cycle in data:
        eol = cycle.get("eol")
        if eol is False or (isinstance(eol, str) and eol > today):
            out.append(cycle["cycle"])
    return sorted(out)


def main():
    with open(VERSIONS_FILE) as f:
        versions = json.load(f)
    with open(UBUNTU_FILE) as f:
        ubuntu = json.load(f)

    changes = []

    with open(COMPONENTS_FILE) as f:
        components = json.load(f)

    seeded = []
    podman = latest_per_track(get_json(PODMAN_API))
    for track, ver in sorted(podman.items()):
        old = versions.get(track)
        if old != ver:
            changes.append(f"podman {track}: {old} -> {ver}")
            versions[track] = ver
            # Seed the new version's dependency stack from the previous
            # release on the track so build_matrix validation passes
            # and the PR can build. Patch releases keep the stack; the
            # PR checklist asks a human to confirm the pins against
            # the upstream release notes before merge.
            if ver not in components and old in components:
                stack = dict(components[old])
                stack["podman"] = ver
                components[ver] = stack
                seeded.append(ver)

    # Component-level watch: a crun/conmon/... release between podman
    # releases used to trigger nothing, so a component security fix
    # waited for the next podman release to reach users. Bump the pin
    # in the CURRENT stacks only (the ones versions.json points at);
    # historical stacks stay frozen as the record of what shipped.
    component_changes = []
    current_stacks = sorted(set(versions.values()))
    for comp, latest in sorted(latest_components().items()):
        for key in current_stacks:
            stack = components.get(key, {})
            pin = stack.get(comp)
            if pin and _ver_key(latest) > _ver_key(pin):
                if not component_bump_ok(pin, latest):
                    print(f"component watch: {comp} {pin} -> {latest} "
                          f"crosses a major line in stack {key}; "
                          "not proposing (track decision, see "
                          "component_bump_ok)")
                    continue
                changes.append(f"{comp} {pin} -> {latest} (stack {key})")
                component_changes.append((comp, pin, latest, key))
                stack[comp] = latest
    head = passt_head()
    if head:
        sha, snap_date = head
        for key in current_stacks:
            stack = components.get(key, {})
            if stack.get("passt_sha") and stack["passt_sha"] != sha:
                changes.append(
                    f"passt {stack.get('passt')} ({stack['passt_sha'][:7]})"
                    f" -> {snap_date} ({sha[:7]}) (stack {key})")
                component_changes.append(
                    ("passt", stack.get("passt"), snap_date, key))
                stack["passt"] = snap_date
                stack["passt_sha"] = sha

    supported = supported_ubuntu()
    if ubuntu.get("releases") != supported:
        changes.append(
            f"ubuntu set: {ubuntu.get('releases')} -> {supported}"
        )
        ubuntu["releases"] = supported

    out = os.environ.get("GITHUB_OUTPUT")
    if not changes:
        if out:
            with open(out, "a") as f:
                f.write("changed=false\n")
        print("No changes.")
        return

    with open(VERSIONS_FILE, "w") as f:
        json.dump(versions, f, indent=2)
        f.write("\n")
    with open(UBUNTU_FILE, "w") as f:
        json.dump(ubuntu, f, indent=2)
        f.write("\n")
    if seeded or component_changes:
        with open(COMPONENTS_FILE, "w") as f:
            json.dump(components, f, indent=2, sort_keys=True)
            f.write("\n")

    summary = "; ".join(changes)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    with open(PR_BODY, "w") as f:
        f.write(
            "Automated upstream watch found changes:\n\n"
            + "".join(f"- {c}\n" for c in changes)
            + "".join(
                f"- dependency pins for podman {v} seeded in "
                "`packaging/components.json` by carrying the previous "
                "release's stack forward\n"
                for v in seeded
            )
            + "".join(
                f"- component pin bump: {c} {o} -> {n} in stack {k} "
                "(verify against that component's upstream release "
                "notes; a human confirms before merge, watch "
                "auto-merge does not apply to component-only bumps)\n"
                for c, o, n, k in component_changes
            )
            + "\n## Release checklist (podman bumps)\n\n"
            "- [ ] Verify the seeded dependency stack pins in "
            "`packaging/components.json` against the upstream release notes "
            "(crun, conmon, netavark, aardvark-dns, passt, containers-common)\n"
            "- [ ] If the release notes require a newer Go/Rust toolchain, "
            "bump `go`/`rust` in the new version's stack in "
            "`packaging/components.json`\n"
            "- [ ] Merging this PR triggers the build matrix.\n"
        )

    if out:
        with open(out, "a") as f:
            f.write("changed=true\n")
            f.write(f"summary={summary}\n")
            f.write(f"stamp={stamp}\n")
    print("Changed:", summary)


if __name__ == "__main__":
    sys.exit(main())
