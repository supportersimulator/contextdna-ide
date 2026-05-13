#!/bin/bash
# Model Switching Setup — API failover for when Anthropic limits hit
#
# Two options:
#   A. OmniRoute (mac1 approach) — full dashboard, multi-provider combos
#   B. claude-code-router (mac2 approach) — simple Anthropic->DeepSeek proxy
#
# Usage: bash scripts/model-switch-setup.sh [install|status|help]

ACTION="${1:-status}"

case "$ACTION" in
    install)
        echo "Which router to install?"
        echo "  1. OmniRoute (recommended — dashboard + multi-provider)"
        echo "  2. claude-code-router (simple proxy)"
        echo ""
        echo "Installing both (skip what fails)..."
        echo ""
        echo "--- OmniRoute ---"
        npm install -g omniroute 2>/dev/null && echo "  OmniRoute installed" || echo "  OmniRoute: npm install failed (try later)"
        echo ""
        echo "--- claude-code-router ---"
        npm install -g claude-code-router 2>/dev/null && echo "  claude-code-router installed" || echo "  claude-code-router: npm install failed (try later)"
        echo ""
        echo "Run: bash scripts/model-switch-setup.sh help"
        ;;
    status)
        echo "=== Model Switch Status ==="
        echo ""
        echo "--- Routers ---"
        if command -v omniroute &>/dev/null; then
            echo "  OmniRoute: INSTALLED"
            if curl -sf http://localhost:20128 >/dev/null 2>&1; then
                echo "  OmniRoute: RUNNING on :20128"
            else
                echo "  OmniRoute: installed but not running"
            fi
        else
            echo "  OmniRoute: not installed (npm install -g omniroute)"
        fi
        echo ""
        if command -v claude-code-router &>/dev/null; then
            echo "  claude-code-router: INSTALLED"
            claude-code-router --version 2>/dev/null || true
            if curl -sf http://127.0.0.1:3456/health >/dev/null 2>&1; then
                echo "  claude-code-router: RUNNING on :3456"
            else
                echo "  claude-code-router: installed but not running"
            fi
        else
            echo "  claude-code-router: not installed"
        fi
        echo ""
        echo "--- Environment ---"
        if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
            echo "  ANTHROPIC_BASE_URL: $ANTHROPIC_BASE_URL"
        else
            echo "  ANTHROPIC_BASE_URL: not set (direct to Anthropic)"
        fi
        if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
            echo "  DEEPSEEK_API_KEY: set"
        else
            echo "  DEEPSEEK_API_KEY: not set"
        fi
        if [ -n "${OPENROUTER_API_KEY:-}" ]; then
            echo "  OPENROUTER_API_KEY: set"
        else
            echo "  OPENROUTER_API_KEY: not set"
        fi
        ;;
    help)
        echo "=== Model Switching Quick Start ==="
        echo ""
        echo "Option A: OmniRoute (full dashboard — mac1 approach)"
        echo "  1. Install:  npm install -g omniroute"
        echo "  2. Start:    omniroute --no-open"
        echo "  3. Dashboard: http://localhost:20128"
        echo "  4. Add providers, create fallback Combo"
        echo "  5. Set env:  export ANTHROPIC_BASE_URL=http://localhost:20128/v1"
        echo "  Failover: Anthropic -> DeepSeek -> OpenRouter (configurable)"
        echo ""
        echo "Option B: claude-code-router (simple proxy — mac2 approach)"
        echo "  1. Install:  bash scripts/model-switch-setup.sh install"
        echo "  2. Start:    claude-code-router start"
        echo "  3. Set env:  export ANTHROPIC_BASE_URL=http://localhost:3456"
        echo "  4. Add key:  export DEEPSEEK_API_KEY=<key>"
        echo "  Failover: Anthropic -> DeepSeek"
        echo ""
        echo "Option C: Manual model override (no router needed)"
        echo "  claude --model claude-sonnet-4-20250514"
        echo "  claude --model claude-haiku-4-20250414"
        echo ""
        echo "Option D: Environment variable"
        echo "  export CLAUDE_CODE_DEFAULT_MODEL=claude-sonnet-4-20250514"
        echo ""
        echo "Trip mode auto-detects and starts whichever router is installed."
        ;;
    *)
        echo "Usage: bash scripts/model-switch-setup.sh [install|status|help]"
        ;;
esac
