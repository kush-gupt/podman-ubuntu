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
import re
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


_SUITE_ARM = re.compile(r"^\s*(\d{2}\.\d{2})\)\s*SUITE=([a-z]+)\s*;;")


def suite_codenames(text=None):
    """Ubuntu release -> suite codename (e.g. "24.04" -> "noble"), parsed
    from prepare-source.sh's case statement. That case statement is the
    build's single source of truth for the map: prepare-source.sh stamps
    the codename into every deb version and exits 1 on a release it has
    no arm for, so this parser exists to check the map, never to
    replace it."""
    if text is None:
        with open(os.path.join(REPO_ROOT, "scripts",
                               "prepare-source.sh")) as f:
            text = f.read()
    out = {}
    for line in text.splitlines():
        m = _SUITE_ARM.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def releases_without_codename(releases, codenames=None):
    """Releases from ubuntu-versions.json that prepare-source.sh cannot
    build for. A watch PR that adds a release without seeding its
    codename arm merges green and then every build cell for the new
    release exits 1; build_matrix.py fails on this list instead."""
    if codenames is None:
        codenames = suite_codenames()
    return [r for r in releases if r not in codenames]


def suites():
    v = versions()
    return {"ubuntu": ubuntu_releases(), "tracks": sorted(v.keys())}


def kept_codenames(codenames, releases):
    """Filter suite codenames (<track>-<YYMM>) to those whose Ubuntu
    release is still supported. Suites for end-of-life Ubuntu releases
    are dropped at publish time; reprepro's clearvanished then purges
    their database entries and dists/ files. Codenames that don't parse
    as <track>-<YYMM> are kept: never delete what we don't understand."""
    keep = {r.replace(".", "") for r in releases}
    out = []
    for c in codenames:
        tail = c.rsplit("-", 1)[-1]
        if len(tail) == 4 and tail.isdigit() and tail not in keep:
            continue
        out.append(c)
    return out


MANIFEST_URL = os.environ.get(
    "MANIFEST_URL",
    "https://raw.githubusercontent.com/kush-gupt/podman-ubuntu/"
    "apt-repo/manifest.json")
_MANIFEST_CACHE = None


def load_manifest():
    """Published-builds manifest: (component, version) -> packaging_sha.

    The manifest is machine state, not source: publish_repo.py records it
    on the apt-repo branch, atomically with the debs it describes, and
    build_matrix reads it from there. main's manifest.json is only the
    migration seed / offline fallback. A fetch failure falls back to the
    local file, then to empty; a stale or empty manifest only ever causes
    extra rebuilds, never wrong skips, because is_built also requires
    the packaging_sha to match. Set MANIFEST_URL="" to disable fetching.
    """
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is not None:
        return _MANIFEST_CACHE
    built = {}
    if MANIFEST_URL:
        try:
            import urllib.request
            with urllib.request.urlopen(MANIFEST_URL, timeout=10) as r:
                built = json.load(r).get("built", {})
        except Exception:
            built = {}
    if not built:
        try:
            built = load_json("manifest.json").get("built", {})
        except Exception:
            built = {}
    _MANIFEST_CACHE = built
    return built


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
