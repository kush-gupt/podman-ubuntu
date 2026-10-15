#!/usr/bin/env python3
"""Assemble the signed apt repository from built .debs and stage it for Pages.

Env: APT_SIGNING_SUBKEY  base64 of the passphrase-less signing subkey (required)
     TRACKS              comma-separated tracks to publish (default: all in versions.json)

Artifact dirs carry a meta.json sidecar (written by build.yml); grouping and
suite assignment read it instead of parsing directory names.

Steps: gather -> reprepro includedeb per suite -> prune to newest 3 ->
       sign -> record manifest.json on the apt-repo branch -> commit+push
       apt-repo branch -> stage apt-repo-publish/. Stdlib only.
"""
import base64
import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from functools import cmp_to_key

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

REPO_ROOT = lib.REPO_ROOT
os.chdir(REPO_ROOT)

BOT = ["-c", "user.name=podman-ubuntu-bot",
       "-c", "user.email=podman-ubuntu-bot@users.noreply.github.com"]


def run(*args, check=True, **kwargs):
    kwargs.setdefault("cwd", REPO_ROOT)
    return subprocess.run(args, check=check, **kwargs)


def git(*args, cwd=REPO_ROOT, check=True):
    return run("git", *args, cwd=cwd, check=check,
               capture_output=True, text=True)


def parse_listed(output):
    """Parse `reprepro list` output into {(version, architecture)}.

    Lines look like "stable-2404|main|amd64: crun 1.30.1-1~noble"
    (suite|component|architecture). The architecture token is the one
    reprepro recorded for the package (its own Architecture field),
    which is what a removefilter Architecture term must match."""
    entries = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "|" in parts[0]:
            arch = parts[0].rsplit("|", 1)[1].rstrip(":")
            entries.add((parts[2], arch))
    return entries


def deb_field(deb, field):
    return run("dpkg-deb", "-f", deb, field,
               capture_output=True, text=True).stdout.strip()


def listed_hits(base, suite, pkg, dver, darch):
    """Recorded architectures under which (pkg, dver) is listed in
    `suite`, restricted to entries a replacement of the (darch)
    build must remove. Matching is per architecture: a filter
    without an Architecture term removes EVERY architecture's
    entry, so replacing the amd64 build and then the arm64 build
    of the same version left only arm64 in the repo (the arm64
    pass deleted the just-included amd64 entry). Note reprepro
    lists an arch-all package once per suite architecture, so its
    recorded tokens are the binary arches, never "all"."""
    out = run("reprepro", "-b", base, "list", suite, pkg,
              capture_output=True, text=True).stdout
    return sorted({a for (v, a) in parse_listed(out)
                   if v == dver
                   and (a == darch or darch == "all" or a == "all")})


def replace_stale(base, suite, pkg, dver, darch):
    """Remove the stale (pkg, dver, darch) entry from `suite` so a
    fresh build of the same version can be included. Returns True
    when an entry was removed. Extracted from main() unchanged in
    behavior so the reprepro integration test can drive it.

    removefilter targets one exact version; plain `remove suite
    pkg=version` silently matches nothing (it parses the whole
    string as a package name). The Architecture term is load-bearing
    and must be the DEB's architecture: without it the filter
    removes every architecture's entry, and for arch-all packages
    the recorded tokens are the suite's binary arches, so filtering
    on a recorded token instead of "all" matches nothing."""
    if not listed_hits(base, suite, pkg, dver, darch):
        return False
    run("reprepro", "-b", base, "removefilter", suite,
        f"Package (== {pkg}), Version (== {dver}), "
        f"Architecture (== {darch})",
        capture_output=True, text=True)
    # removefilter returns success even when it matched nothing;
    # verify the entry is actually gone, or the includedeb after
    # this fails with a checksum error that hides the real cause.
    left = listed_hits(base, suite, pkg, dver, darch)
    if left:
        print(f"ERROR: stale {pkg} {dver} still listed in {suite} "
              f"under {left} after removefilter (arch {darch})",
              flush=True)
        sys.exit(1)
    return True


