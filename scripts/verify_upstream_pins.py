#!/usr/bin/env python3
"""Verify the pinned component stacks against upstream.

For every track in versions.json, using that podman version's stack from
packaging/components.json:

  * the `go` directive of podman's go.mod at the pinned tag must not
    require a Go newer than the builder image's GO_VERSION (the
    toolchain that actually compiles podman) or than the stack's `go`
    pin (the recorded toolchain for the stack; a stale pin is how
    toolchain drift hides);
  * every pinned component version must exist as a tag in its upstream
    repository (the same repositories prepare-source.sh downloads from;
    tag formats verified against the live repos).

Network checks (git ls-remote + one raw go.mod fetch per track), so this
runs as its own CI job on PRs (script-tests.yml `upstream-pins`), not in
the offline unit tests. Stdlib only.
"""
import os
import re
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

# component -> (github repo, tag template). Mirrors prepare-source.sh.
UPSTREAM = {
    "podman": ("containers/podman", "v{v}"),
    "crun": ("containers/crun", "{v}"),
    "conmon": ("containers/conmon", "v{v}"),
    "netavark": ("containers/netavark", "v{v}"),
    "aardvark-dns": ("containers/aardvark-dns", "v{v}"),
    "containers-common": ("podman-container-tools/container-libs",
                          "common/v{v}"),
    "go": ("golang/go", "go{v}"),
    "rust": ("rust-lang/rust", "{v}"),
}
# passt is pinned by snapshot sha, podman-suite is a local metapackage.
SKIP = {"passt", "passt_sha", "podman-suite"}


def parse_go_directive(go_mod):
    """'go 1.25' directive of a go.mod -> (major, minor) or None."""
    m = re.search(r"^go\s+(\d+)\.(\d+)", go_mod, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else None


def go_pin_ok(pin, required):
    """True when the toolchain pin (e.g. '1.26.1') satisfies the go.mod
    requirement (major, minor). Go modules need a toolchain at least as
    new as their `go` directive."""
    parts = pin.split(".")
    return (int(parts[0]), int(parts[1])) >= required


def tag_exists(repo, tag):
    out = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{repo}",
         f"refs/tags/{tag}"],
        capture_output=True, text=True)
    return bool(out.stdout.strip())


def fetch(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode()


def image_go_version():
    """GO_VERSION build arg of the builder image: the Go toolchain that
    actually compiles every component."""
    with open(os.path.join(lib.REPO_ROOT, "images",
                           "builder.Containerfile")) as f:
        m = re.search(r"^ARG GO_VERSION=(\S+)", f.read(), re.M)
    return m.group(1) if m else None


def main():
    errors = []
    image_go = image_go_version()
    if image_go is None:
        errors.append("cannot parse GO_VERSION from "
                      "images/builder.Containerfile")
    for track, pv in sorted(lib.versions().items()):
        stack = lib.stack_pins()[pv]
        try:
            go_mod = fetch("https://raw.githubusercontent.com/"
                           f"containers/podman/v{pv}/go.mod")
            required = parse_go_directive(go_mod)
        except Exception as e:
            errors.append(f"{track}: cannot fetch podman v{pv} go.mod: {e}")
            continue
        if required is None:
            errors.append(f"{track}: no go directive in podman v{pv} go.mod")
        else:
            req = f"{required[0]}.{required[1]}"
            if image_go and not go_pin_ok(image_go, required):
                errors.append(
                    f"{track}: podman {pv} requires Go {req}, builder "
                    f"image GO_VERSION is {image_go}")
            if not go_pin_ok(stack["go"], required):
                errors.append(
                    f"{track}: podman {pv} requires Go {req}, stack pins "
                    f"go {stack['go']} (stale pin; the builder image "
                    "currently masks this)")
        for comp, ver in sorted(stack.items()):
            if comp in SKIP:
                continue
            repo, tmpl = UPSTREAM[comp]
            tag = tmpl.format(v=ver)
            if not tag_exists(repo, tag):
                errors.append(
                    f"{track}: {comp} {ver}: tag {tag} not found in {repo}")
        print(f"{track}: podman {pv} stack checked "
              f"({len(stack)} components)")
    if errors:
        print("\nUPSTREAM PIN PROBLEMS:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print("all pinned stacks verified against upstream")


if __name__ == "__main__":
    main()
