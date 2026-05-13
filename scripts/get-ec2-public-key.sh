#!/bin/bash
# Get the EC2 Ed25519 public key for local voice session validation
#
# Run this on EC2 to get the public key, then set it locally:
#   export CONTEXTDNA_EC2_PUBLIC_KEY="<output>"
#
# This enables the "1 stream" security model where:
# - EC2 signs voice session tokens with private key
# - Local validates tokens with public key (zero network latency)

set -e

# Navigate to backend directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../backend"

if [ ! -d "$BACKEND_DIR" ]; then
    echo "Error: Backend directory not found at $BACKEND_DIR"
    exit 1
fi

cd "$BACKEND_DIR"

# Ensure Django settings are available
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-ersim_backend.settings.prod}"

# Get the public key
echo "Getting EC2 public key..."
echo "=========================="
.venv/bin/python3 -c "
import os
import sys
sys.path.insert(0, '.')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ersim_backend.settings.prod')

import django
django.setup()

from payments.subscription_signer import get_server_public_key_b64
print(get_server_public_key_b64())
"
echo ""
echo "=========================="
echo ""
echo "To enable production mode on your local machine:"
echo "1. Copy the key above"
echo "2. Set environment variable:"
echo "   export CONTEXTDNA_EC2_PUBLIC_KEY=\"<paste_key>\""
echo ""
echo "Or add to your shell profile (~/.zshrc or ~/.bashrc)"
