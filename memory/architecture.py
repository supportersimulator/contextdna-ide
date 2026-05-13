#!/usr/bin/env python3
"""
Architecture Memory Layer for ER Simulator

This module provides secrets-safe architecture documentation storage via Context DNA.
Architecture details are stored in Context DNA's semantic search, making them queryable
by agents before they interact with any system component.

SECRETS PROTECTION:
- All architecture data is stored in Context DNA (which is gitignored)
- No hardcoded IPs, credentials, or instance IDs in code
- Agents query architecture at runtime, getting current values from Context DNA

Usage:
    from memory.architecture import ArchitectureMemory

    # Initialize
    arch = ArchitectureMemory()

    # Record deployment info (done once, updated as architecture changes)
    arch.record_deployment(
        service="django-backend",
        instance_id="i-0b60414d5de76d320",
        region="us-west-2",
        app_path="/var/www/ersim/app",
        runtime="python3.11",
        env_vars={"DJANGO_SETTINGS_MODULE": "ersim_backend.settings.prod", "HOME": "/root"},
        notes="No venv - system Python. HOME=/root needed for git operations."
    )

    # Query before deploying
    info = arch.get_deployment_info("django-backend")

    # Get architecture context for any area
    context = arch.get_architecture_context("voice stack")

CLI:
    # Record deployment
    python memory/architecture.py record-deployment django-backend --region us-west-2

    # Query deployment
    python memory/architecture.py get-deployment django-backend

    # Get full architecture
    python memory/architecture.py get-architecture

    # Get context for a specific area
    python memory/architecture.py context "voice stack agent"
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False


# Architecture categories for semantic organization
ARCHITECTURE_CATEGORIES = {
    "deployment": "Deployment and infrastructure details",
    "service": "Service configurations and connections",
    "protocol": "Development protocols and workflows",
    "layout": "Repository structure and organization",
    "pipeline": "Data and processing pipelines",
    "api": "API endpoints and integrations",
    "budget": "Cost and budget information",
}


class ArchitectureMemory:
    """
    Secrets-safe architecture documentation via Context DNA.

    All architecture data is stored in Context DNA's semantic database,
    which is gitignored and not committed to version control.
    """

    def __init__(self):
        if not CONTEXT_DNA_AVAILABLE:
            raise RuntimeError("Context DNA not available. Install with: pip install acontext")

        try:
            self.memory = ContextDNAClient()
            if not self.memory.ping():
                raise RuntimeError("Context DNA server not running. Start with: ~/.acontext/bin/acontext docker up -d")
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Context DNA: {e}")

    def record_deployment(
        self,
        service: str,
        instance_id: str = None,
        region: str = None,
        app_path: str = None,
        runtime: str = None,
        env_vars: dict = None,
        ports: dict = None,
        health_check: str = None,
        notes: str = None,
        tags: list[str] = None
    ):
        """
        Record deployment architecture for a service.

        Args:
            service: Service name (e.g., "django-backend", "voice-stt", "livekit-server")
            instance_id: AWS instance ID or container ID
            region: AWS region
            app_path: Path to application on the instance
            runtime: Runtime environment (e.g., "python3.11", "node18")
            env_vars: Critical environment variables (NO SECRETS - only config names)
            ports: Port mappings {"http": 8000, "ws": 8089}
            health_check: Health check endpoint
            notes: Important deployment notes
            tags: Keywords for search
        """
        content = f"""## Deployment: {service}

**Service:** {service}
**Last Updated:** {datetime.now().isoformat()}

