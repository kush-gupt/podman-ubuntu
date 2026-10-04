#!/usr/bin/env python3
"""Create PRs for watch branches that don't have them yet.

The watch-upstream workflow pushes branches but GITHUB_TOKEN can't create PRs.
This script uses the PAT to create PRs for orphaned watch branches.
"""
import json, sys, os, base64
sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request
import urllib.request, urllib.error

REPO = "kush-gupt/podman-ubuntu"

def api(method, path, payload=None):
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

# Get all watch branches
branches = api("GET", "/branches?per_page=100")
watch_branches = [b["name"] for b in branches if b["name"].startswith("watch/")]

# Get open PRs
prs = api("GET", "/pulls?state=open&per_page=100")
pr_heads = {p["head"]["ref"] for p in prs}
# Also collect the versions.json content of open watch PRs to detect
# duplicates (same upstream changes, different branch stamps).
pr_versions = {}
for p in prs:
    head = p["head"]["ref"]
    if not head.startswith("watch/"):
        continue
    try:
        content = api("GET", f"/contents/versions.json?ref={head}")
        pr_versions[head] = base64.b64decode(content["content"]).decode()
    except Exception:
        pass
seen_versions = set(pr_versions.values())

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
        # Get versions.json from the branch to build title
        content = api("GET", f"/contents/versions.json?ref={branch}")
        versions_raw = base64.b64decode(content["content"]).decode()
        if versions_raw in seen_versions:
            print(f"Skipping {branch}: duplicate of an open watch PR, deleting branch")
            try:
                api("DELETE", f"/git/refs/heads/{branch}")
            except Exception as e:
                print(f"  could not delete {branch}: {e}")
            continue
        versions = json.loads(versions_raw)
        summary = f"podman stable: {versions.get('stable')}, v5: {versions.get('v5')}"
    except Exception as e:
        print(f"Skipping {branch}: {e}")
        continue
    
    # Create PR
    pr = api("POST", "/pulls", {
        "title": f"chore(watch): {summary}",
        "head": branch,
        "base": "main",
        "body": f"Automated upstream watch found changes in {', '.join(files)}.\n\n## Release checklist\n\n- [ ] Fill dependency pins in `packaging/components.json`\n- [ ] Bump toolchain versions if needed\n- [ ] Merging triggers the build matrix.",
    })
    if pr:
        created.append((pr["number"], branch))
        print(f"Created PR #{pr['number']} for {branch}")

if not created:
    print("No new PRs needed.")
