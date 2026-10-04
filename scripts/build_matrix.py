#!/usr/bin/env python3
"""Build the orchestrate job matrices from the watermark files.

Validates that every tracked podman version has a full stack pin in
packaging/components.json and that every component declares its build
metadata in packaging/<component>/meta.json (fails the workflow otherwise),
then emits the level0 / level1 include-lists and the suite list as
GITHUB_OUTPUT. Skips (component, version) pairs already published with
identical packaging inputs (lib.is_built) unless FORCE_REBUILD=true.

`build_matrix.py --suites` prints just {"ubuntu": [...], "tracks": [...]},
for workflows that only need the suite list (builder.yml). Stdlib only.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib


def resolve_version(comp, m, stack):
    vfrom = m.get("version_from", "self")
    if vfrom == "podman":
        return stack["podman"]
    try:
        return stack[comp]
    except KeyError:
        raise ValueError(
            f"component {comp!r} has packaging/<comp>/meta.json but no pin "
            f"in packaging/components.json")


def main():
    if sys.argv[1:] == ["--suites"]:
        print(json.dumps(lib.suites()))
        return 0
    if len(sys.argv) > 1:
        print(f"ERROR: unknown arguments {sys.argv[1:]}", file=sys.stderr)
        return 1

    vers = lib.versions()
    pins = lib.stack_pins()
    ubuntu = lib.ubuntu_releases()
    meta = lib.component_meta()
    manifest = lib.load_manifest()
    force = os.environ.get("FORCE_REBUILD", "false").lower() == "true"

    # Validate pins and metadata before emitting anything.
    for track, pver in vers.items():
        if pver not in pins:
            print(f"ERROR: podman {pver} (track {track}) has no stack pin "
                  f"in packaging/components.json", file=sys.stderr)
            return 1

    levels = {"level0": [], "level1": []}
    for track, pver in sorted(vers.items()):
        stack = pins[pver]
        for comp, m in sorted(meta.items()):
            cver = resolve_version(comp, m, stack)
            tier = m["tier"]
            for rel in ubuntu:
                for arch in lib.ARCHES:
                    if m.get("arch") == "all" and arch != "amd64":
                        # Architecture-independent package: building once is
                        # enough. Emitting both cells makes reprepro see two
                        # same-named debs with different bytes (pool collision).
                        continue
                    if not force and lib.is_built(comp, cver, manifest):
                        continue
                    cell = {
                        "component": comp, "version": cver,
                        "ubuntu_version": rel, "arch": arch,
                        "track": track,
                    }
                    if m.get("extra_from"):
                        cell["extra"] = stack.get(m["extra_from"], "")
                    levels[f"level{tier}"].append(cell)

    outputs = {"level0": levels["level0"], "level1": levels["level1"],
               "suites": lib.suites()}

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            for name, value in outputs.items():
                f.write(f"{name}={json.dumps(value)}\n")
    print(f"level0 cells: {len(levels['level0'])}, "
          f"level1 cells: {len(levels['level1'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