### Infrastructure
"""
        if instance_id:
            content += f"- **Instance ID:** {instance_id}\n"
        if region:
            content += f"- **Region:** {region}\n"
        if app_path:
            content += f"- **App Path:** {app_path}\n"
        if runtime:
            content += f"- **Runtime:** {runtime}\n"
        if health_check:
            content += f"- **Health Check:** {health_check}\n"

        if ports:
            content += "\n### Ports\n"
            for name, port in ports.items():
                content += f"- **{name}:** {port}\n"

        if env_vars:
            content += "\n### Environment Variables (config, not secrets)\n"
            for key, value in env_vars.items():
                content += f"- `{key}={value}`\n"

        if notes:
            content += f"\n### Notes\n{notes}\n"

        content += f"\n**Tags:** deployment, {service}, {', '.join(tags or [])}"

        # Store as architecture decision for semantic retrieval
        self.memory.record_architecture_decision(
            decision=f"Deployment configuration for {service}",
            rationale=content,
            alternatives=None,
            consequences=f"Query 'deployment {service}' to retrieve this configuration"
        )

        print(f"   Recorded deployment: {service}")
        return True

    def record_service(
        self,
        service: str,
        description: str,
        location: str,
        depends_on: list[str] = None,
        endpoints: dict = None,
        notes: str = None,
        tags: list[str] = None
    ):
        """
        Record service architecture.

        Args:
            service: Service name
            description: What this service does
            location: Where it runs (e.g., "GPU instance", "CPU instance", "ECS")
            depends_on: Services this depends on
            endpoints: API endpoints {"transcribe": "POST /api/transcribe"}
            notes: Important notes
            tags: Keywords for search
        """
        content = f"""## Service: {service}

**Description:** {description}
**Location:** {location}
**Last Updated:** {datetime.now().isoformat()}
"""

        if depends_on:
            content += "\n### Dependencies\n"
            for dep in depends_on:
                content += f"- {dep}\n"

        if endpoints:
            content += "\n### Endpoints\n"
            for name, endpoint in endpoints.items():
                content += f"- **{name}:** `{endpoint}`\n"

        if notes:
            content += f"\n### Notes\n{notes}\n"

        content += f"\n**Tags:** service, {service}, {', '.join(tags or [])}"

        self.memory.record_architecture_decision(
            decision=f"Service architecture for {service}",
            rationale=content,
            alternatives=None,
            consequences=f"Query 'service {service}' to retrieve this configuration"
        )

        print(f"   Recorded service: {service}")
        return True

    def record_protocol(
        self,
        name: str,
        description: str,
        steps: list[str],
        guardrails: list[str] = None,
        notes: str = None,
        tags: list[str] = None
    ):
        """
        Record a development protocol or workflow.

        Args:
            name: Protocol name (e.g., "v0-dev-sync", "django-deployment")
            description: What this protocol is for
            steps: Ordered list of steps
            guardrails: Safety rules / things to avoid
            notes: Additional notes
            tags: Keywords for search
        """
        content = f"""## Protocol: {name}

**Description:** {description}
**Last Updated:** {datetime.now().isoformat()}

### Steps
"""
        for i, step in enumerate(steps, 1):
            content += f"{i}. {step}\n"

        if guardrails:
            content += "\n### Guardrails (MUST follow)\n"
            for rule in guardrails:
                content += f"- {rule}\n"

        if notes:
            content += f"\n### Notes\n{notes}\n"

        content += f"\n**Tags:** protocol, {name}, {', '.join(tags or [])}"

        self.memory.record_architecture_decision(
            decision=f"Protocol: {name}",
            rationale=content,
            alternatives=None,
            consequences=f"Query 'protocol {name}' to retrieve this workflow"
        )

        print(f"   Recorded protocol: {name}")
        return True

    def record_layout(
        self,
        name: str,
        description: str,
        structure: dict,
        notes: str = None,
        tags: list[str] = None
    ):
        """
        Record repository or system layout.

        Args:
            name: Layout name (e.g., "superrepo", "voice-stack")
            description: What this layout represents
            structure: Dict of paths to descriptions
            notes: Additional notes
            tags: Keywords for search
        """
        content = f"""## Layout: {name}

**Description:** {description}
**Last Updated:** {datetime.now().isoformat()}

