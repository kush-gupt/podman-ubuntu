#!/usr/bin/env python3
"""Exit 0 if (component, version) is already recorded as published *with the
same packaging inputs*.

The manifest maps (component, version) -> {"packaging_sha": ...}; see
lib.is_built for the predicate. A packaging-only fix (debian/rules,
debian/control, ...) must trigger a rebuild even when the upstream version
is unchanged; a version-only key would silently ship stale debs.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--component", required=True)
    ap.add_argument("--version", required=True)
    args = ap.parse_args()

    if lib.is_built(args.component, args.version):
        print(f"{args.component} {args.version} already published with "
              f"current packaging, skipping.")
        return 0
    print(f"{args.component} {args.version} needs building.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
