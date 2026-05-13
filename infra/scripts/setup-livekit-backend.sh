#!/bin/bash
# Setup LiveKit environment variables on Django backend (EC2)
# Run via AWS SSM or directly on the instance
#
# Usage:
#   AWS SSM: aws ssm send-command --document-name "AWS-RunShellScript" \
#            --targets "Key=instanceids,Values=i-0b60414d5de76d320" \
#            --parameters 'commands=["bash /var/www/ersim/app/infra/scripts/setup-livekit-backend.sh"]'
#
#   Direct: sudo bash /var/www/ersim/app/infra/scripts/setup-livekit-backend.sh

set -e

echo "=== Setting up LiveKit backend configuration ==="

# Fetch secrets from AWS Secrets Manager
echo "Fetching LiveKit credentials from Secrets Manager..."

LIVEKIT_API_KEY=$(aws secretsmanager get-secret-value \
    --secret-id /ersim/prod/voice/LIVEKIT_API_KEY \
    --query SecretString --output text 2>/dev/null || echo "")

LIVEKIT_API_SECRET=$(aws secretsmanager get-secret-value \
    --secret-id /ersim/prod/voice/LIVEKIT_API_SECRET \
    --query SecretString --output text 2>/dev/null || echo "")

LIVEKIT_URL=$(aws secretsmanager get-secret-value \
    --secret-id /ersim/prod/voice/LIVEKIT_URL \
    --query SecretString --output text 2>/dev/null || echo "wss://voice.ersimulator.com")

if [ -z "$LIVEKIT_API_KEY" ] || [ -z "$LIVEKIT_API_SECRET" ]; then
    echo "ERROR: Could not fetch LiveKit secrets from Secrets Manager"
    echo "Make sure the EC2 instance has the correct IAM role to access Secrets Manager"
    exit 1
fi

echo "✓ Fetched LiveKit credentials"

# Create or update .env file
ENV_FILE="/var/www/ersim/app/backend/.env"
echo "Updating $ENV_FILE..."

# Backup existing .env if it exists
if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "${ENV_FILE}.backup.$(date +%Y%m%d_%H%M%S)"
    echo "✓ Backed up existing .env"
fi

# Remove any existing VOICE_BACKEND, LIVEKIT_* lines and add new ones
if [ -f "$ENV_FILE" ]; then
    grep -v "^VOICE_BACKEND=" "$ENV_FILE" | grep -v "^LIVEKIT_" > "${ENV_FILE}.tmp" || true
    mv "${ENV_FILE}.tmp" "$ENV_FILE"
fi

# Append LiveKit configuration
cat >> "$ENV_FILE" << EOF

# LiveKit self-hosted voice stack (auto-configured $(date))
VOICE_BACKEND=livekit
LIVEKIT_API_KEY=$LIVEKIT_API_KEY
LIVEKIT_API_SECRET=$LIVEKIT_API_SECRET
LIVEKIT_URL=$LIVEKIT_URL
EOF

echo "✓ Updated .env with LiveKit configuration"

# Update gunicorn systemd service
echo "Updating gunicorn systemd service..."

GUNICORN_SERVICE="/etc/systemd/system/gunicorn.service"
if grep -q "VOICE_BACKEND" "$GUNICORN_SERVICE"; then
    echo "✓ VOICE_BACKEND already in gunicorn.service (skipping)"
else
    # Add VOICE_BACKEND to the service file
    sudo sed -i '/Environment=DJANGO_ENV=production/a Environment=VOICE_BACKEND=livekit\nEnvironment=LIVEKIT_URL=wss://voice.ersimulator.com' "$GUNICORN_SERVICE"
    echo "✓ Added VOICE_BACKEND to gunicorn.service"
fi

# Reload systemd and restart gunicorn
echo "Restarting gunicorn..."
sudo systemctl daemon-reload
sudo systemctl restart gunicorn

echo ""
echo "=== LiveKit Backend Setup Complete ==="
echo "VOICE_BACKEND=livekit"
echo "LIVEKIT_URL=$LIVEKIT_URL"
echo ""
echo "OpenAI Realtime is now DEACTIVATED. All voice sessions will use LiveKit."
