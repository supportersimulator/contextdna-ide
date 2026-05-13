#!/bin/bash
# Start Cloudflare Tunnel for Synaptic Voice
# This exposes localhost:8888 (Synaptic) to voice.contextdna.io

echo "🌐 Starting Cloudflare Tunnel for Synaptic..."
echo "   voice.contextdna.io → localhost:8888"
echo ""

# Fetch tunnel token from AWS Secrets Manager
TUNNEL_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id "/contextdna/cloudflare/TUNNEL_TOKEN" \
  --query SecretString --output text 2>/dev/null)

if [ -z "$TUNNEL_TOKEN" ]; then
  echo "❌ Failed to fetch tunnel token from AWS Secrets Manager"
  exit 1
fi

# Run tunnel with token (managed by Cloudflare)
cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN"
