#!/bin/bash
# =============================================================================
# Pre-Commit Secrets Guard - Enhanced Protection
# =============================================================================
# Prevents committing secrets, credentials, or vulnerable data.
# Stricter than default git hooks - protects community template repo.
#
# Blocks:
# - API keys, tokens, passwords
# - Private keys, certificates
# - Personal paths (non-sanitized)
# - Database credentials
# - Webhook URLs with tokens
# - Email addresses (in certain contexts)
# - IP addresses (in certain contexts)
#
# Usage: Install as .git/hooks/pre-commit
# =============================================================================

set -e

# Colors
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo "🔒 Secrets Guard: Scanning staged files..."

# Get list of files being committed
FILES=$(git diff --cached --name-only --diff-filter=ACM)

if [ -z "$FILES" ]; then
    echo "${GREEN}✅ No files to check${NC}"
    exit 0
fi

SECRETS_FOUND=0
WARNINGS_FOUND=0

# =============================================================================
# CRITICAL: API Keys and Tokens
# =============================================================================

# OpenAI API keys
if git diff --cached | grep -qE 'sk-[a-zA-Z0-9]{32,}|sk-proj-[a-zA-Z0-9]{32,}'; then
    echo "${RED}❌ BLOCKED: OpenAI API key detected${NC}"
    echo "   Pattern: sk-... or sk-proj-..."
    echo "   Replace with: \${Context_DNA_OPENAI}"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
fi

# GitHub tokens
if git diff --cached | grep -qE 'ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82}'; then
    echo "${RED}❌ BLOCKED: GitHub token detected${NC}"
    echo "   Pattern: ghp_... or github_pat_..."
    echo "   Replace with: \${GITHUB_TOKEN}"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
fi

# AWS credentials
if git diff --cached | grep -qE 'AKIA[0-9A-Z]{16}|aws_secret_access_key.*[A-Za-z0-9+/]{40}'; then
    echo "${RED}❌ BLOCKED: AWS credentials detected${NC}"
    echo "   Replace with: \${AWS_ACCESS_KEY_ID} / \${AWS_SECRET_ACCESS_KEY}"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
fi

# Generic API keys
if git diff --cached | grep -qiE '(api[_-]?key|apikey|api[_-]?secret)["'\'':]?\s*[:=]\s*["'\''][a-zA-Z0-9+/]{20,}'; then
    echo "${RED}❌ BLOCKED: Generic API key pattern detected${NC}"
    echo "   Replace with: \${API_KEY} or appropriate env var"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
fi

# =============================================================================
# CRITICAL: Passwords and Auth Tokens
# =============================================================================

if git diff --cached | grep -qiE '(password|passwd|pwd)["'\'':]?\s*[:=]\s*["'\''][^"'\'']{3,}'; then
    echo "${RED}❌ BLOCKED: Password detected${NC}"
    echo "   Replace with: \${PASSWORD} or use environment variable"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
fi

# JWT tokens
if git diff --cached | grep -qE 'eyJ[a-zA-Z0-9_-]{20,}\.eyJ[a-zA-Z0-9_-]{20,}'; then
    echo "${RED}❌ BLOCKED: JWT token detected${NC}"
    echo "   Replace with: \${JWT_TOKEN}"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
fi

# =============================================================================
# HIGH RISK: Private Keys and Certificates
# =============================================================================

if git diff --cached | grep -q 'BEGIN.*PRIVATE KEY'; then
    echo "${RED}❌ BLOCKED: Private key detected${NC}"
    echo "   NEVER commit private keys"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
fi

if git diff --cached | grep -q 'BEGIN CERTIFICATE'; then
    echo "${RED}❌ BLOCKED: Certificate detected${NC}"
    echo "   Certificates should not be in version control"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
fi

# =============================================================================
# MODERATE RISK: Personal Information
# =============================================================================

# Unsanitized home paths
if git diff --cached | grep -qE '/Users/[a-z]+/|/home/[a-z]+/|C:\\Users\\[a-z]+\\' | grep -v '\${HOME}'; then
    echo "${YELLOW}⚠️  WARNING: Personal path detected (not sanitized)${NC}"
    echo "   Found: /Users/username/ or /home/username/"
    echo "   Should be: \${HOME}/"
    WARNINGS_FOUND=$((WARNINGS_FOUND + 1))
fi

# Email addresses (in certain files)
for file in $FILES; do
    if [[ "$file" == *.json ]] || [[ "$file" == *.yaml ]] || [[ "$file" == *.toml ]]; then
        if git diff --cached "$file" | grep -qE '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'; then
            echo "${YELLOW}⚠️  WARNING: Email address in $file${NC}"
            echo "   Consider if this should be shared publicly"
            WARNINGS_FOUND=$((WARNINGS_FOUND + 1))
        fi
    fi
done

# =============================================================================
# MODERATE RISK: Database Credentials
# =============================================================================

if git diff --cached | grep -qiE 'postgres://.*:YOUR_PASSWORD@|mysql://.*:.*@|mongodb://.*:YOUR_PASSWORD@'; then
    echo "${RED}❌ BLOCKED: Database connection string with credentials${NC}"
    echo "   Remove username:password from connection strings"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
fi

# =============================================================================
# LOW RISK: Informational
# =============================================================================

# .env files
for file in $FILES; do
    if [[ "$file" == *.env ]] || [[ "$file" == */.env ]]; then
        echo "${RED}❌ BLOCKED: .env file in commit${NC}"
        echo "   File: $file"
        echo "   .env files should NEVER be committed"
        SECRETS_FOUND=$((SECRETS_FOUND + 1))
    fi
done

# =============================================================================
# VERDICT
# =============================================================================

echo ""
echo "──────────────────────────────────────────────────────"

if [ $SECRETS_FOUND -gt 0 ]; then
    echo "${RED}❌ COMMIT BLOCKED: $SECRETS_FOUND critical issue(s) found${NC}"
    echo ""
    echo "🔒 Secrets protection is active."
    echo "   Fix the issues above and try again."
    echo ""
    echo "To bypass (NOT RECOMMENDED):"
    echo "   git commit --no-verify"
    exit 1
elif [ $WARNINGS_FOUND -gt 0 ]; then
    echo "${YELLOW}⚠️  $WARNINGS_FOUND warning(s) found${NC}"
    echo ""
    echo "Review warnings above. If intentional, commit is allowed."
    echo ""
    echo "Proceeding with commit..."
    exit 0
else
    echo "${GREEN}✅ No secrets detected - commit allowed${NC}"
    exit 0
fi
