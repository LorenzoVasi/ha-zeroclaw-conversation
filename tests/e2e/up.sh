#!/usr/bin/env bash
# Bring up the end-to-end stack: a fake model provider, and *two* real
# ZeroClaw daemons built from the companion add-on repository — one plain,
# one with a gateway webhook secret configured, so the webhook-auth matrix
# can be tested for real rather than reasoned about. Used identically by
# CI and by hand — see README.md next to this file.
#
#   ADDON_REPO=../addon-zeroclaw ./up.sh
#
# Prints the URLs to use as ZEROCLAW_E2E_HOST / ZEROCLAW_E2E_SECURED_HOST.
set -euo pipefail

ADDON_REPO="${ADDON_REPO:-../../../addon-zeroclaw}"
NETWORK="${E2E_NETWORK:-zc-e2e}"
TOKEN="${ZEROCLAW_E2E_TOKEN:-e2e-token}"
SECRET="${ZEROCLAW_E2E_WEBHOOK_SECRET:-e2e-webhook-secret}"
GATEWAY_PORT="${E2E_GATEWAY_PORT:-42617}"
SECURED_PORT="${E2E_SECURED_PORT:-42618}"
FAKE_LLM_PORT="${E2E_FAKE_LLM_PORT:-8081}"
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

wait_healthy() {
    # $1 = container name
    for _ in $(seq 1 45); do
        if docker exec "$1" curl -sf -m 2 http://127.0.0.1:8099/health >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    echo "!! ${1} never became healthy"
    docker logs "$1" | tail -30
    return 1
}

start_gateway() {
    # $1 = container name, $2 = published port, $3 = webhook secret ("" for none)
    local name="$1" port="$2" secret="$3" volume="$1-data" options

    docker rm -f "${name}" >/dev/null 2>&1 || true
    docker volume rm "${volume}" >/dev/null 2>&1 || true
    docker volume create "${volume}" >/dev/null

    options="$(mktemp)"
    cat > "${options}" <<EOF
{
  "log_level": "info",
  "api_token": "${TOKEN}",
  "webhook_secret": "${secret}",
  "home_assistant_url": "",
  "home_assistant_token": "",
  "providers": [
    {
      "provider_type": "custom",
      "alias": "fake",
      "uri": "http://fake-llm:${FAKE_LLM_PORT}/v1",
      "model": "fake-model",
      "api_key": "unused"
    }
  ]
}
EOF

    docker create --name "${name}" --network "${NETWORK}" \
        -v "${volume}:/data/zeroclaw" -p "${port}:42617" "${IMAGE}" >/dev/null
    docker cp "$(to_host_path "${options}")" "${name}:/data/options.json"
    docker start "${name}" >/dev/null
    wait_healthy "${name}"

    # Seeding a provider block does NOT bind any agent to it — an agent
    # carries its own `model_provider`, and the baked-in default points at
    # `openrouter.default`, so without this every turn fails with "LLM
    # request failed". In normal use the integration's config flow does
    # this binding when it creates an agent; here it has to be explicit.
    #
    # It has to go through the live API: `zeroclaw config set` rejects
    # `agents.<alias>.model_provider` as an unknown property, the same
    # dynamic-map-path limitation the add-on's own run.sh works around for
    # `mcp.servers` (see that repo's docs/DECISIONS.md).
    local auth=(-H "Authorization: Bearer ${TOKEN}")
    [ -n "${secret}" ] && auth+=(-H "X-Webhook-Secret: ${secret}")
    docker exec "${name}" curl -sf -X PUT "http://127.0.0.1:8099/api/config/prop" \
        -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
        -d '{"path":"agents.default.model_provider","value":"custom.fake"}' >/dev/null

    docker restart "${name}" >/dev/null
    wait_healthy "${name}"

    local reply
    reply="$(docker exec "${name}" curl -s -X POST http://127.0.0.1:8099/webhook \
        "${auth[@]}" -H 'Content-Type: application/json' \
        -d '{"message":"ping"}' --max-time 60)"
    case "${reply}" in
        *'"response":"pong"'*) echo "    ${name}: ${reply}" ;;
        *) echo "!! ${name} came up but did not answer as expected: ${reply}"; return 1 ;;
    esac
}

echo "==> Building the add-on image from ${ADDON_REPO}"
docker build -t "${IMAGE}" -f "${ADDON_REPO_HOST}/Dockerfile" "${ADDON_REPO_HOST}"

echo "==> Creating the network"
docker network create "${NETWORK}" >/dev/null 2>&1 || true

echo "==> Starting the fake model provider"
docker rm -f fake-llm >/dev/null 2>&1 || true
docker run -d --name fake-llm --network "${NETWORK}" \
    -w /app -v "${HERE}:/app:ro" -p "${FAKE_LLM_PORT}:${FAKE_LLM_PORT}" \
    python:3.13-slim \
    sh -c "pip install --quiet aiohttp && python fake_llm.py --port ${FAKE_LLM_PORT}" >/dev/null

for _ in $(seq 1 40); do
    if docker run --rm --network "${NETWORK}" curlimages/curl:latest \
        -sf -m 2 "http://fake-llm:${FAKE_LLM_PORT}/health" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

echo "==> Starting ZeroClaw (plain)"
start_gateway zc-e2e-gw "${GATEWAY_PORT}" ""

echo "==> Starting ZeroClaw (webhook secret configured)"
start_gateway zc-e2e-gw-secret "${SECURED_PORT}" "${SECRET}"

echo
echo "Ready. Run the suite with:"
echo "  ZEROCLAW_E2E_HOST=http://localhost:${GATEWAY_PORT} \\"
echo "  ZEROCLAW_E2E_SECURED_HOST=http://localhost:${SECURED_PORT} \\"
echo "  ZEROCLAW_E2E_FAKE_LLM=http://localhost:${FAKE_LLM_PORT} pytest -m e2e"
echo "…or, from a container on the '${NETWORK}' network, use the container"
echo "names (zc-e2e-gw / zc-e2e-gw-secret / fake-llm) on port 42617/8081."
