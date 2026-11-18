#!/usr/bin/env bash
# Wait for a just-started E2E container's MCP endpoint to answer HTTP.
#
# Usage: wait_for_container_ready.sh <port> <container-name>
#
# The three container-start steps in .github/workflows/e2e-container.yml share
# this loop: it polls http://127.0.0.1:<port>/mcp for up to 30 seconds and
# fails the step - after dumping the container's logs - when no answer arrives.
# A failed container is still removed by each step's `if: always()` cleanup
# step, so this script only reports; it does not tear anything down.
set -u

PORT="$1"
CONTAINER="$2"

echo "Waiting for container HTTP endpoint to be ready..."
READY=0
for i in $(seq 1 30); do
  # curl already prints 000 for a failed request; a `|| echo "000"` fallback
  # would concatenate into "000000" and defeat the check below, declaring a
  # still-booting container ready.
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${PORT}/mcp" 2>/dev/null || true)
  if [ "$HTTP_STATUS" != "000" ]; then
    echo "Container is ready (HTTP $HTTP_STATUS at attempt $i)"
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "❌ Container failed to respond within 30 seconds"
  docker logs "$CONTAINER" || true
  exit 1
fi