### Structure
```
"""
        for path, desc in structure.items():
            content += f"{path}  # {desc}\n"

        content += "```\n"

        if notes:
            content += f"\n### Notes\n{notes}\n"

        content += f"\n**Tags:** layout, {name}, {', '.join(tags or [])}"

        self.memory.record_architecture_decision(
            decision=f"Layout: {name}",
            rationale=content,
            alternatives=None,
            consequences=f"Query 'layout {name}' to retrieve this structure"
        )

        print(f"   Recorded layout: {name}")
        return True

    def record_pipeline(
        self,
        name: str,
        description: str,
        stages: list[dict],
        notes: str = None,
        tags: list[str] = None
    ):
        """
        Record a processing pipeline.

        Args:
            name: Pipeline name (e.g., "voice-pipeline", "simulation-flow")
            description: What this pipeline does
            stages: List of {"name": "STT", "service": "whisper", "input": "audio", "output": "text"}
            notes: Additional notes
            tags: Keywords for search
        """
        content = f"""## Pipeline: {name}

**Description:** {description}
**Last Updated:** {datetime.now().isoformat()}

### Stages
"""
        for i, stage in enumerate(stages, 1):
            content += f"\n**Stage {i}: {stage.get('name', 'Unknown')}**\n"
            if stage.get('service'):
                content += f"- Service: {stage['service']}\n"
            if stage.get('input'):
                content += f"- Input: {stage['input']}\n"
            if stage.get('output'):
                content += f"- Output: {stage['output']}\n"
            if stage.get('location'):
                content += f"- Location: {stage['location']}\n"
            if stage.get('notes'):
                content += f"- Notes: {stage['notes']}\n"

        if notes:
            content += f"\n### Notes\n{notes}\n"

        content += f"\n**Tags:** pipeline, {name}, {', '.join(tags or [])}"

        self.memory.record_architecture_decision(
            decision=f"Pipeline: {name}",
            rationale=content,
            alternatives=None,
            consequences=f"Query 'pipeline {name}' to retrieve this flow"
        )

        print(f"   Recorded pipeline: {name}")
        return True

    def record_api(
        self,
        name: str,
        base_url: str,
        endpoints: list[dict],
        auth: str = None,
        notes: str = None,
        tags: list[str] = None
    ):
        """
        Record API documentation.

        Args:
            name: API name (e.g., "ersim-backend", "voice-tts")
            base_url: Base URL (can be internal DNS or description)
            endpoints: List of {"method": "POST", "path": "/api/foo", "description": "..."}
            auth: Authentication method (e.g., "Bearer token from /ersim/prod/...")
            notes: Additional notes
            tags: Keywords for search
        """
        content = f"""## API: {name}

**Base URL:** {base_url}
**Last Updated:** {datetime.now().isoformat()}

"""
        if auth:
            content += f"**Authentication:** {auth}\n\n"

        content += "### Endpoints\n"
        for ep in endpoints:
            content += f"\n**{ep.get('method', 'GET')} {ep.get('path', '/')}**\n"
            if ep.get('description'):
                content += f"  {ep['description']}\n"
            if ep.get('request'):
                content += f"  Request: {ep['request']}\n"
            if ep.get('response'):
                content += f"  Response: {ep['response']}\n"

        if notes:
            content += f"\n### Notes\n{notes}\n"

        content += f"\n**Tags:** api, {name}, {', '.join(tags or [])}"

        self.memory.record_architecture_decision(
            decision=f"API: {name}",
            rationale=content,
            alternatives=None,
            consequences=f"Query 'api {name}' to retrieve this documentation"
        )

        print(f"   Recorded API: {name}")
        return True

    def record_budget(
        self,
        category: str,
        items: list[dict],
        total_monthly: float = None,
        notes: str = None,
        tags: list[str] = None
    ):
        """
        Record budget information.

        Args:
            category: Budget category (e.g., "aws-infrastructure", "api-costs")
            items: List of {"name": "g5.xlarge", "cost": 1200, "unit": "month"}
            total_monthly: Total monthly cost
            notes: Additional notes
            tags: Keywords for search
        """
        content = f"""## Budget: {category}

**Category:** {category}
**Last Updated:** {datetime.now().isoformat()}

### Items
"""
        for item in items:
            cost = item.get('cost', 0)
            unit = item.get('unit', 'month')
            content += f"- **{item.get('name', 'Unknown')}:** ${cost}/{unit}\n"

        if total_monthly:
            content += f"\n**Total Monthly:** ${total_monthly}\n"

        if notes:
            content += f"\n### Notes\n{notes}\n"

        content += f"\n**Tags:** budget, {category}, {', '.join(tags or [])}"

        self.memory.record_architecture_decision(
            decision=f"Budget: {category}",
            rationale=content,
            alternatives=None,
            consequences=f"Query 'budget {category}' to retrieve this information"
        )

        print(f"   Recorded budget: {category}")
        return True

    def get_architecture_context(self, query: str, limit: int = 5) -> str:
        """
        Get architecture context for a specific query.

        Args:
            query: What you're looking for (e.g., "voice stack", "django deployment")
            limit: Max results

        Returns:
            Formatted architecture context for prompt injection
        """
        learnings = self.memory.get_relevant_learnings(query, limit=limit)

        if not learnings:
            return f"# No architecture documentation found for: {query}\n"

        output = [
            "# ARCHITECTURE CONTEXT",
            f"# Query: {query}",
            "",
            "## Relevant Architecture Documentation:",
            ""
        ]

        for i, learning in enumerate(learnings, 1):
            if learning.get("distance", 1.0) < 0.6:  # Only relevant results
                output.append(f"### {i}. {learning.get('title', 'Architecture')}")
                if learning.get('preferences'):
                    # This contains the full architecture documentation
                    pref = learning['preferences']
                    output.append(pref[:1500] if len(pref) > 1500 else pref)
                output.append("")

        output.append("---")
        output.append("# END ARCHITECTURE CONTEXT")

        return "\n".join(output)

    def get_deployment_info(self, service: str) -> str:
        """Get deployment info for a specific service."""
        return self.get_architecture_context(f"deployment {service}")

    def get_service_info(self, service: str) -> str:
        """Get service info for a specific service."""
        return self.get_architecture_context(f"service {service}")

    def get_protocol(self, name: str) -> str:
        """Get protocol by name."""
        return self.get_architecture_context(f"protocol {name}")

    def get_pipeline(self, name: str) -> str:
        """Get pipeline by name."""
        return self.get_architecture_context(f"pipeline {name}")

    def record_manual(
        self,
        title: str,
        content: str,
        category: str = "general",
        tags: list[str] = None
    ):
        """
        Manually record any architecture detail.

        This is the catch-all method for recording architecture information
        that doesn't fit into the predefined categories. Use this when:
        - Recording one-off details
        - Adding custom documentation
        - Capturing learnings from infrastructure work
        - Adding notes about gotchas or quirks

        Args:
            title: Title of the architecture entry
            content: Full content/documentation (markdown supported)
            category: Category for organization (deployment, service, protocol,
                     layout, pipeline, api, budget, wiring, gotcha, or custom)
            tags: Keywords for search

        Examples:
            arch.record_manual(
                title="Docker env reload gotcha",
                content="docker restart does NOT reload environment variables...",
                category="gotcha",
                tags=["docker", "env", "ecs"]
            )

            arch.record_manual(
                title="GPU Toggle Lambda wiring",
                content="Lambda → ASG → EC2 → ECS → Services...",
                category="wiring",
                tags=["gpu", "lambda", "asg", "toggle"]
            )
        """
        full_content = f"""## {title}

**Category:** {category}
**Last Updated:** {datetime.now().isoformat()}

{content}

**Tags:** {category}, {', '.join(tags or ['manual'])}
"""

        self.memory.record_architecture_decision(
            decision=f"[{category.upper()}] {title}",
            rationale=full_content,
            alternatives=None,
            consequences=f"Query '{category} {title}' or any of the tags to retrieve this"
        )

        print(f"   Recorded [{category}]: {title}")
        return True


