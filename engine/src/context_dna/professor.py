#!/usr/bin/env python3
"""
PROFESSOR - Nobel-Prize Level Context Empowerment

Not a data dump. Not a raw copy.

This is DISTILLED WISDOM - the kind a world-class professor gives a student:
- Exactly what you need to know (no more, no less)
- The insight you didn't know you needed
- The landmine you'd step on without knowing
- The pattern that separates competent from excellent

The professor doesn't give you the textbook. They give you:
1. THE ONE THING that will make or break this task
2. THE PATTERN that veterans use but never write down
3. THE MISTAKE that costs hours when you make it
4. THE CONTEXT that transforms confusion into clarity

Usage:
    from context_dna.professor import consult

    # Before ANY significant work
    wisdom = consult("configure database migrations")
    # Returns: Exactly what a Nobel-prize professor would tell you

---

CONTEXT DNA ADAPTATION:

This is an adaptive version of the Professor that:
1. Starts with general programming wisdom (built-in domains)
2. LEARNS from the user's project to build project-specific wisdom
3. Queries the storage backend for relevant learnings
4. Combines built-in + learned wisdom for comprehensive guidance

The professor evolves with your project - the more you use Context DNA,
the smarter the professor becomes about YOUR codebase.
"""

import re
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from datetime import datetime


# =============================================================================
# PROFESSOR'S KNOWLEDGE BASE - Universal Programming Wisdom
# =============================================================================

# The professor's DISTILLED WISDOM - not raw data, but insights
# Each entry is: domain -> { first_principle, the_one_thing, landmines, pattern, context }

