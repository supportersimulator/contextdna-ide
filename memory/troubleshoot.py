#!/usr/bin/env python3
"""
Acontext Troubleshooting & Debug Tool

Generates comprehensive diagnostic information for debugging Acontext issues.
Output is formatted for easy copy-paste to AI assistants.

Usage:
    python memory/troubleshoot.py           # Full diagnostics
    python memory/troubleshoot.py --quick   # Quick health check
    python memory/troubleshoot.py --copy    # Copy to clipboard (macOS)
"""

import subprocess
import sys
import os
import json
from datetime import datetime
from pathlib import Path

# Colors for terminal output
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'


def run_cmd(cmd, timeout=10):
    """Run a command and return output."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
    except Exception as e:
        return "", str(e), -1


def check_docker():
    """Check Docker status."""
    out, err, code = run_cmd("docker info 2>/dev/null")
    if code != 0:
        return {"running": False, "error": "Docker not running or not installed"}

    # Get Docker version
    ver_out, _, _ = run_cmd("docker --version")
    return {"running": True, "version": ver_out}


def check_containers():
    """Check all Context DNA containers."""
    out, err, code = run_cmd(
        'docker ps -a --filter "name=contextdna" --format "{{.Names}}|{{.Status}}|{{.Ports}}"'
    )

    if code != 0:
        return {"error": err}

    containers = []
    for line in out.strip().split('\n'):
        if line:
            parts = line.split('|')
            if len(parts) >= 2:
                name = parts[0]
                status = parts[1]
                ports = parts[2] if len(parts) > 2 else ""

                # Check if healthy
                healthy = "(healthy)" in status.lower()
                running = status.lower().startswith("up")

                containers.append({
                    "name": name,
                    "status": status,
                    "ports": ports,
                    "running": running,
                    "healthy": healthy
                })

    return {
        "containers": containers,
        "total": len(containers),
        "running": sum(1 for c in containers if c["running"]),
        "healthy": sum(1 for c in containers if c["healthy"])
    }


def check_api():
    """Check Context DNA API health."""
    out, err, code = run_cmd('curl -s --max-time 5 http://localhost:8029/health')
    if code != 0:
        return {"reachable": False, "error": err or "Connection failed"}

    try:
        data = json.loads(out)
        return {"reachable": True, "response": data}
    except Exception:
        return {"reachable": True, "response": out}


def check_core():
    """Check Context DNA Core service."""
    out, err, code = run_cmd('curl -s --max-time 5 http://localhost:8019/health')
    if code != 0:
        return {"reachable": False, "error": err or "Connection failed"}

    try:
        data = json.loads(out)
        return {"reachable": True, "response": data}
    except Exception:
        return {"reachable": True, "response": out}


def check_env_file():
    """Check .env configuration."""
    env_path = Path.home() / "dev/er-simulator-superrepo/context-dna/infra/.env"

    if not env_path.exists():
        return {"exists": False, "error": f"File not found: {env_path}"}

    config = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                # Mask API keys
                if 'KEY' in key.upper() or 'SECRET' in key.upper():
                    config[key] = value[:10] + '...' + value[-4:] if len(value) > 14 else '***'
                else:
                    config[key] = value

    return {"exists": True, "config": config}


def check_python_sdk():
    """Check if acontext Python SDK is installed."""
    out, err, code = run_cmd(
        'python3 -c "import acontext; print(acontext.__version__)" 2>/dev/null'
    )
    if code != 0:
        # Try with venv
        venv_python = Path.home() / "dev/er-simulator-superrepo/.venv/bin/python3"
        out, err, code = run_cmd(
            f'{venv_python} -c "import acontext; print(acontext.__version__)" 2>/dev/null'
        )

    if code != 0:
        return {"installed": False, "error": "acontext package not found"}

    return {"installed": True, "version": out}


def check_cli():
    """Check Context DNA CLI."""
    cli_path = Path.home() / ".acontext/bin/acontext"

    if not cli_path.exists():
        return {"installed": False, "error": f"CLI not found at {cli_path}"}

    out, err, code = run_cmd(f"{cli_path} version 2>/dev/null")
    return {"installed": True, "version": out or "unknown", "path": str(cli_path)}


def check_disk_space():
    """Check Docker disk usage."""
    out, err, code = run_cmd("docker system df --format 'json'")
    if code != 0:
        return {"error": err}

    # Get overall disk usage
    out2, _, _ = run_cmd("df -h /var/lib/docker 2>/dev/null || df -h ~/Library/Containers/com.docker.docker 2>/dev/null || df -h ~")
    return {"docker_df": out, "host_df": out2}


def get_container_logs(container_name, lines=50):
    """Get recent logs from a container."""
    out, err, code = run_cmd(f"docker logs --tail {lines} {container_name} 2>&1")
    return out if code == 0 else err


def check_memory_data():
    """Check if memory data exists."""
    try:
        from acontext import AcontextClient
        client = AcontextClient(
            base_url='http://localhost:8029/api/v1',
            api_key='sk-ac-your-root-api-bearer-token'
        )

        # Try to list spaces
        spaces = client.spaces.list()
        space_count = len(spaces.items)

        # Try a test query if we have a space
        if space_count > 0:
            space_id = spaces.items[0].id
            result = client.spaces.experience_search(space_id, query="test", limit=1)
            return {
                "connected": True,
                "spaces": space_count,
                "search_works": True,
                "space_id": space_id
            }

        return {"connected": True, "spaces": space_count, "search_works": False}
    except Exception as e:
        return {"connected": False, "error": str(e)}


def generate_report(quick=False):
    """Generate full diagnostic report."""
    report = []
    report.append("=" * 70)
    report.append("ACONTEXT TROUBLESHOOTING REPORT")
    report.append(f"Generated: {datetime.now().isoformat()}")
    report.append("=" * 70)
    report.append("")

    # 1. Docker Status
    report.append("## 1. DOCKER STATUS")
    report.append("-" * 40)
    docker = check_docker()
    if docker.get("running"):
        report.append(f"✅ Docker: Running")
        report.append(f"   Version: {docker.get('version', 'unknown')}")
    else:
        report.append(f"❌ Docker: NOT RUNNING")
        report.append(f"   Error: {docker.get('error', 'unknown')}")
        report.append("")
        report.append("   FIX: Start Docker Desktop application")
    report.append("")

    # 2. Container Status
    report.append("## 2. CONTAINER STATUS")
    report.append("-" * 40)
    containers = check_containers()

    if containers.get("error"):
        report.append(f"❌ Error checking containers: {containers['error']}")
    else:
        total = containers.get("total", 0)
        running = containers.get("running", 0)
        healthy = containers.get("healthy", 0)

        if total == 0:
            report.append("❌ No Context DNA containers found")
            report.append("")
            report.append("   FIX: Start Context DNA with:")
            report.append("   cd ~/dev/er-simulator-superrepo/context-dna/infra")
            report.append("   ~/.acontext/bin/acontext docker up -d")
        elif running < total:
            report.append(f"⚠️  Containers: {running}/{total} running, {healthy} healthy")
        else:
            report.append(f"✅ Containers: {running}/{total} running, {healthy} healthy")

        report.append("")
        for c in containers.get("containers", []):
            status_icon = "✅" if c["healthy"] else ("🟡" if c["running"] else "❌")
            report.append(f"   {status_icon} {c['name']}: {c['status']}")
    report.append("")

    # 3. API Health
    report.append("## 3. API HEALTH")
    report.append("-" * 40)
    api = check_api()
    core = check_core()

    if api.get("reachable"):
        report.append(f"✅ API (8029): Reachable - {api.get('response')}")
    else:
        report.append(f"❌ API (8029): Not reachable - {api.get('error')}")

    if core.get("reachable"):
        report.append(f"✅ Core (8019): Reachable - {core.get('response')}")
    else:
        report.append(f"❌ Core (8019): Not reachable - {core.get('error')}")
    report.append("")

    # 4. Configuration
    report.append("## 4. CONFIGURATION")
    report.append("-" * 40)
    env = check_env_file()
    if env.get("exists"):
        report.append("✅ .env file found")
        for key, value in env.get("config", {}).items():
            report.append(f"   {key}={value}")
    else:
        report.append(f"❌ .env file: {env.get('error')}")
    report.append("")

    # 5. SDK & CLI
    report.append("## 5. SDK & CLI")
    report.append("-" * 40)
    sdk = check_python_sdk()
    cli = check_cli()

    if sdk.get("installed"):
        report.append(f"✅ Python SDK: v{sdk.get('version')}")
    else:
        report.append(f"❌ Python SDK: Not installed")
        report.append("   FIX: pip install acontext")

    if cli.get("installed"):
        report.append(f"✅ CLI: {cli.get('version')} at {cli.get('path')}")
    else:
        report.append(f"❌ CLI: Not installed")
        report.append("   FIX: curl -fsSL https://install.acontext.io | sh")
    report.append("")

    # 6. Memory Data
    if not quick:
        report.append("## 6. MEMORY DATA")
        report.append("-" * 40)
        memory = check_memory_data()
        if memory.get("connected"):
            report.append(f"✅ Connected to Context DNA")
            report.append(f"   Spaces: {memory.get('spaces', 0)}")
            report.append(f"   Search: {'✅ Working' if memory.get('search_works') else '⚠️ No data'}")
            if memory.get("space_id"):
                report.append(f"   Space ID: {memory.get('space_id')}")
        else:
            report.append(f"❌ Cannot connect: {memory.get('error')}")
        report.append("")

    # 7. Container Logs (if issues detected)
    if not quick:
        containers_data = check_containers()
        unhealthy = [c for c in containers_data.get("containers", []) if not c.get("healthy") and c.get("running")]

        if unhealthy:
            report.append("## 7. CONTAINER LOGS (Unhealthy Services)")
            report.append("-" * 40)
            for c in unhealthy[:3]:  # Limit to 3 containers
                report.append(f"\n### {c['name']} (last 30 lines)")
                report.append("```")
                logs = get_container_logs(c['name'], lines=30)
                report.append(logs[-2000:] if len(logs) > 2000 else logs)  # Limit log size
                report.append("```")
            report.append("")

    # 8. Quick Reference
    report.append("## QUICK REFERENCE")
    report.append("-" * 40)
    report.append("")
    report.append("### Start Context DNA:")
    report.append("```bash")
    report.append("cd ~/dev/er-simulator-superrepo")
    report.append("./scripts/context-dna up")
    report.append("```")
    report.append("")
    report.append("### Stop Context DNA:")
    report.append("```bash")
    report.append("./scripts/context-dna down")
    report.append("```")
    report.append("")
    report.append("### Query Memory:")
    report.append("```bash")
    report.append('python memory/query.py "your search terms"')
    report.append("```")
    report.append("")
    report.append("### Reseed Data (if lost):")
    report.append("```bash")
    report.append("cd ~/dev/er-simulator-superrepo")
    report.append("source .venv/bin/activate")
    report.append("python memory/seed_acontext.py")
    report.append("```")
    report.append("")
    report.append("### Restart All Containers:")
    report.append("```bash")
    report.append("cd ~/dev/er-simulator-superrepo")
    report.append("./scripts/context-dna restart")
    report.append("```")
    report.append("")

    # 9. Useful Links
    report.append("## USEFUL LINKS")
    report.append("-" * 40)
    report.append("- Dashboard: http://localhost:3000")
    report.append("- API Docs: http://localhost:8029/swagger/index.html")
    report.append("- API Health: http://localhost:8029/health")
    report.append("- Core Health: http://localhost:8019/health")
    report.append("")
    report.append("### GitHub & Documentation:")
    report.append("- Context DNA GitHub: https://github.com/memodb-io/Acontext")
    report.append("- Context DNA Docs: https://docs.acontext.io")
    report.append("- Python SDK: https://pypi.org/project/acontext/")
    report.append("- Discord Support: https://discord.acontext.io")
    report.append("")

    # 10. Setup Info
    report.append("## HOW THIS WAS SET UP")
    report.append("-" * 40)
    report.append("""