def seed_architecture():
    """Seed the architecture memory with initial documentation."""
    print("Seeding architecture documentation...")

    arch = ArchitectureMemory()

    # Django Backend Deployment
    arch.record_deployment(
        service="django-backend",
        instance_id="i-0b60414d5de76d320",
        region="us-west-2",
        app_path="/var/www/ersim/app",
        runtime="python3.11 (system, no venv)",
        env_vars={
            "DJANGO_SETTINGS_MODULE": "ersim_backend.settings.prod",
            "HOME": "/root"
        },
        health_check="/api/health/",
        notes="""CRITICAL: No virtualenv - uses system Python 3.11.
HOME=/root is required for git operations (git config).
Restart: sudo systemctl restart gunicorn
Logs: sudo journalctl -u gunicorn -f
Deploy: cd /var/www/ersim/app && git pull && sudo systemctl restart gunicorn""",
        tags=["backend", "api", "django", "gunicorn"]
    )

    # Voice Stack - GPU Instance
    arch.record_deployment(
        service="voice-gpu",
        instance_id="voice.ersimulator.com (ECS)",
        region="us-west-2",
        runtime="Docker on g5.xlarge",
        ports={"stt": 8081, "tts-http": 8000, "tts-ws": 8089, "llm": 8090},
        health_check="/health on each port",
        notes="""GPU inference services (STT, TTS, LLM).
Cost: ~$1,200/month for g5.xlarge.
Access via Internal NLB for security.""",
        tags=["voice", "gpu", "inference", "stt", "tts", "llm"]
    )

    # LiveKit CPU Instance
    arch.record_deployment(
        service="livekit-server",
        instance_id="livekit.ersimulator.com",
        region="us-west-2",
        runtime="Docker on c6i.large",
        ports={"http": 7880, "rtc": 7881, "turn": 3478, "turns": 5349, "udp": "50000-60000"},
        notes="""LiveKit Server + Agent on CPU instance.
CRITICAL: DNS must NOT be Cloudflare proxied (WebRTC needs direct IP).
Agent connects to GPU services via Internal NLB.
Cost: ~$62/month for c6i.large.""",
        tags=["livekit", "webrtc", "voice", "agent", "cpu"]
    )

    # v0-dev Sync Protocol
    arch.record_protocol(
        name="v0-dev-sync",
        description="Protocol for syncing v0.dev changes with admin.ersimulator.com",
        steps=[
            "Make changes in v0.dev project",
            "Export/download the generated code",
            "Copy relevant components to admin.ersimulator.com/src/",
            "Adapt imports and styling to match existing patterns",
            "Test locally with pnpm dev",
            "Commit with descriptive message"
        ],
        guardrails=[
            "NEVER auto-merge v0 output without review",
            "Always check for hardcoded values that need env vars",
            "Preserve existing component patterns (we use shadcn/ui)",
            "Keep LiveKit integration code separate from v0 changes"
        ],
        notes="v0.dev is for rapid prototyping. Production code lives in admin.ersimulator.com git submodule.",
        tags=["v0", "frontend", "admin", "workflow"]
    )

    # Superrepo Layout
    arch.record_layout(
        name="superrepo",
        description="ER Simulator monorepo structure",
        structure={
            "admin.ersimulator.com/": "Next.js admin dashboard (git submodule)",
            "backend/": "Django REST API",
            "ersim-voice-stack/": "Voice AI services (STT, TTS, LLM, Agent)",
            "infra/aws/terraform/": "AWS infrastructure as code",
            "memory/": "Context DNA memory system (this!)",
            "mobile/": "React Native mobile app",
            "simulator-core/": "Core simulation logic",
            "landing-page/": "Marketing site (git submodule)",
        },
        notes="""admin.ersimulator.com and landing-page are git submodules.
Run: git submodule update --init --recursive after clone.""",
        tags=["structure", "monorepo", "layout"]
    )

    # Voice Pipeline
    arch.record_pipeline(
        name="voice-pipeline",
        description="Speech-to-speech AI pipeline for ER simulations",
        stages=[
            {
                "name": "Audio Capture",
                "service": "LiveKit SDK (browser)",
                "input": "Microphone audio",
                "output": "WebRTC audio stream",
                "location": "User browser",
                "notes": "48kHz WebRTC audio"
            },
            {
                "name": "STT",
                "service": "Whisper (faster-whisper)",
                "input": "Audio stream",
                "output": "Transcribed text",
                "location": "GPU instance (port 8081)",
                "notes": "Streaming transcription"
            },
            {
                "name": "LLM",
                "service": "Bedrock Claude",
                "input": "Transcribed text + scenario context",
                "output": "AI response text",
                "location": "GPU instance proxy (port 8090)",
                "notes": "Uses asyncio.to_thread() to avoid blocking"
            },
            {
                "name": "TTS",
                "service": "Kyutai Moshi",
                "input": "Response text",
                "output": "Audio stream",
                "location": "GPU instance (port 8089 WS)",
                "notes": "KYUTAI_TTS_SAMPLE_RATE=24000"
            },
            {
                "name": "Audio Playback",
                "service": "LiveKit SDK (browser)",
                "input": "Audio stream",
                "output": "Speaker audio",
                "location": "User browser"
            }
        ],
        notes="""CRITICAL: Agent runs on CPU instance, NOT GPU instance.
Agent orchestrates the pipeline via Internal NLB to GPU services.
WebRTC handles bidirectional audio streaming.""",
        tags=["voice", "pipeline", "stt", "llm", "tts", "webrtc"]
    )

    # Budget
    arch.record_budget(
        category="aws-infrastructure",
        items=[
            {"name": "g5.xlarge (GPU)", "cost": 1200, "unit": "month"},
            {"name": "c6i.large (LiveKit CPU)", "cost": 62, "unit": "month"},
            {"name": "t3.medium (Django)", "cost": 30, "unit": "month"},
            {"name": "Internal NLB", "cost": 18, "unit": "month"},
            {"name": "Data transfer", "cost": 50, "unit": "month"},
        ],
        total_monthly=1360,
        notes="GPU is the primary cost driver. Consider spot instances for non-production.",
        tags=["cost", "aws", "infrastructure"]
    )

    print("\nArchitecture seeding complete!")
    print("Query with: python memory/architecture.py context 'voice pipeline'")


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Architecture Memory CLI")
        print("")
        print("Commands:")
        print("  seed                    - Seed initial architecture documentation")
        print("  context <query>         - Get architecture context for a query")
        print("  deployment <service>    - Get deployment info for a service")
        print("  service <name>          - Get service info")
        print("  protocol <name>         - Get protocol by name")
        print("  pipeline <name>         - Get pipeline by name")
        print("  record <title> <category> <content> [tags...]  - Manual recording")
        print("")
        print("Categories for manual recording:")
        print("  deployment, service, protocol, layout, pipeline, api, budget, wiring, gotcha, general")
        print("")
        print("Examples:")
        print("  python architecture.py seed")
        print("  python architecture.py context 'voice stack agent location'")
        print("  python architecture.py deployment django-backend")
        print("  python architecture.py protocol v0-dev-sync")
        print("  python architecture.py record 'NLB timeout gotcha' gotcha 'Internal NLB has 60s idle timeout' nlb timeout voice")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "seed":
        seed_architecture()

    elif cmd == "context":
        if len(sys.argv) < 3:
            print("Usage: context <query>")
            sys.exit(1)
        arch = ArchitectureMemory()
        print(arch.get_architecture_context(" ".join(sys.argv[2:])))

    elif cmd == "deployment":
        if len(sys.argv) < 3:
            print("Usage: deployment <service>")
            sys.exit(1)
        arch = ArchitectureMemory()
        print(arch.get_deployment_info(sys.argv[2]))

    elif cmd == "service":
        if len(sys.argv) < 3:
            print("Usage: service <name>")
            sys.exit(1)
        arch = ArchitectureMemory()
        print(arch.get_service_info(sys.argv[2]))

    elif cmd == "protocol":
        if len(sys.argv) < 3:
            print("Usage: protocol <name>")
            sys.exit(1)
        arch = ArchitectureMemory()
        print(arch.get_protocol(sys.argv[2]))

    elif cmd == "pipeline":
        if len(sys.argv) < 3:
            print("Usage: pipeline <name>")
            sys.exit(1)
        arch = ArchitectureMemory()
        print(arch.get_pipeline(sys.argv[2]))

    elif cmd == "record" or cmd == "manual":
        if len(sys.argv) < 5:
            print("Usage: record <title> <category> <content> [tags...]")
            print("")
            print("Categories: deployment, service, protocol, layout, pipeline, api, budget, wiring, gotcha, general")
            print("")
            print("Example:")
            print("  python architecture.py record 'Docker env gotcha' gotcha 'docker restart does NOT reload env vars' docker ecs")
            sys.exit(1)
        arch = ArchitectureMemory()
        title = sys.argv[2]
        category = sys.argv[3]
        content = sys.argv[4]
        tags = sys.argv[5:] if len(sys.argv) > 5 else None
        arch.record_manual(title=title, content=content, category=category, tags=tags)
        print(f"\nRecorded. Query with: python architecture.py context '{category} {title}'")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
