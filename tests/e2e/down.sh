#!/usr/bin/env bash
# Tear down whatever `up.sh` created.
set -uo pipefail

NETWORK="${E2E_NETWORK:-zc-e2e}"

docker rm -f fake-llm zc-e2e-gw zc-e2e-gw-secret >/dev/null 2>&1 || true
docker volume rm zc-e2e-gw-data zc-e2e-gw-secret-data zc-e2e-data >/dev/null 2>&1 || true
docker network rm "${NETWORK}" >/dev/null 2>&1 || true
echo "Stack removed."
