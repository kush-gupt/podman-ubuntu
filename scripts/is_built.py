#!/usr/bin/env python3
"""Exit 0 if (component, version) is already recorded as published.

Used by build.yml to skip matrix cells that need no work.
"""
import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--component", required=True)
    ap.add_argument("--version", required=True)
    args = ap.parse_args()

    with open(os.path.join(REPO_ROOT, "manifest.json")) as f:
        built = json.load(f).get("built", {})

    if built.get(args.component, {}).get(args.version):
        print(f"{args.component} {args.version} already published, skipping.")
        return 0
    print(f"{args.component} {args.version} needs building.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