PROFESSOR_WISDOM = {
    "async_python": {
        "first_principle": """
            Python's asyncio is COOPERATIVE multitasking. The event loop can only
            switch tasks at 'await' points. A synchronous call is like a student
            who won't stop talking - everyone waits.
        """,
        "the_one_thing": """
            Libraries like requests, boto3, and file I/O are SYNCHRONOUS.
            They block your entire event loop. The fix is always:
                await asyncio.to_thread(blocking_function, *args)
            This runs the sync code in a thread pool, freeing the event loop.
        """,
        "landmines": [
            "Most AWS SDKs are sync - wrap in asyncio.to_thread() or use aioboto3.",
            "File operations (open, write) look fast but block. Always to_thread() in async code.",
            "Don't mix sync and async carelessly - one sync call can freeze your entire service.",
        ],
        "pattern": """
            VETERAN PATTERN for async services:
            1. Identify ALL I/O operations (network, file, subprocess)
            2. Wrap each in asyncio.to_thread() or use async-native library
            3. Test by watching event loop latency, not just correctness
            4. Profile with py-spy to find hidden sync calls
        """,
        "context": """
            Affects: API endpoints, WebSocket handlers, background tasks.
            A 14-second LLM call blocking the event loop means 14 seconds of
            unresponsiveness - users will think it crashed.
        """,
    },

    "docker": {
        "first_principle": """
            Docker containers are immutable artifacts. Environment comes from outside.
            Think of containers as functions, orchestrators as the scheduler.
            Build once, run anywhere - but configuration varies.
        """,
        "the_one_thing": """
            `docker restart` does NOT reload --env-file. The old environment persists.
            You must: docker stop → docker rm → docker run
            Or use: docker-compose up -d --force-recreate
            This catches 80% of "my env var change didn't work" issues.
        """,
        "landmines": [
            "Container logs show last state before crash - check EARLIER logs for root cause.",
            "HEALTHCHECK failures can cause restart loops - verify health endpoint works.",
            "Volume mounts override container contents - order matters.",
        ],
        "pattern": """
            VETERAN DEPLOYMENT PATTERN:
            1. docker-compose up -d --force-recreate (ensures fresh containers)
            2. Watch docker logs -f for startup errors
            3. Wait for health checks (don't trust "running" status alone)
            4. Test the actual endpoint, not just container status
        """,
        "context": """
            Development vs production: Dev uses local volumes for hot reload,
            production uses built images. Never mount source code in production.
        """,
    },

    "database": {
        "first_principle": """
            Databases are the source of truth. Migrations are code.
            Schema changes are irreversible without data loss.
            Always backup before migrations. Always.
        """,
        "the_one_thing": """
            Run migrations BEFORE deploying new code, not after.
            The old code must work with the new schema during rollout.
            Add columns as nullable first, then make required later.
        """,
        "landmines": [
            "DROP COLUMN loses data forever - add to backup checklist.",
            "Index creation on large tables locks the table - use CONCURRENTLY in Postgres.",
            "Foreign keys can cause cascade deletes - verify ON DELETE behavior.",
        ],
        "pattern": """
            VETERAN MIGRATION PATTERN:
            1. Backup database
            2. Test migration on copy of prod data
            3. Run migration
            4. Deploy new code
            5. Verify everything works
            6. Keep backup for 7 days
        """,
        "context": """
            ORM migrations (Django, Alembic, Prisma) are reversible in theory,
            but data migrations often aren't. Treat migrations as one-way streets.
        """,
    },

    "api_design": {
        "first_principle": """
            APIs are contracts. Breaking changes break clients.
            Versioning is mandatory. Documentation is code.
            Errors should be helpful, not cryptic.
        """,
        "the_one_thing": """
            Always return consistent error formats:
            { "error": "message", "code": "ERROR_CODE", "details": {...} }
            Clients can handle errors they understand.
            500 errors should NEVER expose internal details.
        """,
        "landmines": [
            "Changing response structure breaks clients silently - use versioning.",
            "Rate limiting without headers frustrates developers - include X-RateLimit-*.",
            "Pagination without cursors fails at scale - offset pagination is O(n).",
        ],
        "pattern": """
            VETERAN API PATTERN:
            1. Design endpoints around resources, not actions
            2. Use HTTP methods correctly (GET reads, POST creates, etc.)
            3. Return appropriate status codes (201 Created, 404 Not Found)
            4. Include HATEOAS links for discoverability
            5. Document everything with OpenAPI/Swagger
        """,
        "context": """
            Internal APIs can break compatibility with coordination.
            Public APIs must maintain backwards compatibility forever (or version).
        """,
    },

    "git": {
        "first_principle": """
            Git is a content-addressable filesystem with a VCS on top.
            Commits are immutable snapshots. Branches are just pointers.
            The reflog is your safety net - nothing is truly lost.
        """,
        "the_one_thing": """
            Never force push to main/master. Ever.
            If you need to fix history, create a new branch.
            Force push to feature branches only after team agreement.
        """,
        "landmines": [
            "git reset --hard destroys uncommitted work - stash first.",
            "Rebasing public branches causes duplicate commits for others.",
            "Large files in history bloat the repo forever - use git-lfs.",
        ],
        "pattern": """
            VETERAN GIT PATTERN:
            1. Small, focused commits with clear messages
            2. Branch from latest main, rebase before merge
            3. Squash WIP commits before PR
            4. Use conventional commits (feat:, fix:, docs:)
            5. Review diff before every commit
        """,
        "context": """
            Monorepos need careful CI/CD - only build what changed.
            Submodules are tricky - consider subtrees or separate repos.
        """,
    },

    "testing": {
        "first_principle": """
            Tests are specifications that run. They document behavior.
            Fast tests run often. Slow tests get skipped.
            Flaky tests are worse than no tests - they erode trust.
        """,
        "the_one_thing": """
            Test behavior, not implementation.
            Ask: "What should this do?" not "How does it work?"
            If refactoring breaks tests, the tests were too coupled.
        """,
        "landmines": [
            "Mocking everything makes tests pass but bugs ship - use real dependencies when feasible.",
            "Time-dependent tests flake on CI - mock datetime or use freezegun.",
            "Shared test state causes order-dependent failures - isolate each test.",
        ],
        "pattern": """
            VETERAN TESTING PATTERN:
            1. Unit tests for business logic (fast, isolated)
            2. Integration tests for boundaries (DB, APIs)
            3. E2E tests for critical paths only (slow but comprehensive)
            4. Run unit tests on every commit, others on PR
            5. Track flaky tests and fix or delete them
        """,
        "context": """
            Coverage is a metric, not a goal. 100% coverage with bad tests is worse
            than 70% coverage with thoughtful tests.
        """,
    },

    "security": {
        "first_principle": """
            Never trust user input. Validate everything.
            Secrets belong in environment variables, not code.
            The principle of least privilege applies everywhere.
        """,
        "the_one_thing": """
            NEVER interpolate user input into SQL, HTML, or shell commands.
            Use parameterized queries, HTML escaping, and subprocess arrays.
            This single rule prevents SQL injection, XSS, and command injection.
        """,
        "landmines": [
            "Logging user data can leak PII - sanitize logs.",
            "JWT tokens without expiration are permanent access grants.",
            "CORS * in production allows any site to call your API.",
        ],
        "pattern": """
            VETERAN SECURITY PATTERN:
            1. Use established auth libraries (don't roll your own)
            2. Hash passwords with bcrypt/argon2 (never MD5/SHA1)
            3. Validate all input on the server (client validation is UX)
            4. Use HTTPS everywhere (Let's Encrypt is free)
            5. Audit dependencies for known vulnerabilities
        """,
        "context": """
            Security is a process, not a feature. Regular audits, dependency
            updates, and penetration testing are ongoing requirements.
        """,
    },

    "performance": {
        "first_principle": """
            Measure before optimizing. Premature optimization is the root of evil.
            The bottleneck is rarely where you think it is.
            Profile production, not just development.
        """,
        "the_one_thing": """
            Database queries are the #1 performance killer in web apps.
            N+1 queries, missing indexes, and full table scans are the usual suspects.
            EXPLAIN ANALYZE is your friend.
        """,
        "landmines": [
            "Caching invalid data is worse than slow data - invalidation is hard.",
            "Premature async makes code complex without actual benefit - measure first.",
            "CDN caching static assets is easy win - do it early.",
        ],
        "pattern": """
            VETERAN PERFORMANCE PATTERN:
            1. Add observability first (APM, logging, metrics)
            2. Identify the actual bottleneck with data
            3. Optimize the bottleneck only
            4. Verify improvement with benchmarks
            5. Monitor for regressions
        """,
        "context": """
            User-perceived performance matters more than server metrics.
            A 200ms API that feels instant beats a 100ms API with 2s JS bundle.
        """,
    },

    "debugging": {
        "first_principle": """
            Bugs are logic errors - the code does what you wrote, not what you meant.
            Read the error message. Really read it.
            The bug is in the code you just changed.
        """,
        "the_one_thing": """
            Reproduce first, then fix. If you can't reproduce it,
            you can't verify the fix. Add logging to capture state
            at the point of failure.
        """,
        "landmines": [
            "Fixing symptoms masks the root cause - dig deeper.",
            "print() debugging is fine - don't let anyone shame you.",
            "Production bugs need production-like environments to reproduce.",
        ],
        "pattern": """
            VETERAN DEBUGGING PATTERN:
            1. Read the full error message and stack trace
            2. Reproduce the bug reliably
            3. Minimize the test case
            4. Form hypothesis, test it
            5. Fix and verify
            6. Add test to prevent regression
        """,
        "context": """
            Rubber duck debugging works. Explaining the problem often reveals
            the solution. Write the question before asking for help.
        """,
    },

    "deployment": {
        "first_principle": """
            If it's not in version control, it doesn't exist.
            Infrastructure should be code. Manual changes drift.
            Rollback must always be possible.
        """,
        "the_one_thing": """
            Deploy the same artifact to all environments.
            Build once, deploy many. Configuration varies, code doesn't.
            If it works in staging, it will work in production (with same config).
        """,
        "landmines": [
            "Friday deploys ruin weekends - deploy early in the week.",
            "Deploying without rollback plan is gambling.",
            "Database migrations are the riskiest part - separate them.",
        ],
        "pattern": """
            VETERAN DEPLOYMENT PATTERN:
            1. Automated CI/CD pipeline (no manual steps)
            2. Feature flags for risky changes
            3. Canary/blue-green for zero-downtime
            4. Automated rollback on error rate spike
            5. Post-deploy smoke tests
        """,
        "context": """
            The goal is boring deploys. If deployments are stressful,
            deploy more often with smaller changes.
        """,
    },
}

