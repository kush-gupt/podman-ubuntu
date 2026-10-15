#!/usr/bin/env python3
"""Check the apt signing subkey's expiry and file an issue before it bites.

The CI signing subkey (keys/apt-signing.asc, see docs/SIGNING.md) has a
1-year expiry and rotation is manual. Nothing else in the pipeline
looks at the date: when it lapses, reprepro export starts failing and
every publish breaks at once. This check runs in the repo-health
workflow (daily), parses the subkey expiry with gpg, and opens or
updates a tracking issue once the subkey is within 60 days of expiry,
escalating the wording inside 30 days. The issue auto-closes here
once the key is rotated (expiry back above the threshold), following
the same search-by-title convention as ci-failure-alert.

Usage:
    check_key_expiry.py [--key keys/apt-signing.asc] [--file-issue]
                        [--now EPOCH]   (testing aid)

Without --file-issue it only reports (exit 0 always; expiry state is
reported on stdout, it is not a failure of the repository itself).
Stdlib only; gpg required for parsing.
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WARN_DAYS = 60
ESCALATE_DAYS = 30
ISSUE_TITLE_PREFIX = "Signing key expiry:"


def parse_subkey_expiry(colon_output):
    """Parse `gpg --show-keys --with-colons` output.

    Returns (fingerprint, expiry_epoch) of the signing subkey: the
    `sub` record with 's' in its capability field (field 12), paired
    with the `fpr` record that follows it. Field 7 of sub is the
    expiry epoch (empty = never expires, returned as None)."""
    fpr = None
    pending = False
    for line in colon_output.splitlines():
        fields = line.split(":")
        if fields[0] == "sub":
            caps = fields[11] if len(fields) > 11 else ""
            pending = "s" in caps
            expiry = fields[6] if len(fields) > 6 else ""
            pending_expiry = int(expiry) if expiry else None
        elif fields[0] == "fpr" and pending:
            fpr = fields[9]
            return fpr, pending_expiry
    return None, None


def read_key(path):
    out = subprocess.run(
        ["gpg", "--batch", "--show-keys", "--with-colons", path],
        capture_output=True, text=True, check=True)
    return parse_subkey_expiry(out.stdout)


def assess(days_left):
    """Map days-to-expiry to an alert level: ok / warn / escalate."""
    if days_left is None:
        return "ok"
    if days_left <= ESCALATE_DAYS:
        return "escalate"
    if days_left <= WARN_DAYS:
        return "warn"
    return "ok"


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          check=check)


def file_or_update_issue(fpr, expiry_iso, days_left, level):
    repo = os.environ.get("GITHUB_REPOSITORY", "kush-gupt/podman-ubuntu")
    title = f"{ISSUE_TITLE_PREFIX} CI signing subkey expires {expiry_iso}"
    existing = gh("issue", "list", "--repo", repo, "--state", "open",
                  "--search", ISSUE_TITLE_PREFIX, "--json", "number,title",
                  "--jq", ".[0].number // empty").stdout.strip()
    urgency = ("**Under 30 days.** Rotate now: docs/SIGNING.md."
               if level == "escalate" else
               "Rotation is a manual process: docs/SIGNING.md.")
    body = (f"The passphrase-less CI signing subkey `{fpr}` expires on "
            f"{expiry_iso} ({days_left} days from this check). When it "
            f"lapses, reprepro can no longer sign the apt repository and "
            f"every publish fails.\n\n{urgency}\n\n"
            f"Filed by scripts/check_key_expiry.py in the repo-health "
            f"workflow. This issue closes automatically once the rotated "
            f"key is in place.")
    if existing:
        gh("issue", "comment", existing, "--repo", repo, "--body",
           f"Still expiring {expiry_iso}: {days_left} days left "
           f"({level}).")
        print(f"updated issue #{existing}")
    else:
        gh("issue", "create", "--repo", repo, "--title", title,
           "--body", body)
        print(f"created issue: {title}")


def close_issue_if_open():
    repo = os.environ.get("GITHUB_REPOSITORY", "kush-gupt/podman-ubuntu")
    existing = gh("issue", "list", "--repo", repo, "--state", "open",
                  "--search", ISSUE_TITLE_PREFIX, "--json", "number",
                  "--jq", ".[0].number // empty").stdout.strip()
    if existing:
        gh("issue", "close", existing, "--repo", repo,
           "--comment", "Signing subkey rotated; expiry is back above "
                        "the alert threshold.")
        print(f"closed issue #{existing} (key rotated)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.path.join(REPO_ROOT, "keys",
                                                  "apt-signing.asc"))
    ap.add_argument("--file-issue", action="store_true")
    ap.add_argument("--now", type=int, default=None,
                    help="epoch seconds to treat as now (tests)")
    args = ap.parse_args()

    fpr, expiry = read_key(args.key)
    if fpr is None:
        sys.exit("ERROR: no signing subkey found in " + args.key)
    now = args.now or int(datetime.now(timezone.utc).timestamp())
    if expiry is None:
        print(f"KEY-EXPIRY: subkey {fpr} never expires")
        return
    expiry_iso = datetime.fromtimestamp(
        expiry, timezone.utc).strftime("%Y-%m-%d")
    days_left = (expiry - now) // 86400
    level = assess(days_left)
    print(f"KEY-EXPIRY: subkey {fpr} expires {expiry_iso} "
          f"({days_left} days left, level={level})")
    if not args.file_issue:
        return
    if level == "ok":
        close_issue_if_open()
    else:
        file_or_update_issue(fpr, expiry_iso, days_left, level)


if __name__ == "__main__":
    sys.exit(main())
