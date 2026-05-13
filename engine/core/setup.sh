#!/bin/bash
# Context DNA - One-Command Setup Script
# Run with: curl -sSL https://context-dna.dev/setup.sh | bash
# Or locally: ./setup.sh

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║                    CONTEXT DNA SETUP                                   ║"
echo "║              Autonomous Learning for Developers                       ║"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check prerequisites
echo -e "${YELLOW}Checking prerequisites...${NC}"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is required but not installed.${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ Python 3 found${NC}"

# Check pip
if ! command -v pip3 &> /dev/null && ! command -v pip &> /dev/null; then
    echo -e "${RED}Error: pip is required but not installed.${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ pip found${NC}"

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker is required but not installed.${NC}"
    echo "Install Docker from: https://docs.docker.com/get-docker/"
    exit 1
fi
echo -e "${GREEN}  ✓ Docker found${NC}"

# Check if Docker is running
if ! docker info &> /dev/null; then
    echo -e "${RED}Error: Docker daemon is not running.${NC}"
    echo "Please start Docker and try again."
    exit 1
fi
echo -e "${GREEN}  ✓ Docker daemon running${NC}"

# Check docker-compose
if ! command -v docker-compose &> /dev/null; then
    # Try docker compose (v2)
    if docker compose version &> /dev/null; then
        DOCKER_COMPOSE="docker compose"
    else
        echo -e "${RED}Error: docker-compose is required but not installed.${NC}"
        exit 1
    fi
else
    DOCKER_COMPOSE="docker-compose"
fi
echo -e "${GREEN}  ✓ docker-compose found${NC}"

echo ""

# Install Context DNA
echo -e "${YELLOW}Installing Context DNA...${NC}"

# Check if already installed
if pip3 show context-dna &> /dev/null; then
    echo -e "${GREEN}  ✓ context-dna already installed${NC}"
else
    # Install from PyPI (once published) or local
    if [ -f "pyproject.toml" ]; then
        pip3 install -e . --quiet
    else
        pip3 install context-dna --quiet 2>/dev/null || {
            echo -e "${YELLOW}  Installing from source...${NC}"
            pip3 install git+https://github.com/supportersimulator/context-dna.git --quiet
        }
    fi
    echo -e "${GREEN}  ✓ context-dna installed${NC}"
fi

echo ""

# Start Docker infrastructure
echo -e "${YELLOW}Starting Docker infrastructure...${NC}"

# Find docker-compose.yml
COMPOSE_FILE=""
if [ -f "docker-compose.yml" ]; then
    COMPOSE_FILE="docker-compose.yml"
elif [ -f "$(pip3 show context-dna 2>/dev/null | grep Location | cut -d' ' -f2)/context_dna/docker-compose.yml" ]; then
    COMPOSE_FILE="$(pip3 show context-dna | grep Location | cut -d' ' -f2)/context_dna/docker-compose.yml"
fi

if [ -n "$COMPOSE_FILE" ]; then
    echo "  Using: $COMPOSE_FILE"

    # Start core services
    $DOCKER_COMPOSE -f "$COMPOSE_FILE" up -d postgres redis seaweedfs

    echo -e "${GREEN}  ✓ PostgreSQL started${NC}"
    echo -e "${GREEN}  ✓ Redis started${NC}"
    echo -e "${GREEN}  ✓ SeaweedFS started${NC}"

    # Optionally start Ollama
    read -p "Start Ollama for local LLM? (recommended) [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        $DOCKER_COMPOSE -f "$COMPOSE_FILE" up -d ollama
        echo -e "${GREEN}  ✓ Ollama started${NC}"

        # Wait for Ollama to be ready
        echo "  Waiting for Ollama to initialize..."
        sleep 10

        # Check if Pro and models should be pulled
        if context-dna upgrade --status 2>/dev/null | grep -q "PRO"; then
            echo "  Pulling optimized models (Pro tier detected)..."
            docker exec context-dna-ollama ollama pull llama3.1:8b-instruct-q4_K_M 2>/dev/null || true
            docker exec context-dna-ollama ollama pull nomic-embed-text 2>/dev/null || true
        fi
    fi
else
    echo -e "${YELLOW}  No docker-compose.yml found. Skipping infrastructure setup.${NC}"
    echo "  Run 'context-dna setup' to start infrastructure later."
fi

echo ""

# Wait for services
echo -e "${YELLOW}Waiting for services to be healthy...${NC}"
sleep 5

# Health check
echo "  Checking service health..."

# Check PostgreSQL
if docker exec context-dna-postgres pg_isready -U context_dna &> /dev/null; then
    echo -e "${GREEN}  ✓ PostgreSQL healthy${NC}"
else
    echo -e "${YELLOW}  ⚠ PostgreSQL starting...${NC}"
fi

# Check Redis
if docker exec context-dna-redis redis-cli ping &> /dev/null; then
    echo -e "${GREEN}  ✓ Redis healthy${NC}"
else
    echo -e "${YELLOW}  ⚠ Redis starting...${NC}"
fi

# Check Ollama (if running)
if docker ps | grep -q context-dna-ollama; then
    if curl -s http://localhost:11434/api/tags &> /dev/null; then
        echo -e "${GREEN}  ✓ Ollama healthy${NC}"
    else
        echo -e "${YELLOW}  ⚠ Ollama starting...${NC}"
    fi
fi

echo ""

# Initialize in current project
echo -e "${YELLOW}Initializing Context DNA...${NC}"

if [ -f ".context-dna/config.json" ]; then
    echo -e "${GREEN}  ✓ Already initialized in this project${NC}"
else
    # Initialize with pgvector backend
    context-dna init --backend sqlite 2>/dev/null || {
        echo -e "${YELLOW}  Initializing with SQLite backend...${NC}"
        context-dna init
    }
    echo -e "${GREEN}  ✓ Initialized${NC}"
fi

echo ""

# Summary
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                    SETUP COMPLETE!                                    ║${NC}"
echo -e "${BLUE}╠══════════════════════════════════════════════════════════════════════╣${NC}"
echo -e "${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${GREEN}Context DNA is ready to use!${NC}"
echo -e "${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  Quick Start:"
echo -e "${BLUE}║${NC}    ${YELLOW}context-dna win \"First win\" \"This is how I did it\"${NC}"
echo -e "${BLUE}║${NC}    ${YELLOW}context-dna query \"search term\"${NC}"
echo -e "${BLUE}║${NC}    ${YELLOW}context-dna consult \"task I'm about to do\"${NC}"
echo -e "${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  Install IDE Hooks:"
echo -e "${BLUE}║${NC}    ${YELLOW}context-dna hooks install claude${NC}  (Claude Code)"
echo -e "${BLUE}║${NC}    ${YELLOW}context-dna hooks install cursor${NC} (Cursor)"
echo -e "${BLUE}║${NC}    ${YELLOW}context-dna hooks install git${NC}    (Git commits)"
echo -e "${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  Upgrade to Pro (local LLM, \$29 one-time):"
echo -e "${BLUE}║${NC}    ${YELLOW}context-dna upgrade${NC}"
echo -e "${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  Documentation: https://context-dna.dev/docs"
echo -e "${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════════╝${NC}"
echo ""