# Domain detection keywords - EXPANDED for better relevance detection (80%+ accuracy target)
DOMAIN_KEYWORDS = {
    # Core technical domains
    "async_python": [
        "async", "asyncio", "await", "to_thread", "event loop", "blocking", "coroutine",
        "aiohttp", "aioboto", "concurrent", "threading", "multiprocessing", "gather",
        "create_task", "run_until_complete", "asynccontextmanager"
    ],
    "docker_ecs": [
        "docker", "ecs", "container", "compose", "task", "service", "ecr",
        "dockerfile", "docker-compose", "image", "volume", "network", "restart",
        "dev server", "dev-server", "npm run dev", "npm start", "pnpm", "yarn dev",
        "next dev", "vite", "webpack", "hot reload", "hmr", "fargate", "task definition"
    ],
    "webrtc_livekit": [
        "webrtc", "livekit", "rtc", "turn", "stun", "ice", "sdp", "participant",
        "websocket", "ws://", "wss://", "realtime", "streaming", "peer connection",
        "media stream", "audio track", "video track"
    ],
    "aws_infrastructure": [
        "aws", "ec2", "lambda", "terraform", "asg", "nlb", "vpc", "iam",
        "cloudfront", "s3", "rds", "route53", "cloudwatch", "secrets manager",
        "api gateway", "sqs", "sns", "dynamodb", "elasticache", "bedrock"
    ],
    "voice_pipeline": [
        "voice", "stt", "tts", "llm", "whisper", "kyutai", "bedrock", "audio", "sample",
        "speech", "recognition", "synthesis", "microphone", "speaker", "elevenlabs",
        "transcription", "vad", "voice activity"
    ],
    "django_backend": [
        "django", "gunicorn", "backend", "wsgi", "manage.py", "migrations",
        "python manage", "runserver", "collectstatic", "makemigrations", "migrate",
        "rest framework", "drf", "serializer", "viewset"
    ],
    "memory_system": [
        "memory", "acontext", "context-dna", "contextdna", "brain", "sop", "learning",
        "query.py", "context.py", "professor", "injection", "xbar", "menu bar",
        "work_dialogue", "dialogue_log", "helper agent", "helper-agent", "port 8080",
        "learnings", "consult", "webhook", "hook", "electron", "dashboard", "synaptic"
    ],
    # Frontend/React domains
    "frontend_react": [
        "react", "next", "nextjs", "component", "tsx", "jsx", "hook", "useState",
        "useEffect", "framer", "motion", "animation", "tailwind", "shadcn",
        "frontend", "ui", "v0", "admin.contextdna", "split panel", "resizable"
    ],
    # Git/Version Control
    "git_version_control": [
        "git", "commit", "push", "pull", "merge", "branch", "rebase", "stash",
        "checkout", "git diff", "git log", "remote", "origin", "git main", "git master",
        "git add", "git status", "git reset", "submodule", "cherry-pick"
    ],
    # Database
    "database": [
        "database", "db", "sql", "postgres", "postgresql", "mysql", "sqlite",
        "migration", "schema", "query", "index", "foreign key", "constraint",
        "redis", "rabbitmq", "opensearch", "elasticsearch", "orm", "sqlalchemy"
    ],
    # Testing
    "testing": [
        "test", "pytest", "jest", "unittest", "mock", "fixture", "coverage",
        "assert", "expect", "spec", "e2e", "integration", "unit test", "snapshot"
    ],
    # Build/Deployment
    "build_deploy": [
        "build", "deploy", "release", "ci", "cd", "pipeline", "github actions",
        "vercel", "netlify", "amplify", "production", "staging", "rollback",
        "blue-green", "canary", "rollout"
    ],
    # Security
    "security": [
        "auth", "password", "token", "jwt", "oauth", "permission", "encryption",
        "cors", "csrf", "xss", "injection", "credentials", "secrets", "api key",
        "ssl", "tls", "certificate", "firewall", "iam", "rbac"
    ],
    # Performance
    "performance": [
        "performance", "slow", "optimize", "cache", "bottleneck", "profile",
        "benchmark", "latency", "throughput", "memory leak", "cpu", "gpu"
    ],
    # Debugging
    "debugging": [
        "debug", "error", "exception", "traceback", "bug", "fix", "issue",
        "breakpoint", "logging", "stack trace", "root cause"
    ],
}


