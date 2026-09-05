#!/usr/bin/env bash
# Bring up the end-to-end stack: a fake model provider, and a real
# ZeroClaw daemon built from the companion add-on repository, wired to
# it. Used identically by CI and by hand — see README.md next to this
# file.
#
#   ADDON_REPO=../addon-zeroclaw ./up.sh
#
# Prints the gateway URL to use as ZEROCLAW_E2E_HOST.
set -euo pipefail

ADDON_REPO="${ADDON_REPO:-../../../addon-zeroclaw}"
NETWORK="${E2E_NETWORK:-zc-e2e}"
TOKEN="${ZEROCLAW_E2E_TOKEN:-e2e-token}"
GATEWAY_PORT="${E2E_GATEWAY_PORT:-42617}"
IMAGE="zeroclaw-addon-e2e"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Git Bash on Windows rewrites arguments that look like absolute paths,
# so `-w /app` reaches Docker as `C:/Program Files/Git/app` and the run
# fails. Turning that off means every path handed *to* Docker has to be
# a native Windows path instead — hence `to_host_path`. All of this is a
# no-op elsewhere, so the same script serves CI (Linux) and a developer
# machine.
to_host_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s' "$1"
    fi
}

if command -v cygpath >/dev/null 2>&1; then
    export MSYS_NO_PATHCONV=1
fi

HERE="$(to_host_path "${HERE}")"
ADDON_REPO_HOST="$(to_host_path "$(cd "${ADDON_REPO}" && pwd)")"

echo "==> Building the add-on image from ${ADDON_REPO}"
docker build -t "${IMAGE}" -f "${ADDON_REPO_HOST}/Dockerfile" "${ADDON_REPO_HOST}"

echo "==> Creating network and volumes"
docker network create "${NETWORK}" >/dev/null 2>&1 || true
docker rm -f fake-llm zc-e2e-gw >/dev/null 2>&1 || true
docker volume rm zc-e2e-data >/dev/null 2>&1 || true
docker volume create zc-e2e-data >/dev/null

echo "==> Starting the fake model provider"
docker run -d --name fake-llm --network "${NETWORK}" \
    -w /app -v "${HERE}:/app:ro" \
    python:3.13-slim \
    sh -c "pip install --quiet aiohttp && python fake_llm.py --port 8081" >/dev/null

for _ in $(seq 1 40); do
    if docker run --rm --network "${NETWORK}" curlimages/curl:latest \
        -sf -m 2 http://fake-llm:8081/health >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

echo "==> Starting ZeroClaw pointed at it"
# The add-on's `custom` provider slot is what makes this possible: any
# OpenAI-compatible endpoint, no credentials that matter.
OPTIONS_FILE="$(mktemp)"
cat > "${OPTIONS_FILE}" <<EOF
{
  "log_level": "info",
  "api_token": "${TOKEN}",
  "webhook_secret": "",
  "home_assistant_url": "",
  "home_assistant_token": "",
  "providers": [
    {
      "provider_type": "custom",
      "alias": "fake",
      "uri": "http://fake-llm:8081/v1",
      "model": "fake-model",
      "api_key": "unused"
    }
  ]
}
EOF

docker create --name zc-e2e-gw --network "${NETWORK}" \
    -v zc-e2e-data:/data/zeroclaw -p "${GATEWAY_PORT}:42617" "${IMAGE}" >/dev/null
docker cp "$(to_host_path "${OPTIONS_FILE}")" zc-e2e-gw:/data/options.json
docker start zc-e2e-gw >/dev/null

for _ in $(seq 1 45); do
    if docker exec zc-e2e-gw curl -sf -m 2 http://127.0.0.1:8099/health >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

# Seeding a provider block does NOT bind any agent to it — an agent
# carries its own `model_provider`, and the baked-in default points at
# `openrouter.default`, so without this every turn fails with "LLM
# request failed". In normal use the integration's config flow does this
# binding when it creates an agent; here it has to be done explicitly.
#
# It has to go through the live API: `zeroclaw config set` rejects
# `agents.<alias>.model_provider` as an unknown property, the same
# dynamic-map-path limitation the add-on's own run.sh works around for
# `mcp.servers` (see that repo's docs/DECISIONS.md).
echo "==> Binding the default agent to the fake provider"
docker exec zc-e2e-gw curl -sf -X PUT "http://127.0.0.1:8099/api/config/prop" \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    -d '{"path":"agents.default.model_provider","value":"custom.fake"}' >/dev/null

docker restart zc-e2e-gw >/dev/null
for _ in $(seq 1 45); do
    if docker exec zc-e2e-gw curl -sf -m 2 http://127.0.0.1:8099/health >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

echo "==> Smoke check"
reply="$(docker exec zc-e2e-gw curl -s -X POST http://127.0.0.1:8099/webhook \
    -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
    -d '{"message":"ping"}' --max-time 60)"
echo "    ${reply}"
case "${reply}" in
    *'"response":"pong"'*) ;;
    *) echo "!! the stack came up but did not answer as expected"; exit 1 ;;
esac

echo
echo "Ready. Run the suite with:"
echo "  ZEROCLAW_E2E_HOST=http://localhost:${GATEWAY_PORT} pytest -m e2e"
echo "…or, from a container on the '${NETWORK}' network:"
echo "  ZEROCLAW_E2E_HOST=http://zc-e2e-gw:42617 pytest -m e2e"
