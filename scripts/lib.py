#!/usr/bin/env python3
"""Shared helpers for the podman-ubuntu build scripts. Stdlib only.

Single source of truth for: loading the JSON watermark files, computing the
packaging input hash, deciding whether a (component, version) needs building,
per-component build metadata, and reading artifact meta.json sidecars.
"""
import functools
import glob
import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHES = ["amd64", "arm64"]
VALID_TIERS = (0, 1)


def load_json(name):
    with open(os.path.join(REPO_ROOT, name)) as f:
        return json.load(f)


def versions():
    """track -> podman version."""
    return load_json("versions.json")


def stack_pins():
    """podman version -> {component: version, ...}."""
    return load_json("packaging/components.json")


def ubuntu_releases():
    return load_json("ubuntu-versions.json")["releases"]


def suites():
    v = versions()
    return {"ubuntu": ubuntu_releases(), "tracks": sorted(v.keys())}


def load_manifest():
    return load_json("manifest.json").get("built", {})


def _tree_sha(path):
    # HEAD: tree objects, so the hash is deterministic for a given commit.
    # (CI always runs on a checked-out commit; run on committed state.)
    try:
        return subprocess.run(
            ["git", "rev-parse", f"HEAD:{path}"],
            capture_output=True, text=True, check=True,
            cwd=REPO_ROOT).stdout.strip()
    except subprocess.CalledProcessError:
        raise ValueError(
            f"cannot hash packaging inputs: {path!r} is not committed; "
            f"commit your changes first")


@functools.lru_cache(maxsize=None)
def packaging_sha(component):
    """Hash of everything that affects the built debs for one component: its
    own packaging dir, the shared packaging/common snippets every debian/rules
    includes, and the source preparation script."""
    return "".join(_tree_sha(p)[:12] for p in (
        f"packaging/{component}",
        "packaging/common",
        "scripts/prepare-source.sh",
    ))


def is_built(component, version, manifest=None):
    """True when (component, version) is recorded in the manifest with the
    same packaging inputs. A packaging-only fix must trigger a rebuild even
    when the upstream version is unchanged, so the sha comparison (not the
    version key alone) is the predicate. Old-style `true` entries and missing
    entries always need building once, then get re-recorded with a sha."""
    manifest = manifest if manifest is not None else load_manifest()
    entry = manifest.get(component, {}).get(version)
    return (isinstance(entry, dict)
            and entry.get("packaging_sha") == packaging_sha(component))


def component_meta():
    """Per-component build metadata from packaging/<component>/meta.json.

    Fields (all optional except tier):
      tier:         0 = base stack, 1 = built on top of the stack.
      arch:         "any" (default) or "all" (architecture-independent:
                    build once on amd64).
      version_from: "self" (default, version comes from the stack pin) or
                    "podman" (version tracks the podman version, for the
                    podman-suite meta-package).
      extra_from:   stack key whose value feeds the build's `extra` input
                    (e.g. passt's snapshot sha).
    """
    meta = {}
    for path in sorted(glob.glob(os.path.join(REPO_ROOT, "packaging", "*", "meta.json"))):
        comp = os.path.basename(os.path.dirname(path))
        with open(path) as f:
            m = json.load(f)
        if m.get("tier") not in VALID_TIERS:
            raise ValueError(f"{comp}/meta.json: tier must be one of {VALID_TIERS}")
        if m.get("arch", "any") not in ("any", "all"):
            raise ValueError(f"{comp}/meta.json: arch must be 'any' or 'all'")
        if m.get("version_from", "self") not in ("self", "podman"):
            raise ValueError(f"{comp}/meta.json: version_from must be 'self' or 'podman'")
        meta[comp] = m
    return meta


def read_deb_meta(path):
    """Read one artifact's meta.json sidecar (written by build.yml)."""
    with open(path) as f:
        return json.load(f)


def iter_artifacts(debs_dir="debs"):
    """Yield (artifact_dir, meta) for every downloaded artifact that carries
    a meta.json sidecar."""
    for mp in sorted(glob.glob(os.path.join(debs_dir, "debs-*", "meta.json"))):
        yield os.path.dirname(mp), read_deb_meta(mp)
