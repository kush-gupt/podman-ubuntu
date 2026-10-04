#!/usr/bin/env python3
"""Assemble the signed apt repository from built .debs and stage it for Pages.

Env: APT_SIGNING_SUBKEY  base64 of the passphrase-less signing subkey (required)
     TRACKS              comma-separated tracks to publish (default: all in versions.json)

Artifact dirs carry a meta.json sidecar (written by build.yml); grouping and
suite assignment read it instead of parsing directory names.

Steps: gather -> reprepro includedeb per suite -> prune to newest 3 ->
       sign -> commit+push apt-repo branch -> stage apt-repo-publish/ ->
       record manifest.json on main. Stdlib only.
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
    dists = []
    for track in tracks:
        for uv in ubuntus:
            suite = f"{track}-{flat(uv)}"
            dists.append(
                f"Origin: podman-ubuntu\n"
                f"Label: podman-ubuntu\n"
                f"Codename: {suite}\n"
                f"Suite: {suite}\n"
                f"Architectures: amd64 arm64\n"
                f"Components: main\n"
                f"Description: podman-ubuntu {track} track for Ubuntu {uv}\n"
                f"SignWith: {keyid}\n")
    with open(os.path.join(aptd, "conf", "distributions"), "w") as f:
        f.write("\n".join(dists) + "\n")
    with open(os.path.join(aptd, "conf", "options"), "w") as f:
        f.write("verbose\n")

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
        for track in sorted(g["tracks"]):
            suite = f"{track}-{flat(ubuntu)}"
            for deb in sorted(glob.glob(os.path.join(g["dir"], "*.deb"))):
                run("reprepro", "-b", aptd, "includedeb", suite, deb,
                    capture_output=True)
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
                    run("reprepro", "-b", aptd, "remove", suite,
                        f"{pkg}={old}", check=False, capture_output=True)
                    print(f"pruned {pkg} {old} from {suite}", flush=True)

    # --- sign + publish key ---------------------------------------------------
    run("reprepro", "-b", aptd, "export", capture_output=True)
    shutil.copy("keys/apt-signing.asc",
                os.path.join(aptd, "keys", "apt-signing.asc"))

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

    # --- record manifest on main --------------------------------------------------
    # Records (component, version) -> packaging_sha so future builds skip only
    # when both the upstream version AND the packaging inputs are unchanged.
    # (A packaging-only fix like a debian/rules change must trigger a rebuild;
    # a version-only key would silently ship stale debs.)
    with open("manifest.json") as f:
        manifest = json.load(f)
    built = manifest.setdefault("built", {})
    for _d, m in artifacts:
        built.setdefault(m["component"], {})[m["version"]] = {
            "packaging_sha": lib.packaging_sha(m["component"]),
        }
    with open("manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    run("git", "add", "manifest.json")
    run("git", *BOT, "commit", "-m",
        "chore: record published builds in manifest.json",
        check=False, capture_output=True)
    run("git", "push", "origin", "main")
    run("git", "worktree", "remove", "--force", "apt-repo")

    print("Publish complete.")


if __name__ == "__main__":
    main()
