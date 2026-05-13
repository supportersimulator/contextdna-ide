#!/bin/bash
set -e

# Push-freeze guard: CI budget protection. Set FLEET_PUSH_FREEZE=0 to actually deploy.
if [ "${FLEET_PUSH_FREEZE:-0}" = "1" ]; then
    echo "❄️  FLEET_PUSH_FREEZE=1 — skipping landing-page deploy. Unset to push."
    exit 0
fi

echo "🔵 Deploying landing page submodule..."

# Step 1 – commit changes inside the submodule
cd landing-page
git add .
git commit -m "${1:-Update landing page}"
git push origin main
echo "   ✓ Submodule updated"

# Step 2 – update pointer in the superrepo
cd ..
git add landing-page
git commit -m "Update landing-page submodule pointer"
git push origin main
echo "   ✓ Superrepo updated"

echo "🚀 Deployment complete."
echo "Landing page and pointer are fully synchronized."