Context DNA is a rebrand of the Acontext project (Apache 2.0 licensed):

1. Start services:
   cd ~/dev/er-simulator-superrepo
   ./scripts/context-dna up

2. Install Python SDK:
   pip install acontext

3. Seed initial learnings:
   python memory/seed_acontext.py

Key files:
- ~/dev/er-simulator-superrepo/context-dna/infra/docker-compose.yaml (container config)
- ~/dev/er-simulator-superrepo/context-dna/infra/.env (LLM config)
- ~/dev/er-simulator-superrepo/scripts/context-dna (CLI wrapper)
- ~/dev/er-simulator-superrepo/memory/context_dna_client.py (Python client)
- ~/dev/er-simulator-superrepo/memory/query.py (CLI query tool)
- ~/Library/Application Support/xbar/plugins/context-dna.2m.sh (menu bar)
""")
    report.append("")
    report.append("=" * 70)
    report.append("END OF REPORT")
    report.append("=" * 70)

    return '\n'.join(report)


def main():
    quick = '--quick' in sys.argv
    copy = '--copy' in sys.argv

    report = generate_report(quick=quick)

    if copy:
        # Copy to clipboard on macOS
        try:
            subprocess.run(['pbcopy'], input=report.encode(), check=True)
            print(f"{GREEN}✅ Report copied to clipboard!{RESET}")
            print(f"\n{YELLOW}Paste this to your AI assistant for help debugging.{RESET}\n")
        except Exception:
            print(report)
            print(f"\n{YELLOW}(Could not copy to clipboard automatically){RESET}")
    else:
        print(report)

    # Print copy hint
    if not copy:
        print(f"\n{BLUE}TIP: Run with --copy to copy report to clipboard:{RESET}")
        print(f"     python memory/troubleshoot.py --copy")


if __name__ == "__main__":
    main()