# =============================================================================
# THE PROFESSOR CLASS
# =============================================================================

@dataclass
class ProfessorGuidance:
    """The professor's complete guidance for a task."""
    task: str
    domains: List[str] = field(default_factory=list)
    first_principles: List[str] = field(default_factory=list)
    the_one_thing: str = ""
    landmines: List[str] = field(default_factory=list)
    pattern: str = ""
    context: str = ""
    additional_learnings: List[str] = field(default_factory=list)
    project_specific: List[str] = field(default_factory=list)

    def format(self) -> str:
        """Format as readable guidance."""
        output = []

        # Header
        output.append("╔══════════════════════════════════════════════════════════════════════╗")
        output.append("║  🎓 PROFESSOR'S GUIDANCE                                              ║")
        output.append("╚══════════════════════════════════════════════════════════════════════╝")
        output.append("")
        output.append(f"Task: {self.task}")
        output.append(f"Domains: {', '.join(self.domains)}")
        output.append("")

        # The One Thing (always first - this is the most important)
        if self.the_one_thing:
            output.append("━━━ THE ONE THING ━━━")
            output.append(self._clean(self.the_one_thing))
            output.append("")

        # Landmines (critical warnings)
        if self.landmines:
            output.append("━━━ LANDMINES (avoid these) ━━━")
            for mine in self.landmines:
                output.append(f"  💣 {mine}")
            output.append("")

        # Pattern (how veterans do it)
        if self.pattern:
            output.append("━━━ THE PATTERN (veteran approach) ━━━")
            output.append(self._clean(self.pattern))
            output.append("")

        # Context (bigger picture)
        if self.context:
            output.append("━━━ CONTEXT (where this fits) ━━━")
            output.append(self._clean(self.context))
            output.append("")

        # First Principles (mental model)
        if self.first_principles:
            output.append("━━━ FIRST PRINCIPLES (mental model) ━━━")
            for principle in self.first_principles:
                output.append(self._clean(principle))
            output.append("")

        # Project-specific learnings
        if self.project_specific:
            output.append("━━━ FROM YOUR PROJECT (learned patterns) ━━━")
            for learning in self.project_specific[:5]:
                output.append(f"  📌 {learning}")
            output.append("")

        # Additional learnings from memory
        if self.additional_learnings:
            output.append("━━━ FROM EXPERIENCE (past learnings) ━━━")
            for learning in self.additional_learnings[:5]:
                output.append(f"  → {learning}")
            output.append("")

        output.append("═══════════════════════════════════════════════════════════════════════")

        return "\n".join(output)

    def _clean(self, text: str) -> str:
        """Clean up multiline text."""
        lines = [line.strip() for line in text.strip().split("\n")]
        return "\n".join(f"  {line}" if line else "" for line in lines)


