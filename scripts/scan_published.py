#!/usr/bin/env python3
"""Collect the published .debs for the weekly vulnerability scan, and
turn a grype JSON report into a tracking issue.

Two modes:

  scan_published.py collect <outdir>
      Download every unique .deb the live repo currently publishes
      (stable + v5 suites, every Ubuntu release, both arches; the
      pool path is shared, so dedupe is by pool filename). The
      vuln-scan workflow then runs grype over the directory: grype's
      dpkg cataloger reads the control metadata inside each .deb.

  scan_published.py report <grype.json> [--file-issue]
      Summarise a grype JSON report on stdout. With --file-issue,
      open/update the "Vulnerability scan:" tracking issue when
      there are findings, or close it when the scan is clean (the
      same convention as check_key_expiry.py / ci-failure-alert).

Stdlib only.
"""
import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

BASE = os.environ.get(
    "LIVE_REPO_BASE", "https://kush-gupt.github.io/podman-ubuntu")
ISSUE_TITLE_PREFIX = "Vulnerability scan:"
SEVERITY_ORDER = ["Critical", "High", "Medium", "Low", "Negligible",
                  "Unknown"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent":
                                               "podman-ubuntu-scan"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def parse_index_filenames(text):
    """Packages index text -> {pool filename}."""
    out = set()
    for line in text.splitlines():
        if line.startswith("Filename: "):
            out.add(line.split(" ", 1)[1].strip())
    return out


def published_filenames():
    names = set()
    for track in ("stable", "v5"):
        for uv in lib.ubuntu_releases():
            flat = uv.replace(".", "")
            for arch in ("amd64", "arm64"):
                url = (f"{BASE}/dists/{track}-{flat}/main/"
                       f"binary-{arch}/Packages")
                names |= parse_index_filenames(fetch(url).decode())
    return sorted(names)


def collect(outdir):
    os.makedirs(outdir, exist_ok=True)
    names = published_filenames()
    for name in names:
        dest = os.path.join(outdir, os.path.basename(name))
        if not os.path.exists(dest):
            with open(dest, "wb") as f:
                f.write(fetch(f"{BASE}/{name}"))
    print(f"collected {len(names)} unique published .debs into {outdir}")
    return names


def summarise(grype):
    """grype JSON -> sorted list of finding dicts (worst first)."""
    findings = []
    for m in grype.get("matches", []):
        vuln = m.get("vulnerability", {})
        art = m.get("artifact", {})
        fix = vuln.get("fix", {}).get("versions") or []
        findings.append({
            "id": vuln.get("id", "?"),
            "severity": vuln.get("severity", "Unknown"),
            "package": art.get("name", "?"),
            "version": art.get("version", "?"),
            "fixed_in": ", ".join(fix) if fix else "",
        })

    def key(f):
        sev = (SEVERITY_ORDER.index(f["severity"])
               if f["severity"] in SEVERITY_ORDER else 99)
        return (sev, f["package"], f["id"])

    findings.sort(key=key)
    return findings


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          check=check)


def report(path, file_issue):
    with open(path) as f:
        findings = summarise(json.load(f))
    if not findings:
        print("VULN-SCAN: clean (no grype matches in published .debs)")
    else:
        print(f"VULN-SCAN: {len(findings)} findings")
        for f in findings:
            print(f"  {f['severity']:10} {f['id']:22} "
                  f"{f['package']} {f['version']}"
                  + (f" (fixed in {f['fixed_in']})" if f["fixed_in"]
                     else ""))
    if not file_issue:
        return
    repo = os.environ.get("GITHUB_REPOSITORY",
                          "kush-gupt/podman-ubuntu")
    existing = gh("issue", "list", "--repo", repo, "--state", "open",
                  "--search", ISSUE_TITLE_PREFIX, "--json", "number",
                  "--jq", ".[0].number // empty").stdout.strip()
    if not findings:
        if existing:
            gh("issue", "close", existing, "--repo", repo,
               "--comment", "Weekly vulnerability scan is clean again.")
            print(f"closed issue #{existing} (clean scan)")
        return
    rows = "\n".join(
        f"| {f['severity']} | {f['id']} | {f['package']} | "
        f"{f['version']} | {f['fixed_in']} |" for f in findings[:50])
    body = (f"Weekly grype scan of the published .debs found "
            f"{len(findings)} vulnerabilities (first 50 shown):\n\n"
            f"| Severity | ID | Package | Published version | Fixed in |\n"
            f"|---|---|---|---|---|\n{rows}\n\n"
            f"Filed by scripts/scan_published.py in the vuln-scan "
            f"workflow. This issue updates on each weekly run and "
            f"closes when a scan comes back clean.")
    if existing:
        gh("issue", "comment", existing, "--repo", repo, "--body", body)
        print(f"updated issue #{existing}")
    else:
        gh("issue", "create", "--repo", repo,
           "--title", f"{ISSUE_TITLE_PREFIX} findings in published "
                      f"packages", "--body", body)
        print("created vulnerability scan issue")


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "collect":
        collect(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "report":
        report(sys.argv[2], "--file-issue" in sys.argv[3:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
