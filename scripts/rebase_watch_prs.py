#!/usr/bin/env python3
"""Rebase open watch/* PRs onto current main when they conflict.

The watch bot's PRs only touch versions.json and
packaging/components.json, but main edits components.json too (for
example pinning a toolchain inside an existing stack), so a watch PR
left sitting while main moves goes 'dirty' and can never merge —
GitHub's update-branch cannot resolve it, and auto-merge stalls
forever. This script performs the resolution semantically:

- three-way merge per top-level key of versions.json and
  components.json against the merge-base: a key changed only on the
  branch takes the branch value, a key changed only on main keeps
  main's value, a key added on either side is kept, and a key
  changed differently on both sides is a REAL conflict — the PR is
  skipped and reported for a human.
- any other file the branch changed is carried verbatim, unless
  main changed it too (skip + report).
- the regenerated branch is a single commit whose parent is the
  current main head (a rebase), pushed with force, and noted with a
  PR comment.

Runs next to create_watch_prs.py with the user's token, because
pushes from GITHUB_TOKEN do not trigger PR CI.

Usage: rebase_watch_prs.py [--dry-run]
"""
import base64
import json
import sys
import urllib.error
import urllib.request

_AUTH = None


def _auth():
    """Lazily load the credential helper (only present on the
    maintainer's machine, where this script runs next to
    create_watch_prs.py). Importing this module for tests must not
    require it."""
    global _AUTH
    if _AUTH is None:
        for _p in ("/opt/hatch/workflow-skills/custom-connector-setup/bin",
                   "/opt/hatch/skills/custom-connector-setup/bin",
                   "/opt/hatch/skills/skill-creator/bin",
                   "/opt/muse/skills/custom-connector-setup/bin"):
            sys.path.insert(0, _p)
        from dynamic_credentials import add_surrogate_to_request
        _AUTH = add_surrogate_to_request
    return _AUTH


REPO = "kush-gupt/podman-ubuntu"
SEMANTIC_FILES = ("versions.json", "packaging/components.json")
DRY = "--dry-run" in sys.argv


def api(method, path, payload=None):
    url = f"https://api.github.com/repos/{REPO}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/vnd.github+json")
    _auth()(req, "custom.github", allowed_hosts=("api.github.com",))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def get_json_file(ref, path):
    d = api("GET", f"/contents/{path}?ref={ref}")
    if d is None:
        return None
    return json.loads(base64.b64decode(d["content"]))


class RealConflict(Exception):
    pass


def three_way_merge(base, ours, theirs, what):
    """Per-top-level-key three-way merge of two JSON objects.
    ours = main (wins ties it alone changed), theirs = branch."""
    out = {}
    for key in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(key), ours.get(key), theirs.get(key)
        if t == o:
            if o is not None:
                out[key] = o
        elif b == o:      # only the branch changed/added it
            if t is not None:
                out[key] = t
        elif b == t:      # only main changed/added it
            if o is not None:
                out[key] = o
        else:
            raise RealConflict(f"{what}: key {key!r} changed on both "
                               f"sides (base={b!r} main={o!r} "
                               f"branch={t!r})")
    return out


def tree_map(sha):
    commit = api("GET", f"/git/commits/{sha}")
    tree = api("GET", f"/git/trees/{commit['tree']['sha']}?recursive=1")
    return {e["path"]: e for e in tree["tree"] if e["type"] == "blob"}


def changed_files(base_sha, head_sha):
    cmp = api("GET", f"/compare/{base_sha}...{head_sha}")
    return {f["filename"] for f in cmp.get("files", [])}


