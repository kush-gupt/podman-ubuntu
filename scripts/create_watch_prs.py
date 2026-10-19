#!/usr/bin/env python3
"""Create PRs for watch branches that don't have them yet.

The watch-upstream workflow pushes branches but GITHUB_TOKEN can't create PRs.
This script uses the PAT to create PRs for orphaned watch branches.
"""
import json, sys, os, base64
import urllib.request, urllib.error

REPO = "kush-gupt/podman-ubuntu"

def api(method, path, payload=None):
    sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
    from dynamic_credentials import add_surrogate_to_request
    url = f"https://api.github.com/repos/{REPO}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/vnd.github+json")
    add_surrogate_to_request(req, "custom.github", allowed_hosts=("api.github.com",))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def file_at(ref, path):
    content = api("GET", f"/contents/{path}?ref={ref}")
    return base64.b64decode(content["content"]).decode()

def pr_summary(main_versions, versions, main_components, components,
               main_ubuntu, ubuntu):
    """One-line description of what a watch branch changes, used as the
    PR title. Names every kind of change and nothing else: the old
    title restated the branch's current podman versions unconditionally,
    so PR #23 (an Ubuntu-set-only change adding 26.10) went out titled
    like a podman bump with the already-live versions."""
    parts = []
    seed_base = {}
    for track in sorted(set(main_versions) | set(versions)):
        old, new = main_versions.get(track), versions.get(track)
        if old != new:
            parts.append(f"podman {track}: {old} -> {new}")
            if new is not None and old is not None:
                seed_base[new] = old
    for key in sorted(set(versions.values())):
        new_stack = components.get(key, {})
        # A newly seeded stack is absent from main's components.json;
        # diff it against the stack it was seeded from (the track's
        # previous version), not against nothing.
        old_stack = main_components.get(
            key, main_components.get(seed_base.get(key), {}))
        for comp in sorted(set(new_stack) | set(old_stack)):
            if comp == "podman":
                continue  # covered by the per-track part above
            if new_stack.get(comp) != old_stack.get(comp):
                parts.append(
                    f"{comp} {old_stack.get(comp)} -> "
                    f"{new_stack.get(comp)} (stack {key})")
    added = [r for r in ubuntu if r not in main_ubuntu]
    removed = [r for r in main_ubuntu if r not in ubuntu]
    if added or removed:
        bits = [f"+{r}" for r in added] + [f"-{r}" for r in removed]
        parts.append(f"ubuntu releases: {', '.join(bits)}")
    return "; ".join(parts)

def main():
    # Get all watch branches
    branches = api("GET", "/branches?per_page=100")
    watch_branches = [b["name"] for b in branches if b["name"].startswith("watch/")]

    # Get open PRs
    prs = api("GET", "/pulls?state=open&per_page=100")
    pr_heads = {p["head"]["ref"] for p in prs}
    # Also collect the versions.json + components.json + ubuntu-versions.json
    # content of open watch PRs to detect duplicates (same upstream changes,
    # different branch stamps). The dedupe key must cover components.json
    # too: component-only watch branches leave versions.json identical to
    # main, and keying on versions alone would mistake a component PR for a
    # duplicate of an unrelated podman PR (and delete its branch). The
    # Ubuntu set belongs in the key for the same reason.
    pr_state = {}
    for p in prs:
        head = p["head"]["ref"]
        if not head.startswith("watch/"):
            continue
        try:
            pr_state[head] = (file_at(head, "versions.json"),
                              file_at(head, "packaging/components.json"),
                              file_at(head, "ubuntu-versions.json"))
        except Exception:
            pass
    seen_state = set(pr_state.values())

    main_versions = json.loads(file_at("main", "versions.json"))
    main_components = json.loads(file_at("main", "packaging/components.json"))
    main_ubuntu = json.loads(file_at("main", "ubuntu-versions.json"))["releases"]

    created = []
    for branch in watch_branches:
        if branch in pr_heads:
            continue
        # Get the diff to build a summary
        try:
            compare = api("GET", f"/compare/main...{branch}")
            files = [f["filename"] for f in compare.get("files", [])]
            if not files:
                print(f"Skipping {branch}: no diff")
                continue
            # Get the state files from the branch to build the dedupe
            # key and the title summary.
            versions_raw = file_at(branch, "versions.json")
            components_raw = file_at(branch, "packaging/components.json")
            ubuntu_raw = file_at(branch, "ubuntu-versions.json")
            if (versions_raw, components_raw, ubuntu_raw) in seen_state:
                print(f"Skipping {branch}: duplicate of an open watch PR, deleting branch")
                try:
                    api("DELETE", f"/git/refs/heads/{branch}")
                except Exception as e:
                    print(f"  could not delete {branch}: {e}")
                continue
            summary = pr_summary(
                main_versions, json.loads(versions_raw),
                main_components, json.loads(components_raw),
                main_ubuntu, json.loads(ubuntu_raw)["releases"],
            ) or f"watch update ({branch})"
        except Exception as e:
            print(f"Skipping {branch}: {e}")
            continue

        body_lines = [
            f"Automated upstream watch found changes in {', '.join(files)}.",
            "",
        ]
        if "versions.json" in files or "packaging/components.json" in files:
            body_lines += [
                "Dependency pins for new podman versions are seeded in "
                "`packaging/components.json` by carrying the previous "
                "release's stack forward.",
                "",
            ]
        if "ubuntu-versions.json" in files:
            body_lines += [
                "Ubuntu release set changed: each added release needs a "
                "suite codename arm in `scripts/prepare-source.sh` (the "
                "watcher seeds one from endoflife.date) and a builder "
                "image; script-tests fails until the codename exists.",
                "",
            ]
        body_lines += [
            "## Release checklist",
            "",
            "- [ ] Verify the seeded dependency pins in `packaging/components.json` against the upstream release notes",
            "- [ ] Bump toolchain versions in the seeded stack if the release notes require it",
            "- [ ] Merging triggers the build matrix.",
        ]

        # Create PR
        pr = api("POST", "/pulls", {
            "title": f"chore(watch): {summary}",
            "head": branch,
            "base": "main",
            "body": "\n".join(body_lines) + "\n",
        })
        if pr:
            created.append((pr["number"], branch))
            print(f"Created PR #{pr['number']} for {branch}")

    if not created:
        print("No new PRs needed.")

if __name__ == "__main__":
    main()