class Professor:
    """
    The Nobel-Prize winning professor who gives you exactly what you need.

    Combines built-in universal wisdom with project-specific learnings.
    """

    def __init__(self, storage=None):
        """
        Initialize the Professor.

        Args:
            storage: Optional storage backend for querying project learnings
        """
        self.storage = storage
        self._custom_domains = {}  # User-added domains

    def add_domain(self, name: str, wisdom: dict, keywords: List[str]):
        """
        Add a custom domain with its wisdom.

        This allows users to extend the professor with project-specific wisdom.

        Args:
            name: Domain name (e.g., "my_framework")
            wisdom: Dict with first_principle, the_one_thing, landmines, pattern, context
            keywords: Keywords that trigger this domain
        """
        self._custom_domains[name] = wisdom
        DOMAIN_KEYWORDS[name] = keywords

    def consult(self, task: str = None, file: str = None) -> ProfessorGuidance:
        """
        Consult the professor before starting work.

        This is THE function to call before any significant work.
        Returns distilled wisdom - not data, but insight.

        Args:
            task: What you're about to do
            file: File you're about to modify

        Returns:
            ProfessorGuidance with exactly what you need to know
        """
        query = task or file or ""

        # Detect relevant domains
        domains = self._detect_domains(query)

        # Build guidance
        guidance = ProfessorGuidance(task=query, domains=domains)

        # Gather wisdom from each relevant domain
        all_wisdom = {**PROFESSOR_WISDOM, **self._custom_domains}

        for domain in domains:
            wisdom = all_wisdom.get(domain, {})

            if wisdom.get("first_principle"):
                guidance.first_principles.append(wisdom["first_principle"])

            if wisdom.get("the_one_thing") and not guidance.the_one_thing:
                guidance.the_one_thing = wisdom["the_one_thing"]

            if wisdom.get("landmines"):
                guidance.landmines.extend(wisdom["landmines"])

            if wisdom.get("pattern") and not guidance.pattern:
                guidance.pattern = wisdom["pattern"]

            if wisdom.get("context"):
                if guidance.context:
                    guidance.context += "\n\n" + wisdom["context"]
                else:
                    guidance.context = wisdom["context"]

        # Add learnings from storage if available
        if self.storage:
            guidance.additional_learnings = self._get_learnings_from_storage(query, domains)
            guidance.project_specific = self._get_project_patterns(query, domains)

        # Dedupe landmines
        guidance.landmines = list(dict.fromkeys(guidance.landmines))[:7]

        return guidance

    def _detect_domains(self, query: str) -> list:
        """Detect relevant domains from the query."""
        query_lower = query.lower()
        domains = []

        for domain, keywords in DOMAIN_KEYWORDS.items():
            if any(kw in query_lower for kw in keywords):
                domains.append(domain)

        # Default to debugging if nothing matches (always good advice)
        if not domains:
            domains = ["debugging"]

        return domains[:3]  # Limit to 3 most relevant

    def _get_learnings_from_storage(self, query: str, domains: list) -> List[str]:
        """Get additional learnings from storage backend."""
        learnings = []

        if not self.storage:
            return learnings

        try:
            # Search for relevant learnings
            results = self.storage.search(query, limit=5)
            for r in results:
                # Extract meaningful content
                title = r.get('title', '')
                content = r.get('content', '')[:150]
                if title:
                    learnings.append(f"{title}: {content}")
                elif content:
                    learnings.append(content)
        except Exception as e:
            print(f"[WARN] Failed to get learnings from storage: {e}")

        return learnings[:5]

    def _get_project_patterns(self, query: str, domains: list) -> List[str]:
        """Get project-specific patterns from storage."""
        patterns = []

        if not self.storage:
            return patterns

        try:
            # Search for patterns and SOPs
            results = self.storage.search(f"pattern sop {query}", limit=5)
            for r in results:
                learning_type = r.get('learning_type', '')
                if learning_type in ['pattern', 'sop', 'gotcha']:
                    title = r.get('title', '')
                    if title:
                        patterns.append(f"[{learning_type.upper()}] {title}")
        except Exception as e:
            print(f"[WARN] Failed to get project patterns from storage: {e}")

        return patterns[:5]


