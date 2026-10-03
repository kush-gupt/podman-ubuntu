#!/usr/bin/env python3
"""Build the orchestrate job matrices from the watermark files.

Validates that every tracked podman version has a full stack pin in
packaging/components.json (fails the workflow otherwise), then emits
level0 / level1 include-lists and the suite list as GITHUB_OUTPUT.
Skips (component, version) pairs already recorded in manifest.json
unless FORCE_REBUILD=true. Stdlib only.
"""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STACK_COMPONENTS = ["crun", "conmon", "netavark", "aardvark-dns", "passt",
                    "containers-common"]
LEVEL1_COMPONENTS = ["podman", "podman-suite"]
ARCHES = ["amd64", "arm64"]


def load(name):
    with open(os.path.join(REPO_ROOT, name)) as f:
        return json.load(f)


def main():
    versions = load("versions.json")
    components = load("packaging/components.json")
    ubuntu = load("ubuntu-versions.json")["releases"]
    manifest = load("manifest.json").get("built", {})
    force = os.environ.get("FORCE_REBUILD", "false").lower() == "true"

    # Validate pins before emitting anything.
    for track, pver in versions.items():
        if pver not in components:
            print(f"ERROR: podman {pver} (track {track}) has no stack pin "
                  f"in packaging/components.json", file=sys.stderr)
            sys.exit(1)

    def already_built(component, version):
        return not force and manifest.get(component, {}).get(version, False)

    level0, level1 = [], []
    for track, pver in sorted(versions.items()):
        stack = components[pver]
        for rel in ubuntu:
            for arch in ARCHES:
                for comp in STACK_COMPONENTS:
                    cver = stack[comp]
                    if not already_built(comp, cver):
                        level0.append({
                            "component": comp, "version": cver,
                            "ubuntu_version": rel, "arch": arch,
                            "track": track,
                        })
                for comp in LEVEL1_COMPONENTS:
                    if not already_built(comp, pver):
                        level1.append({
                            "component": comp, "version": pver,
                            "ubuntu_version": rel, "arch": arch,
                            "track": track,
                        })

    suites = {"ubuntu": ubuntu, "tracks": sorted(versions.keys())}
    outputs = {"level0": level0, "level1": level1, "suites": suites}

    # Single-output mode: `build_matrix.py <name>` prints ONLY that output's
    # JSON to stdout (used with command substitution in workflow steps).
    if len(sys.argv) > 1:
        name = sys.argv[1]
        if name not in outputs:
            print(f"ERROR: unknown output {name!r}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(outputs[name]))
        return 0

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"level0={json.dumps(level0)}\n")
            f.write(f"level1={json.dumps(level1)}\n")
            f.write(f"suites={json.dumps(suites)}\n")
    print(f"level0 cells: {len(level0)}, level1 cells: {len(level1)}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