def main():
    try:
        subkey_b64 = os.environ["APT_SIGNING_SUBKEY"]
    except KeyError:
        sys.exit("ERROR: APT_SIGNING_SUBKEY is required")
    tracks = os.environ.get("TRACKS",
                            ",".join(lib.versions().keys())).split(",")
    ubuntus = lib.ubuntu_releases()
    flat = lambda v: v.replace(".", "")

    # --- apt-repo branch worktree --------------------------------------
    if git("show-ref", "--verify", "--quiet", "refs/heads/apt-repo",
           check=False).returncode == 0:
        run("git", "worktree", "add", "-f", "apt-repo", "apt-repo")
    elif "apt-repo" in git("ls-remote", "--heads", "origin",
                           "apt-repo").stdout:
        run("git", "fetch", "origin", "apt-repo:apt-repo")
        run("git", "worktree", "add", "-f", "apt-repo", "apt-repo")
    else:
        run("git", "worktree", "add", "--detach", "apt-repo")
        run("git", "checkout", "--orphan", "apt-repo", cwd="apt-repo")
        run("git", "rm", "-rf", ".", cwd="apt-repo", check=False,
            capture_output=True)
    aptd = os.path.join(REPO_ROOT, "apt-repo")
    os.makedirs(os.path.join(aptd, "conf"), exist_ok=True)
    os.makedirs(os.path.join(aptd, "pool"), exist_ok=True)
    os.makedirs(os.path.join(aptd, "keys"), exist_ok=True)

    # --- GPG -------------------------------------------------------------
    # NOTE: the keyring lives outside $APTD on purpose: $APTD is committed to
    # the apt-repo branch, and a previous revision of this script committed
    # the passphrase-less secret subkey to that public branch. Never put
    # GNUPGHOME back under $APTD.
    gnupghome = os.path.join(REPO_ROOT, ".gnupg-publish")
    os.makedirs(gnupghome, mode=0o700, exist_ok=True)
    os.chmod(gnupghome, 0o700)
    os.environ["GNUPGHOME"] = gnupghome
    run("gpg", "--batch", "--import",
        input=base64.b64decode(subkey_b64), capture_output=True)
    cols = run("gpg", "--batch", "--list-keys", "--with-colons",
               capture_output=True, text=True).stdout
    fpr = next(l.split(":")[9] for l in cols.splitlines()
               if l.startswith("fpr:"))
    run("gpg", "--batch", "--import-ownertrust",
        input=f"{fpr}:6:\n".encode(), capture_output=True)
    keyid = next(l.split(":")[4] for l in cols.splitlines()
                 if l.startswith("sub:"))

    # --- conf/distributions ----------------------------------------------
    # Additive: keep suites other tracks published (a nightly-only run must
    # not strip stable/v5 from the shared apt-repo branch); (over)write the
    # stanzas for this run's tracks.
    def stanza(track, uv):
        suite = f"{track}-{flat(uv)}"
        return (
            f"Origin: podman-ubuntu\n"
            f"Label: podman-ubuntu\n"
            f"Codename: {suite}\n"
            f"Suite: {suite}\n"
            f"Architectures: amd64 arm64\n"
            f"Components: main\n"
            f"Description: podman-ubuntu {track} track for Ubuntu {uv}\n"
            f"SignWith: {keyid}\n")

    dist_path = os.path.join(aptd, "conf", "distributions")
    existing = {}
    if os.path.exists(dist_path):
        # Split into stanzas on blank lines, keyed by Codename.
        cur, codename = [], None
        for line in open(dist_path).read().splitlines() + [""]:
            if not line.strip():
                if cur and codename:
                    existing[codename] = "\n".join(cur) + "\n"
                cur, codename = [], None
                continue
            cur.append(line)
            if line.startswith("Codename:"):
                codename = line.split(":", 1)[1].strip()
        if cur and codename:
            existing[codename] = "\n".join(cur) + "\n"
    for track in tracks:
        for uv in ubuntus:
            existing[f"{track}-{flat(uv)}"] = stanza(track, uv)
    # End-of-life cleanup: drop stanzas for Ubuntu releases that left
    # ubuntu-versions.json, for every track (the additive merge above
    # would otherwise keep publishing them forever with stale packages).
    # clearvanished below purges their db entries and dists/ files.
    keep = set(lib.kept_codenames(list(existing), lib.ubuntu_releases()))
    for codename in sorted(set(existing) - keep):
        print(f"dropping end-of-life suite {codename}", flush=True)
        del existing[codename]
    with open(dist_path, "w") as f:
        f.write("\n".join(existing[c] for c in sorted(existing)) + "\n")
    with open(os.path.join(aptd, "conf", "options"), "w") as f:
        f.write("verbose\n")
    # Drop db entries for suites no longer in conf/distributions (e.g. after
    # a suite rename); otherwise includedeb fails with "unused database".
    cv = run("reprepro", "-b", aptd, "clearvanished", capture_output=True,
             text=True, check=False)
    print(cv.stdout, flush=True)
    print(cv.stderr, flush=True)
    # Pool files of vanished suites are unreferenced now; drop them too.
    run("reprepro", "-b", aptd, "deleteunreferenced",
        capture_output=True, text=True, check=False)

    # --- include debs ------------------------------------------------------
    # Artifacts download to debs/debs-*/ (actions/download-artifact nests each
    # artifact under its own directory). Fail loudly when nothing matches:
    # silently publishing an empty repo is how the apt-repo branch once
    # shipped zero packages.
    #
    # Group by (component, version, ubuntu, arch): the same component version
    # pinned in two tracks (e.g. passt in v5 and stable) builds twice with
    # identical inputs, producing byte-different debs with the same
    # (name, version, arch). reprepro's pool is shared across suites and can
    # only hold one file per pool path, so include a single copy into every
    # track-suite in the group.
    artifacts = list(lib.iter_artifacts("debs"))
    if not artifacts:
        sys.exit("ERROR: no debs/debs-*/ artifact dirs found; "
                 "refusing to publish an empty repo")
    groups = {}
    for d, m in artifacts:
        key = (m["component"], m["version"], m["ubuntu"], m["arch"])
        g = groups.setdefault(key, {"dir": d, "tracks": set()})
        g["tracks"].add(m["track"])

    for (comp, ver, ubuntu, arch) in sorted(groups):
        g = groups[(comp, ver, ubuntu, arch)]
        debs = sorted(glob.glob(os.path.join(g["dir"], "*.deb")))
        # A rebuild of an already-published (package, version) produces
        # different bytes (the builds are not reproducible), and reprepro
        # refuses to include a pool file whose checksums differ from the
        # copy already published ("Already existing files can only be
        # included again, if they are the same"). That is exactly what a
        # packaging-only fix triggers: same version, new bytes. Remove the
        # stale entry from every suite this run publishes and drop the
        # unreferenced pool file, so the fresh build can land.
        run_suites = [f"{t}-{flat(ubuntu)}" for t in tracks]
        for deb in debs:
            pkg = deb_field(deb, "Package")
            dver = deb_field(deb, "Version")
            darch = deb_field(deb, "Architecture")
            removed = False
            for suite in run_suites:
                if replace_stale(aptd, suite, pkg, dver, darch):
                    removed = True
            if removed:
                run("reprepro", "-b", aptd, "deleteunreferenced",
                    capture_output=True, text=True)
                print(f"replaced stale {pkg} {dver} (Ubuntu {ubuntu}, "
                      f"{arch})", flush=True)
        for track in sorted(g["tracks"]):
            suite = f"{track}-{flat(ubuntu)}"
            for deb in debs:
                r = run("reprepro", "-b", aptd, "includedeb", suite, deb,
                        capture_output=True, text=True, check=False)
                if r.returncode != 0:
                    print(f"includedeb {suite} {deb} failed:",
                          flush=True)
                    print(r.stdout, flush=True)
                    print(r.stderr, flush=True)
                    sys.exit(1)
        print(f"included {comp} {ver} (tracks:{sorted(g['tracks'])}, "
              f"Ubuntu {ubuntu}, {arch})", flush=True)

    # --- prune: keep newest 3 versions per package per suite -----------------
    # Ordering uses dpkg's own version comparison (~ sorts below the bare
    # revision, so 6.1.2-1~noble is older than 6.1.2-1).
    def deb_cmp(a, b):
        for op, result in (("lt", -1), ("gt", 1)):
            if run("dpkg", "--compare-versions", a, op, b,
                   check=False, capture_output=True).returncode == 0:
                return result
        return 0

    for track in tracks:
        for uv in ubuntus:
            suite = f"{track}-{flat(uv)}"
            pkgs = sorted(set(
                l.split()[1] for l in
                run("reprepro", "-b", aptd, "list", suite,
                    capture_output=True, text=True).stdout.splitlines()))
            for pkg in pkgs:
                vers = sorted(
                    (l.split()[2] for l in
                     run("reprepro", "-b", aptd, "list", suite, pkg,
                         capture_output=True, text=True).stdout.splitlines()),
                    key=cmp_to_key(deb_cmp))
                for old in vers[:-3]:
                    # removefilter, not `remove pkg=version`: the latter
                    # silently matches nothing, so pruning never actually
                    # removed anything.
                    run("reprepro", "-b", aptd, "removefilter", suite,
                        f"Package (== {pkg}), Version (== {old})",
                        check=False, capture_output=True)
                    print(f"pruned {pkg} {old} from {suite}", flush=True)

    # --- sign + publish key ---------------------------------------------------
    run("reprepro", "-b", aptd, "export", capture_output=True)
    shutil.copy("keys/apt-signing.asc",
                os.path.join(aptd, "keys", "apt-signing.asc"))

    # --- record the manifest on the apt-repo branch -------------------------
    # Records (component, version) -> packaging_sha so future builds skip
    # only when both the upstream version AND the packaging inputs are
    # unchanged. (A packaging-only fix like a debian/rules change must
    # trigger a rebuild; a version-only key would silently ship stale
    # debs.) The manifest is machine state: it lives on the apt-repo
    # branch and lands atomically with the debs it describes. It used to
    # be committed to main by this job, but required status checks on
    # main reject the bot's direct push, and the extra commits raced
    # human pushes besides.
    mpath = os.path.join(aptd, "manifest.json")
    if os.path.exists(mpath):
        with open(mpath) as f:
            manifest = json.load(f)
    else:  # migration seed: main's last hand-off copy
        with open("manifest.json") as f:
            manifest = json.load(f)
    built = manifest.setdefault("built", {})
    for _d, m in artifacts:
        built.setdefault(m["component"], {})[m["version"]] = {
            "packaging_sha": lib.packaging_sha(m["component"]),
        }
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    run("git", "add", "-A", cwd=aptd)
    run("git", *BOT, "commit", "-m",
        f"apt-repo: publish {datetime.now(timezone.utc):%Y%m%d-%H%M}",
        cwd=aptd, check=False, capture_output=True)
    run("git", "push", "origin", "apt-repo", cwd=aptd)

    # --- stage for Pages (exclude git metadata) -------------------------------
    stage = os.path.join(REPO_ROOT, "apt-repo-publish")
    shutil.rmtree(stage, ignore_errors=True)
    run("rsync", "-a", "--delete",
        "--exclude", ".git", "--exclude", ".gnupg",
        f"{aptd}/", f"{stage}/")

    run("git", "worktree", "remove", "--force", "apt-repo")

    print("Publish complete.")


if __name__ == "__main__":
    main()
