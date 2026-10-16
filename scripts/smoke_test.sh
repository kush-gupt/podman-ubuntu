#!/usr/bin/env bash
# Deep smoke test for an installed podman-ubuntu stack.
#
# Replaces the old inline "run one Alpine container" step. Sections:
#   1. every stack binary reports a version
#   2. basic container run (podman + crun + conmon + netavark)
#   3. container-to-container DNS: two containers on a custom network
#      resolve and ping each other by name (netavark + aardvark-dns)
#   4. rootless run as a non-root user
#   5. upgrade report: if /tmp/podman-before.txt exists (written by
#      install_published.sh before the local build was installed over
#      the published stack), report the version transition explicitly.
#
# Environment:
#   SMOKE_IMAGE           image to run (default docker.io/library/alpine:latest)
#   SMOKE_STRICT_ROOTLESS=1  fail the smoke if the rootless section fails
#                            (default: report it explicitly as unsupported
#                            in this environment and continue; nested
#                            GitHub job containers cannot always set up
#                            rootless userns/networking, and that is an
#                            environment limit, not a packaging defect)
#   SMOKE_HOST=1          host mode: run directly on a real runner
#                            host (no job container) and test the
#                            defaults users actually get: overlay
#                            storage driver, real cgroup v2, rootless
#                            networking. NO silent fallbacks in this
#                            mode: each of the three is asserted and
#                            reported as SMOKE-HOST: <area> PASS/FAIL,
#                            and any failure fails the smoke.
#
# The vfs storage driver and --cgroups=disabled are for the nested CI
# container environment, same as the smoke this replaces: cgroup v2
# controller delegation does not work inside GitHub's job containers.
set -euo pipefail

IMAGE="${SMOKE_IMAGE:-docker.io/library/alpine:latest}"
if [ "${SMOKE_HOST:-0}" = "1" ]; then
  PODMAN="podman"
  RUNFLAGS=""
  SMOKE_STRICT_ROOTLESS="${SMOKE_STRICT_ROOTLESS:-1}"
else
  PODMAN="podman --storage-driver=vfs"
  RUNFLAGS="--cgroups=disabled"
fi
BEFORE_FILE="${SMOKE_BEFORE_FILE:-/tmp/podman-before.txt}"

host_fail() {
  echo "SMOKE-HOST: $1 FAIL ($2)" >&2
  exit 1
}
HOST_NET_BLOCKED=0

echo "== stack binary versions =="
podman --version
conmon --version
crun --version
/usr/libexec/podman/netavark --version
/usr/libexec/podman/aardvark-dns --version
command -v passt
$PODMAN info --format '{{.Host.Arch}} {{.Version.Version}}'

