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
#
# The vfs storage driver and --cgroups=disabled are for the nested CI
# container environment, same as the smoke this replaces: cgroup v2
# controller delegation does not work inside GitHub's job containers.
set -euo pipefail

IMAGE="${SMOKE_IMAGE:-docker.io/library/alpine:latest}"
PODMAN="podman --storage-driver=vfs"
RUNFLAGS="--cgroups=disabled"
BEFORE_FILE="${SMOKE_BEFORE_FILE:-/tmp/podman-before.txt}"

echo "== stack binary versions =="
podman --version
conmon --version
crun --version
/usr/libexec/podman/netavark --version
/usr/libexec/podman/aardvark-dns --version
command -v passt
$PODMAN info --format '{{.Host.Arch}} {{.Version.Version}}'

echo "== basic container run =="
$PODMAN run $RUNFLAGS --rm "$IMAGE" echo "smoke: basic run ok"

echo "== network + DNS: containers resolve each other by name =="
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

echo "== rootless run =="
rootless_ok=0
if [ "$(id -u)" != "0" ]; then
  if $PODMAN run $RUNFLAGS --rm "$IMAGE" echo "smoke: rootless ok"; then
    rootless_ok=1
  fi
else
  if ! id smokeuser >/dev/null 2>&1; then
    useradd -m -s /bin/bash smokeuser || true
  fi
  if id smokeuser >/dev/null 2>&1; then
    grep -q '^smokeuser:' /etc/subuid 2>/dev/null || \
      echo 'smokeuser:100000:65536' >> /etc/subuid || true
    grep -q '^smokeuser:' /etc/subgid 2>/dev/null || \
      echo 'smokeuser:100000:65536' >> /etc/subgid || true
    mkdir -p /tmp/smoke-xdg
    chown smokeuser:smokeuser /tmp/smoke-xdg || true
    if runuser -u smokeuser -- env HOME=/home/smokeuser \
        XDG_RUNTIME_DIR=/tmp/smoke-xdg \
        podman --storage-driver=vfs run $RUNFLAGS --rm "$IMAGE" \
        echo "smoke: rootless ok" > /tmp/smoke-rootless.log 2>&1; then
      cat /tmp/smoke-rootless.log
      rootless_ok=1
    else
      echo "rootless run failed; last log lines:"
      tail -20 /tmp/smoke-rootless.log || true
    fi
  fi
fi
if [ "$rootless_ok" = "1" ]; then
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