# =============================================================================
# PUBLIC API
# =============================================================================

_professor = None


def get_professor(storage=None) -> Professor:
    """Get the global Professor instance."""
    global _professor
    if _professor is None or storage is not None:
        _professor = Professor(storage=storage)
    return _professor


def consult(task: str = None, file: str = None, storage=None) -> str:
    """
    Consult the professor before starting work.

    This is THE function to call before any significant work.
    Returns distilled wisdom - not data, but insight.

    Args:
        task: What you're about to do
        file: File you're about to modify
        storage: Optional storage backend for project-specific learnings

    Returns:
        Formatted guidance string
    """
    professor = get_professor(storage)
    guidance = professor.consult(task=task, file=file)
    return guidance.format()


def get_wisdom(domain: str) -> dict:
    """
    Get the professor's wisdom for a specific domain.

    Args:
        domain: One of: async_python, docker, database, api_design, git,
                testing, security, performance, debugging, deployment

    Returns:
        Dict with first_principle, the_one_thing, landmines, pattern, context
    """
    return PROFESSOR_WISDOM.get(domain, {})


def list_domains() -> List[str]:
    """List all domains the professor knows about."""
    return list(PROFESSOR_WISDOM.keys())


def add_custom_domain(name: str, wisdom: dict, keywords: List[str]):
    """
    Add a custom domain to the professor.

    This is how users extend the professor with project-specific wisdom.

    Example:
        add_custom_domain(
            "my_framework",
            {
                "first_principle": "...",
                "the_one_thing": "...",
                "landmines": ["..."],
                "pattern": "...",
                "context": "..."
            },
            ["myframework", "myfw", "my-framework"]
        )
    """
    professor = get_professor()
    professor.add_domain(name, wisdom, keywords)


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("🎓 PROFESSOR - Nobel-Prize Level Context")
        print("")
        print("Usage:")
        print("  python professor.py <task description>")
        print("  python professor.py --file <filepath>")
        print("  python professor.py --domain <domain>")
        print("  python professor.py --list")
        print("")
        print("Examples:")
        print("  python professor.py 'configure database migrations'")
        print("  python professor.py 'fix async boto3 performance'")
        print("  python professor.py --domain async_python")
        print("")
        print("This gives you EXACTLY what you need - not a data dump, but wisdom.")
        sys.exit(0)

    if sys.argv[1] == "--file":
        if len(sys.argv) < 3:
            print("Usage: --file <filepath>")
            sys.exit(1)
        print(consult(file=sys.argv[2]))

    elif sys.argv[1] == "--domain":
        if len(sys.argv) < 3:
            print("Usage: --domain <domain>")
            print(f"Available: {', '.join(list_domains())}")
            sys.exit(1)
        domain = sys.argv[2]
        wisdom = get_wisdom(domain)
        if wisdom:
            print(f"\n🎓 WISDOM FOR: {domain}\n")
            for key, value in wisdom.items():
                print(f"━━━ {key.upper()} ━━━")
                if isinstance(value, list):
                    for item in value:
                        print(f"  • {item}")
                else:
                    print(f"  {value.strip()}")
                print()
        else:
            print(f"Unknown domain: {domain}")
            print(f"Available: {', '.join(list_domains())}")

    elif sys.argv[1] == "--list":
        print("Available domains:")
        for domain in list_domains():
            keywords = DOMAIN_KEYWORDS.get(domain, [])
            print(f"  {domain}: {', '.join(keywords[:5])}")

    else:
        task = " ".join(sys.argv[1:])
        print(consult(task=task))
