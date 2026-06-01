#!/usr/bin/env bash
# Safe replay of Natural Food Wompi event (SD7wnV) against deployed router.
# Run from your machine or the API server (not from CI without network).
set -euo pipefail

# Production API hostname (api.warocol.com has no public DNS; app uses api.warolabs.com)
API_BASE="${API_BASE:-https://api.warolabs.com}"
PAYLOAD_FILE="${PAYLOAD_FILE:-/tmp/natural_food_sd7wnv_wompi.json}"

cat > "$PAYLOAD_FILE" <<'EOF'
{
  "data": {
    "transaction": {
      "id": "186468-1780267163-84099",
      "status": "APPROVED",
      "currency": "COP",
      "reference": "SD7wnV_1780267109_9FFY8F1wQ",
      "redirect_url": "https://warocol.com/billing/confirmacion",
      "payment_link_id": "SD7wnV",
      "amount_in_cents": 9590000
    }
  },
  "event": "transaction.updated",
  "sent_at": "2026-05-31T22:40:35.577Z",
  "signature": {
    "checksum": "57013da4f2efe3dfe62dd871735ba3e7912271c83801d2da28ad5fdb2877720b",
    "properties": [
      "transaction.id",
      "transaction.status",
      "transaction.amount_in_cents"
    ]
  },
  "timestamp": 1780267235,
  "environment": "prod"
}
EOF

echo "POST ${API_BASE}/payments/webhooks/wompi"
echo "---"
curl -sS -w "\nHTTP %{http_code}\n" \
  -X POST "${API_BASE}/payments/webhooks/wompi" \
  -H "Content-Type: application/json" \
  -d @"$PAYLOAD_FILE"
echo "---"
echo "Expected: HTTP 200 and body {\"received\": true} (Colombia)"
echo "If 401: WOMPI_EVENTS_SECRET on server does not match prod Wompi signing key"
echo "If 500 tenant detection: redeploy latest main (middleware fix) and check docker logs"
echo "If 500 classification/dispatch failed: DB unreachable from container (see logs)"
echo "If {\"status\":\"received\"}: routed to Tickets (unexpected for this payload)"
echo "Logs: docker logs api-warocolcom-web-1 --tail 80 2>&1 | grep -i wompi"