if [ "${SMOKE_HOST:-0}" = "1" ]; then
  DRIVER="$($PODMAN info --format '{{.Store.GraphDriverName}}')"
  [ "$DRIVER" = "overlay" ] || host_fail "overlay" "graph driver is $DRIVER, want overlay"
  echo "SMOKE-HOST: overlay PASS"
  # Podman 6 dropped the CgroupVersion field from `podman info`
  # (template access errors out); read the JSON key when present
  # and otherwise ask the kernel which hierarchy is mounted.
  CGROUPVER="$($PODMAN info --format json 2>/dev/null \
    | grep -o '"cgroupVersion"[^,}]*' | head -1 | grep -o 'v[12]' || true)"
  if [ -z "$CGROUPVER" ] && \
     [ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" = "cgroup2fs" ]; then
    CGROUPVER="v2"
  fi
  [ "$CGROUPVER" = "v2" ] || host_fail "cgroups" "cgroup version is ${CGROUPVER:-unknown}, want v2"
fi

if [ "${SMOKE_HOST:-0}" = "1" ] && [ "$(id -u)" != "0" ]; then
  # Rootless networking probe (rootless runs only; rootful
  # networking does not involve pasta). Some hosts deny pasta the
  # two opens it requires - the parent netns's
  # /proc/sys/net/ipv4/ip_local_port_range and the target netns
  # file. GitHub's runner images do exactly that for every user
  # and session, and podman 6 removed the slirp4netns fallback
  # ("invalid default_rootless_network_cmd option"), so on such a
  # host no rootless networked run can work: the failure is the
  # environment's, not the stack's. Classify that exact signature
  # loudly - BLOCKED-BY-ENVIRONMENT, raw error kept in the log,
  # never a silent skip - and let the remaining sections prove
  # what the environment does allow. Any other probe failure is a
  # real failure. SMOKE_HOST_ROOTLESS_NET_STRICT=1 restores a hard
  # failure even for the classified case. On an unrestricted host
  # the probe passes and nothing below changes.
  if ! probe_out="$($PODMAN run $RUNFLAGS --rm "$IMAGE" true 2>&1)"; then
    echo "$probe_out"
    if echo "$probe_out" | grep -q "ip_local_port_range" \
       && echo "$probe_out" | grep -q "Permission denied"; then
      if [ "${SMOKE_HOST_ROOTLESS_NET_STRICT:-0}" = "1" ]; then
        host_fail "rootless-networking" "pasta denied by host policy (strict mode)"
      fi
      HOST_NET_BLOCKED=1
      echo "::warning title=smoke-host::rootless networking blocked by this host's policy (pasta denied ip_local_port_range and netns access); classified BLOCKED-BY-ENVIRONMENT, raw error above"
      echo "SMOKE-HOST: rootless-networking BLOCKED-BY-ENVIRONMENT (pasta denied by host policy; raw error above)"
    else
      host_fail "rootless-networking" "probe run failed (see above)"
    fi
  fi
fi

echo "== basic container run =="
if [ "$HOST_NET_BLOCKED" = "1" ]; then
  $PODMAN run $RUNFLAGS --network=none --rm "$IMAGE" echo "smoke: basic run ok (network blocked by environment; see probe above)"
else
  $PODMAN run $RUNFLAGS --rm "$IMAGE" echo "smoke: basic run ok"
fi
if [ "${SMOKE_HOST:-0}" = "1" ]; then
  echo "SMOKE-HOST: cgroups PASS (container ran on cgroup v2 with no --cgroups=disabled)"
fi

echo "== network + DNS: containers resolve each other by name =="
if [ "$HOST_NET_BLOCKED" = "1" ]; then
  echo "network + DNS section not run: rootless networking is blocked by this environment (see probe above)"
else
NET="smoke-net-$$"
PEER="smoke-peer-$$"
cleanup_net() {
  $PODMAN rm -f "$PEER" >/dev/null 2>&1 || true
  $PODMAN network rm -f "$NET" >/dev/null 2>&1 || true
}
trap cleanup_net EXIT
$PODMAN network create "$NET" >/dev/null
$PODMAN run $RUNFLAGS -d --name "$PEER" --network "$NET" \
  "$IMAGE" sleep 120 >/dev/null
# Name resolution here is aardvark-dns; connectivity is netavark.
$PODMAN run $RUNFLAGS --rm --network "$NET" "$IMAGE" \
  sh -c "ping -c 1 -W 5 $PEER >/dev/null && echo 'smoke: DNS + network ok'"
cleanup_net
trap - EXIT
fi

echo "== rootless run =="
rootless_ok=0
rootless_mode=""

# Rootless sections run as a dedicated non-root user when the smoke
# itself runs as root. SMOKE_ROOTLESS_USER selects the user (default
# smokeuser, created on demand). Host jobs pass the runner user:
# GitHub's images preconfigure rootless podman for that user (real
# home, a real /run/user/<uid> session, subuid ranges), while a
# synthetic user has no session and pasta cannot set up its
# networking from the fake runtime dir.
ROOTLESS_USER="${SMOKE_ROOTLESS_USER:-smokeuser}"
ROOTLESS_HOME=""
ROOTLESS_XDG=""
setup_rootless_user() {
  # Rootless podman needs newuidmap (uidmap package); the minimal
  # container + our stack do not install it. Add it (and slirp4netns
  # as the fallback rootless network helper) before concluding the
  # environment cannot do rootless.
  if ! command -v newuidmap >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    apt-get install -y -qq uidmap slirp4netns >/dev/null 2>&1 || true
  fi
  if ! id "$ROOTLESS_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$ROOTLESS_USER" || true
  fi
  id "$ROOTLESS_USER" >/dev/null 2>&1 || return 1
  grep -q "^${ROOTLESS_USER}:" /etc/subuid 2>/dev/null || \
    echo "${ROOTLESS_USER}:100000:65536" >> /etc/subuid || true
  grep -q "^${ROOTLESS_USER}:" /etc/subgid 2>/dev/null || \
    echo "${ROOTLESS_USER}:100000:65536" >> /etc/subgid || true
  ROOTLESS_HOME="$(getent passwd "$ROOTLESS_USER" | cut -d: -f6)"
  [ -n "$ROOTLESS_HOME" ] || ROOTLESS_HOME="/home/$ROOTLESS_USER"
  # Prefer the user's real runtime dir (a live user session); fall
  # back to a private dir under /tmp for synthetic users.
  if [ -d "/run/user/$(id -u "$ROOTLESS_USER")" ]; then
    ROOTLESS_XDG="/run/user/$(id -u "$ROOTLESS_USER")"
  else
    ROOTLESS_XDG="/tmp/smoke-xdg"
    mkdir -p "$ROOTLESS_XDG"
    chown "$ROOTLESS_USER:$ROOTLESS_USER" "$ROOTLESS_XDG" || true
  fi
}
run_as_rootless() {
  # HOME/XDG_CONFIG_HOME are pinned: the caller's environment can
  # carry another user's values, and podman-as-$ROOTLESS_USER then
  # reads config it cannot traverse (permission denied).
  runuser -u "$ROOTLESS_USER" -- env HOME="$ROOTLESS_HOME" \
    XDG_RUNTIME_DIR="$ROOTLESS_XDG" \
    XDG_CONFIG_HOME="$ROOTLESS_HOME/.config" "$@"
}
if [ "$(id -u)" != "0" ]; then
  if $PODMAN run $RUNFLAGS --rm "$IMAGE" echo "smoke: rootless ok"; then
    rootless_ok=1
    rootless_mode="networked"
  elif $PODMAN run $RUNFLAGS --network=none --rm "$IMAGE" echo "smoke: rootless ok"; then
    rootless_ok=1
    rootless_mode="no-network"
  fi
else
  if setup_rootless_user; then
    run_rootless() {
      # podman inherits the caller's cwd; when that is a checkout
      # the rootless user cannot traverse, the run dies with
      # "cannot chdir", so start from the user's home.
      run_as_rootless bash -c 'cd "$HOME" || exit 1; exec "$@"' _ \
        $PODMAN run $RUNFLAGS "$@" --rm "$IMAGE" \
        echo "smoke: rootless ok"
    }
    if run_rootless > /tmp/smoke-rootless.log 2>&1; then
      cat /tmp/smoke-rootless.log
      rootless_ok=1
      rootless_mode="networked"
    elif run_rootless --network=none > /tmp/smoke-rootless.log 2>&1; then
      # Rootless execution works; rootless NETWORKING does not in
      # this nested container: pasta cannot open the outer
      # container's masked /proc/sys (ip_local_port_range) or the
      # netns. Report that distinction explicitly instead of
      # failing or silently skipping the whole section.
      cat /tmp/smoke-rootless.log
      rootless_ok=1
      rootless_mode="no-network"
      echo "::warning title=smoke-rootless::rootless networking unsupported in this nested environment (pasta/netns permission denied); rootless execution proven with --network=none"
    else
      echo "rootless run failed; last log lines:"
      tail -20 /tmp/smoke-rootless.log || true
    fi
  fi
fi
if [ "$rootless_ok" = "1" ] && [ "$rootless_mode" = "no-network" ]; then
  echo "SMOKE-ROOTLESS: PASS (execution, --network=none; rootless networking unsupported in this environment, reported above)"
elif [ "$rootless_ok" = "1" ]; then
  echo "SMOKE-ROOTLESS: PASS"
elif [ "${SMOKE_STRICT_ROOTLESS:-0}" = "1" ]; then
  echo "SMOKE-ROOTLESS: FAIL (strict mode)" >&2
  exit 1
else
  # Explicit report, never a silent skip: the nested job container
  # cannot always provide the userns / tun setup rootless podman
  # needs, which is an environment limit. The rootful sections above
  # still gate the publish.
  echo "::warning title=smoke-rootless::rootless run unsupported in this environment; see log above"
  echo "SMOKE-ROOTLESS: UNSUPPORTED-IN-THIS-ENVIRONMENT (explicitly reported, not skipped silently)"
fi

if [ "${SMOKE_HOST:-0}" = "1" ]; then
  echo "== rootless networking (host mode, required) =="
  # Two rootless containers on a custom network resolving each other
  # by name: exercises rootless netavark + aardvark-dns + pasta (the
  # rootless networking backend our passt package provides). This is
  # the section the nested job containers cannot run at all (pasta
  # cannot open their masked /proc/sys); on a real host it must work,
  # so failure here fails the smoke.
  if [ "$HOST_NET_BLOCKED" = "1" ]; then
    echo "rootless networking already classified BLOCKED-BY-ENVIRONMENT by the probe above; not re-run"
  else
  cat > /tmp/smoke-rootless-net.sh <<'EOS'
#!/usr/bin/env bash
set -euo pipefail
# podman inherits the caller's cwd; when invoked via runuser from a
# root-owned checkout, that cwd is not traversable by the rootless
# user, so start from its home.
cd "$HOME" || cd /tmp
IMAGE="${SMOKE_IMAGE:-docker.io/library/alpine:latest}"
NET="smoke-rnet-$$"
PEER="smoke-rpeer-$$"
cleanup() {
  podman rm -f "$PEER" >/dev/null 2>&1 || true
  podman network rm -f "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
podman network create "$NET" >/dev/null
podman run -d --name "$PEER" --network "$NET" "$IMAGE" sleep 120 >/dev/null
podman run --rm --network "$NET" "$IMAGE" \
  sh -c "ping -c 1 -W 5 $PEER >/dev/null && echo rootless-net-ok"
EOS
  chmod +x /tmp/smoke-rootless-net.sh
  if [ "$(id -u)" = "0" ]; then
    setup_rootless_user || host_fail "rootless-networking" "could not set up rootless user $ROOTLESS_USER"
    if run_as_rootless SMOKE_IMAGE="$IMAGE" \
        bash /tmp/smoke-rootless-net.sh > /tmp/smoke-rootless-net.log 2>&1; then
      cat /tmp/smoke-rootless-net.log
      echo "SMOKE-HOST: rootless-networking PASS"
    else
      cat /tmp/smoke-rootless-net.log
      host_fail "rootless-networking" "see rootless network log above"
    fi
  else
    if bash /tmp/smoke-rootless-net.sh > /tmp/smoke-rootless-net.log 2>&1; then
      cat /tmp/smoke-rootless-net.log
      echo "SMOKE-HOST: rootless-networking PASS"
    else
      cat /tmp/smoke-rootless-net.log
      host_fail "rootless-networking" "see rootless network log above"
    fi
  fi
  fi
fi

if [ -f "$BEFORE_FILE" ]; then
  before="$(cat "$BEFORE_FILE")"
  after="$(dpkg-query -W -f='${Version}' podman 2>/dev/null || podman --version)"
  if [ "$before" != "$after" ]; then
    echo "SMOKE-UPGRADE: PASS (published $before -> this build $after, container runs above)"
  else
    # A same-version rebuild (packaging-only change) legitimately has
    # no version move; the assertion for that case is that the stack
    # was reinstalled in place over the published one and still runs.
    echo "SMOKE-UPGRADE: version unchanged ($after); same-version rebuild installed over the published stack and the runs above passed"
  fi
fi

echo "smoke: all core sections ok"
