#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
net="swarm-sandbox-$$"
cid=""

cleanup() {
  if [ -n "$cid" ]; then
    docker rm -f "$cid" >/dev/null 2>&1 || true
  fi
  docker network rm "$net" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create --internal "$net" >/dev/null
cid="$(docker run -d \
  --name "swarm-sandbox-$$" \
  --network "$net" \
  --user 65534:65534 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 64 \
  --memory 512m \
  --cpus 0.5 \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e HOME=/tmp \
  -e RUN_TOKEN=tok-sandbox \
  -e PROXY_URL=http://proxy:18894 \
  -v "$root/tests/sandbox_probe.py:/probe.py:ro" \
  python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 \
  python /probe.py)"

status="$(docker wait "$cid")"
docker logs "$cid"
if [ "$status" != "0" ]; then
  echo "probe exited $status" >&2
  exit 1
fi

inspect="$(docker inspect "$cid" --format '{{.HostConfig.ReadonlyRootfs}}|{{.Config.User}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}|{{json .HostConfig.Tmpfs}}')"
echo "inspect $inspect"
python3 - "$inspect" <<'PY'
import json, sys
readonly, user, capdrop, secopt, memory, nano, pids, tmpfs = sys.argv[1].split("|", 7)
assert readonly == "true", readonly
assert user == "65534:65534", user
assert "ALL" in json.loads(capdrop), capdrop
assert any("no-new-privileges" in item for item in json.loads(secopt)), secopt
assert int(memory) == 512 * 1024 * 1024, memory
assert int(nano) == 500_000_000, nano
assert int(pids) == 64, pids
mounts = json.loads(tmpfs)
assert "/tmp" in mounts, mounts
assert "size=64m" in mounts["/tmp"] or "size=67108864" in mounts["/tmp"], mounts
print("inspect flags held")
PY