def regenerate(pr):
    branch = pr["head"]["ref"]
    head_sha = pr["head"]["sha"]
    main_sha = api("GET", "/git/ref/heads/main")["object"]["sha"]
    cmp = api("GET", f"/compare/main...{branch}")
    if cmp is None or cmp.get("merge_base_commit") is None:
        return f"PR #{pr['number']}: no merge base, skipped"
    base_sha = cmp["merge_base_commit"]["sha"]
    if base_sha == main_sha:
        return None  # already based on current main

    branch_files = changed_files(base_sha, head_sha)
    main_files = changed_files(base_sha, main_sha)
    overlap = (branch_files & main_files) - set(SEMANTIC_FILES)
    if overlap:
        return (f"PR #{pr['number']}: files changed on both sides "
                f"outside the semantic set: {sorted(overlap)} — "
                f"needs a human")
    main_tree = tree_map(main_sha)
    branch_tree = tree_map(head_sha)
    entries = []
    for path in SEMANTIC_FILES:
        if path in branch_files:
            merged = three_way_merge(get_json_file(base_sha, path),
                                     get_json_file(main_sha, path),
                                     get_json_file(head_sha, path),
                                     path)
            blob = api("POST", "/git/blobs", {
                "content": base64.b64encode(
                    (json.dumps(merged, indent=2) + "\n").encode()
                ).decode(),
                "encoding": "base64"})
            entries.append({"path": path, "mode": "100644",
                            "type": "blob", "sha": blob["sha"]})
    for path in sorted(branch_files - set(SEMANTIC_FILES)):
        e = branch_tree[path]
        entries.append({"path": path, "mode": e["mode"],
                        "type": "blob", "sha": e["sha"]})
    if DRY:
        return (f"PR #{pr['number']} ({branch}): would rebase onto "
                f"main {main_sha[:8]} carrying "
                f"{sorted(branch_files)}")
    tree = api("POST", "/git/trees",
               {"base_tree": main_tree_sha(main_sha), "tree": entries})
    commit = api("POST", "/git/commits", {
        "message": f"Rebase {branch} onto main ({main_sha[:8]})\n\n"
                   "Semantic three-way merge of versions.json and "
                   "packaging/components.json against the merge-base; "
                   "keys changed only on main keep main's values, "
                   "stacks/versions added by the watch branch are "
                   "carried over. Automated by rebase_watch_prs.py.",
        "tree": tree["sha"], "parents": [main_sha]})
    api("PATCH", f"/git/refs/heads/{branch}",
        {"sha": commit["sha"], "force": True})
    api("POST", f"/issues/{pr['number']}/comments", {
        "body": f"Rebased onto current main ({main_sha[:8]}) by the "
                f"watch-PR maintenance script: the branch had "
                f"conflicts with main in the packaging pins. The "
                f"watch changes were re-applied semantically "
                f"(versions.json, packaging/components.json); CI is "
                f"re-running on the new head."})
    return (f"PR #{pr['number']} ({branch}): rebased onto main "
            f"{main_sha[:8]} as {commit['sha'][:8]}")


def main_tree_sha(main_sha):
    return api("GET", f"/git/commits/{main_sha}")["tree"]["sha"]


def main():
    prs = api("GET", "/pulls?state=open&per_page=100") or []
    actions = []
    for pr in prs:
        if not pr["head"]["ref"].startswith("watch/"):
            continue
        fresh = api("GET", f"/pulls/{pr['number']}")
        state = fresh.get("mergeable_state")
        if state == "dirty":
            try:
                r = regenerate(fresh)
            except RealConflict as e:
                r = (f"PR #{pr['number']}: REAL CONFLICT — {e} — "
                     f"needs a human")
            if r:
                actions.append(r)
        elif state == "behind":
            if DRY:
                actions.append(
                    f"PR #{pr['number']}: would update-branch")
                continue
            r = api("PUT", f"/pulls/{pr['number']}/update-branch",
                    {"expected_head_sha": pr["head"]["sha"]})
            if r is None:
                try:
                    r2 = regenerate(fresh)
                except RealConflict as e:
                    r2 = (f"PR #{pr['number']}: REAL CONFLICT — {e} "
                          f"— needs a human")
                if r2:
                    actions.append(r2)
            else:
                actions.append(
                    f"PR #{pr['number']}: branch updated with main")
    for a in actions:
        print(a)
    if not actions:
        print("No watch PRs needed rebasing.")


if __name__ == "__main__":
    main()
