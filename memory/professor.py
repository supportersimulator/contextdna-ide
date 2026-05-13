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
    from memory.professor import consult

    # Before ANY significant work
    wisdom = consult("configure LiveKit TURN server")
    # Returns: Exactly what a Nobel-prize professor would tell you

    # Or for code-specific guidance
    wisdom = consult(file="ersim-voice-stack/services/llm/app/main.py")

---

WHAT MAKES THIS DIFFERENT:

OLD APPROACH (data dump):
    "Here are 47 learnings about async, boto3, docker, websockets..."
    → Agent drowns in information
    → Can't distinguish critical from trivial
    → Still makes the obvious mistake

PROFESSOR APPROACH (distilled wisdom):
    "ONE CRITICAL THING: boto3.converse() blocks your entire event loop.
     Every call freezes everything for 1-14 seconds.

     THE FIX: await asyncio.to_thread(client.converse, **params)

     WHY THIS MATTERS: Without this, your voice agent will stutter and
     users will think the system is broken. This single line is the
     difference between 'feels responsive' and 'feels broken'.

     THE LANDMINE: Don't wrap the streaming version - it's sync-per-token
     anyway. Use non-streaming + to_thread. Counter-intuitive but faster."

---

THE PROFESSOR'S FRAMEWORK:

1. FIRST PRINCIPLES
   - What is this thing at its core?
   - What mental model should the agent have?

2. THE ONE THING
   - The single most critical insight for this specific task
   - If they remember nothing else, remember this

3. THE LANDMINES
   - Mistakes that look reasonable but cost hours
   - The "gotcha" that only experience teaches

4. THE PATTERN
   - How veterans approach this
   - The workflow that separates juniors from seniors

5. THE CONTEXT
   - Where this fits in the bigger picture
   - Dependencies and ripple effects

---

This module queries all memory systems and DISTILLS them into professor-level guidance.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / "memory"

sys.path.insert(0, str(PROJECT_ROOT))

# Import memory systems
try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False

try:
    from memory.knowledge_graph import KnowledgeGraph, CROSS_CATEGORY_KEYWORDS
    KNOWLEDGE_GRAPH_AVAILABLE = True
except ImportError:
    KNOWLEDGE_GRAPH_AVAILABLE = False

try:
    from memory.brain import ArchitectureBrain
    BRAIN_AVAILABLE = True
except ImportError:
    BRAIN_AVAILABLE = False


# =============================================================================
# PROFESSOR'S KNOWLEDGE BASE
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
            boto3, whisper, soundfile, and most I/O libraries are SYNCHRONOUS.
            They block your entire event loop. The fix is always:
                await asyncio.to_thread(blocking_function, *args)
            This runs the sync code in a thread pool, freeing the event loop.
        """,
        "landmines": [
            "boto3's streaming API is sync-per-token. Streaming doesn't help - use non-streaming + to_thread instead.",
            "soundfile.write() looks fast but blocks. Always to_thread() file I/O in async code.",
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
            This affects: Voice pipeline (STT→LLM→TTS), WebSocket handlers, API endpoints.
            A 14-second LLM call blocking the event loop means 14 seconds of silence
            to the user - they'll think it crashed.
        """,
    },

    "docker_ecs": {
        "first_principle": """
            Docker containers are immutable artifacts. Environment comes from outside.
            ECS is an orchestrator that decides WHERE and WHEN containers run.
            Think of containers as functions, ECS as the scheduler.
        """,
        "the_one_thing": """
            `docker restart` does NOT reload --env-file. The old environment persists.
            You must: docker stop → docker rm → docker run
            This catches 80% of "my env var change didn't work" issues.
        """,
        "landmines": [
            "ECS tasks take 1-2 minutes for health checks. Don't declare success too early.",
            "Container logs show last state before crash - the crash cause is often in earlier logs.",
            "Private ECR images need task execution role, not just task role.",
        ],
        "pattern": """
            VETERAN DEPLOYMENT PATTERN:
            1. docker-compose up -d --force-recreate (ensures fresh containers)
            2. Watch docker logs -f for startup errors
            3. Wait for health checks (don't trust "running" status alone)
            4. Test the actual endpoint, not just container status
        """,
        "context": """
            Voice stack runs: Agent on CPU ECS, STT/LLM/TTS on GPU ECS.
            The GPU services are the expensive part (~$1.20/hr). Toggle them off when not in use.
        """,
    },

    "webrtc_livekit": {
        "first_principle": """
            WebRTC is peer-to-peer media streaming. LiveKit is a SFU (Selective
            Forwarding Unit) - it receives all streams and decides what to forward.
            The key constraint: WebRTC needs DIRECT UDP connectivity for media.
        """,
        "the_one_thing": """
            DNS for LiveKit/TURN MUST NOT be Cloudflare proxied.
            WebRTC needs direct IP, not a proxy. Set Cloudflare DNS to DNS-only (grey cloud).
            This single setting causes 90% of "WebRTC won't connect" issues.
        """,
        "landmines": [
            "TURN server credentials expire - if using time-based auth, ensure clock sync.",
            "ICE candidates gather slowly - wait for gathering complete, don't rush.",
            "Media packets are UDP - firewall must allow UDP 50000-60000 range.",
        ],
        "pattern": """
            VETERAN DEBUGGING PATTERN:
            1. Check DNS is not proxied (dig +short should return actual IP)
            2. Verify UDP ports are open (nc -uvz voice.domain.com 50000)
            3. Check LiveKit server logs for ICE failures
            4. Use browser WebRTC internals (chrome://webrtc-internals/)
        """,
        "context": """
            Voice pipeline: Browser → LiveKit → Agent → STT → LLM → TTS → LiveKit → Browser
            Latency budget: ~200ms total for "feels responsive". Every hop counts.
        """,
    },

    "aws_infrastructure": {
        "first_principle": """
            AWS is eventually consistent. Changes propagate over time, not instantly.
            IAM changes, DNS updates, security group modifications - all have delay.
            Always verify the actual state, not what you just configured.
        """,
        "the_one_thing": """
            Auto Scaling Group instances get NEW PRIVATE IPs on restart.
            Never hardcode IPs. Use Internal NLB for stable endpoints.
            This architectural decision prevents 90% of "it worked then stopped" bugs.
        """,
        "landmines": [
            "IAM policy changes take 30-60 seconds to propagate. Wait before testing.",
            "Security group changes are instant but NACLs have stateful rules that confuse.",
            "ECS uses task role for app permissions, execution role for ECR/logs.",
        ],
        "pattern": """
            VETERAN TERRAFORM PATTERN:
            1. terraform plan FIRST - read the plan, look for destroys
            2. Apply in stages: networking → compute → application
            3. Use depends_on for non-obvious ordering
            4. Tag everything - you'll need to find it later
        """,
        "context": """
            ER Simulator runs on: t3.small (Django), t3.small (LiveKit), g5.xlarge (GPU).
            GPU is the cost center. Internal NLB routes to GPU services.
        """,
    },

    "voice_pipeline": {
        "first_principle": """
            Voice is REAL-TIME. Latency is user experience.
            200ms feels instant, 500ms feels laggy, 1s feels broken.
            Every millisecond matters. Profile everything.
        """,
        "the_one_thing": """
            Sample rate mismatches cause distorted/chipmunk audio.
            WebRTC standard: 48000 Hz. Kyutai TTS outputs: 24000 Hz.
            Always resample to 48000 Hz before sending to LiveKit.
        """,
        "landmines": [
            "LLM streaming looks faster but blocks per-token. Non-streaming + to_thread is often better.",
            "STT needs silence detection or you get hallucinations on empty audio.",
            "TTS audio chunks must be consistent size or playback stutters.",
        ],
        "pattern": """
            VETERAN LATENCY PATTERN:
            1. Measure end-to-end: user speaks → user hears response
            2. Break down: STT time + LLM time + TTS time + network time
            3. Optimize the biggest contributor first
            4. Consider interruption handling - user may speak during response
        """,
        "context": """
            Full pipeline: Mic → LiveKit → Agent → STT (Whisper) → LLM (Bedrock) → TTS (Kyutai) → LiveKit → Speaker
            Target: 2 seconds from user stops speaking to hearing first word of response.
        """,
    },

    "django_backend": {
        "first_principle": """
            Django is a synchronous framework with WSGI. Async views exist but
            need careful handling. Gunicorn with workers is the production pattern.
            Database connections are per-worker, not shared.
        """,
        "the_one_thing": """
            On EC2, git operations fail because HOME=/root is required.
            Add to systemd service: Environment="HOME=/root"
            This catches most "git clone failed" deployment issues.
        """,
        "landmines": [
            "Django's runserver is NOT for production. Always use gunicorn.",
            "Static files need collectstatic + nginx/CDN serving.",
            "Database migrations on production: always backup first, apply during low traffic.",
        ],
        "pattern": """
            VETERAN DEPLOYMENT PATTERN:
            1. SSH to EC2, cd to /home/ubuntu/backend
            2. git pull origin main
            3. source .venv/bin/activate
            4. pip install -r requirements.txt
            5. python manage.py migrate
            6. sudo systemctl restart gunicorn
            7. Check journalctl -u gunicorn -f for errors
        """,
        "context": """
            Django backend at api.ersimulator.com serves: scenarios, patients, sessions.
            It's the source of truth - voice agents query it for simulation state.
        """,
    },

    "memory_system": {
        "first_principle": """
            This memory system is AUTONOMOUS. It learns from git commits, detects
            success patterns, and surfaces relevant context before work begins.
            The agent should TRUST it - the system knows what the agent doesn't know it doesn't know.
        """,
        "the_one_thing": """
            Query BEFORE writing code. Always.
            .venv/bin/python3 memory/query.py "what you're about to do"
            The 10 seconds of querying saves hours of repeating past mistakes.
        """,
        "landmines": [
            "Don't skip the memory query because 'this seems simple' - the gotchas live in simple tasks.",
            "After success, run: python memory/brain.py success 'task' 'what worked'",
            "The system only learns what you teach it - good commit messages matter.",
            "Ecosystem health daemon credentials: sourced from context-dna/infra/.env",
            "Synaptic's Signal Strength depends on: Redis + PostgreSQL connections + domain matching.",
            "Section 6 = Synaptic→Atlas (task guidance), Section 8 = Synaptic→Aaron (subconscious).",
            "Professor unavailable? Check domain keywords match or expand DOMAIN_KEYWORDS.",
            "Foundation empty? Check learnings database has content matching the query terms.",
        ],
        "pattern": """
            SYNAPTIC VOICE DEBUGGING PATTERN:
            1. Check ecosystem_health.py daemon is running with correct credentials
            2. Verify .synaptic_system_awareness.json shows redis/postgresql healthy
            3. Test get_8th_intelligence_data() returns learnings and patterns
            4. Check DOMAIN_KEYWORDS has terms that match your query
            5. Verify auto-memory-query.sh sources context-dna/infra/.env correctly
        """,
        "context": """
            9-Section Architecture:
            - Section 0: Safety (NEVER DO)
            - Section 1: Foundation (start file + SOPs)
            - Section 2: Professor Wisdom (this!)
            - Section 3-5: Awareness, Deep Context, Protocol
            - Section 6: Synaptic→Atlas (task-focused)
            - Section 8: Synaptic→Aaron (8th Intelligence)

            Signal Strength levels: 🟢 Clear (rich), 🟡 Present (moderate), 🔴 Quiet (degraded)
            Context Confidence: 100% (all 5 sources), 60% (3 sources), 40% (2 sources)
        """,
    },

    # NEW DOMAINS BELOW - Expanded wisdom coverage

    "frontend_react": {
        "first_principle": """
            React is declarative - describe WHAT, not HOW.
            Components should be pure functions of props and state.
            Side effects belong in useEffect, never in render.
        """,
        "the_one_thing": """
            Check the dev server console AND browser console.
            90% of React bugs show errors in one of these two places.
            Hot reload can mask issues - if something weird happens, hard refresh (Cmd+Shift+R).
        """,
        "landmines": [
            "useEffect with empty deps [] runs ONCE - missing deps cause stale closures.",
            "Don't mutate state directly - always use setState/setX with new object.",
            "Keys must be stable and unique - using index as key causes bugs with reordering.",
        ],
        "pattern": """
            VETERAN REACT PATTERN:
            1. Start dev server: npm run dev
            2. Open browser console (F12)
            3. Make changes → watch for hot reload
            4. If weird: hard refresh + clear React DevTools
            5. Check terminal for build errors
        """,
        "context": """
            Next.js adds server components - 'use client' for browser-only code.
            Framer Motion for animations - AnimatePresence for exit animations.
        """,
    },

    "git_version_control": {
        "first_principle": """
            Git is append-only history. Commits are immutable snapshots.
            The working directory, staging area, and commit history are separate.
            Understanding this three-tree architecture prevents most confusion.
        """,
        "the_one_thing": """
            git status BEFORE every commit.
            See what's staged, what's modified, what's untracked.
            This prevents accidentally committing secrets or large files.
        """,
        "landmines": [
            "git reset --hard destroys uncommitted work - stash first if unsure.",
            "Force push to main/master rewrites history for everyone - avoid unless necessary.",
            "Large files (>50MB) bloat the repo forever - use .gitignore or git-lfs.",
        ],
        "pattern": """
            VETERAN GIT PATTERN:
            1. git status (see current state)
            2. git diff (review changes)
            3. git add <specific files> (not git add .)
            4. git commit -m "feat/fix: description"
            5. git push origin <branch>
        """,
        "context": """
            Conventional commits: feat:, fix:, docs:, refactor:, test:, chore:
            Branch naming: feature/, bugfix/, hotfix/ prefixes help organization.
        """,
    },

    "database": {
        "first_principle": """
            The database is the source of truth. Application state is derived.
            Migrations are code - they should be in version control.
            Schema changes are often irreversible - backup first.
        """,
        "the_one_thing": """
            ALWAYS backup before migrations.
            Test migrations on a copy of production data before running on prod.
            Data migrations (changing values) are riskier than schema migrations.
        """,
        "landmines": [
            "DROP COLUMN deletes data forever - no undo without backup.",
            "Adding NOT NULL to existing column fails if NULLs exist - add default first.",
            "Index creation on large tables can lock the table - use CONCURRENTLY in Postgres.",
        ],
        "pattern": """
            VETERAN DATABASE PATTERN:
            1. Backup database
            2. Test migration on staging with prod-like data
            3. Run migration
            4. Verify data integrity
            5. Keep backup for 7 days minimum
        """,
        "context": """
            Django: python manage.py makemigrations → migrate
            Check migration files before applying - they're just Python.
        """,
    },

    "testing": {
        "first_principle": """
            Tests are specifications that run. They document expected behavior.
            Fast tests get run often. Slow tests get skipped.
            Flaky tests erode trust - fix or delete them.
        """,
        "the_one_thing": """
            Test behavior, not implementation.
            Ask 'what should this do?' not 'how does the code work?'
            If refactoring breaks tests, the tests were too coupled.
        """,
        "landmines": [
            "Mocking everything makes tests pass but bugs ship - use real dependencies when feasible.",
            "Shared test state causes order-dependent failures - isolate each test.",
            "Time-dependent tests flake on CI - mock datetime or use freezegun.",
        ],
        "pattern": """
            VETERAN TESTING PATTERN:
            1. Unit tests for pure logic (fast, isolated)
            2. Integration tests for boundaries (DB, APIs)
            3. E2E tests for critical user paths only
            4. Run tests before committing
        """,
        "context": """
            Python: pytest (not unittest). JavaScript: Jest or Vitest.
            Coverage is a metric, not a goal - 70% thoughtful > 100% shallow.
        """,
    },

    "build_deploy": {
        "first_principle": """
            Build once, deploy many. The same artifact goes to all environments.
            Configuration varies by environment - code doesn't.
            If it works in staging with same config, it works in production.
        """,
        "the_one_thing": """
            Always have a rollback plan.
            Know how to revert to the previous deployment before you deploy.
            Test the rollback procedure - it's not real until it's tested.
        """,
        "landmines": [
            "Friday deploys ruin weekends - deploy early in the week.",
            "Database migrations before code deploy - old code must work with new schema.",
            "Environment variables not reloaded on restart - may need container recreate.",
        ],
        "pattern": """
            VETERAN DEPLOY PATTERN:
            1. Verify CI/CD passes
            2. Deploy to staging first
            3. Smoke test critical paths
            4. Deploy to production
            5. Monitor for 15 minutes
            6. If issues: rollback immediately
        """,
        "context": """
            Vercel: git push triggers deploy automatically.
            AWS: Check CloudWatch after deploy for errors.
        """,
    },

    # =============================================================================
    # ADDITIONAL DOMAINS (Added 2026-01-26)
    # These fill the gap where DOMAIN_KEYWORDS existed but no wisdom templates
    # =============================================================================

    "build_deploy": {
        "first_principle": """
            Deployment is a STATE TRANSITION, not just "copying files".
            You're moving from known-good-state to new-state. The risk is
            getting stuck in between - neither old nor new works.
        """,
        "the_one_thing": """
            ALWAYS have a rollback plan BEFORE you deploy.
            Whether it's `git revert`, previous Docker image tag, or Terraform state backup -
            know exactly how to undo what you're about to do.
            The deploy that goes wrong without rollback costs 10x the time.
        """,
        "landmines": [
            "Don't deploy on Friday afternoon - Murphy's Law is real.",
            "'It works on my machine' means nothing - test in staging first.",
            "Database migrations can't be rolled back easily - test them separately.",
            "Health checks lie - a 200 OK doesn't mean the app actually works.",
            "Load balancer takes time to drain - don't kill old instances immediately.",
        ],
        "pattern": """
            VETERAN DEPLOYMENT PATTERN:
            1. Backup current state (DB snapshot, note current image tags)
            2. Deploy to staging first, verify manually
            3. Deploy to production with canary (10% traffic)
            4. Watch metrics for 5 minutes (not just health checks)
            5. Full rollout only after metrics look good
            6. Keep old version running for 1 hour before terminating
        """,
        "context": """
            In this codebase: Django deploys via systemctl, voice services via ECS.
            Landing page is Vercel (automatic). Terraform for infrastructure.
            GPU instances are expensive - only start them when needed.
        """,
    },

    "database": {
        "first_principle": """
            Databases are the SOURCE OF TRUTH. Everything else can be rebuilt
            from the database. Treat it with appropriate paranoia.
            A corrupted or lost database is catastrophic.
        """,
        "the_one_thing": """
            NEVER run migrations on production without:
            1. A backup taken in the last 5 minutes
            2. Testing the exact migration on a copy of prod data
            3. A rollback migration ready to go
            One bad migration can corrupt data in ways that are impossible to fix.
        """,
        "landmines": [
            "Django makemigrations creates migrations - it doesn't run them. migrate does.",
            "Adding NOT NULL column to table with data fails - need default or nullable first.",
            "Dropping a column is instant, but the ORM will error until code is deployed.",
            "Index creation locks the table in some databases - use CONCURRENTLY in Postgres.",
            "Foreign key constraints prevent deletion - check cascades carefully.",
        ],
        "pattern": """
            VETERAN DATABASE PATTERN:
            1. Make migration with makemigrations
            2. Review the generated SQL: sqlmigrate app_name migration_name
            3. Test on local with prod-like data volume
            4. Backup prod database
            5. Run migration during low-traffic window
            6. Verify data integrity after migration
        """,
        "context": """
            This project uses PostgreSQL via Supabase/RDS.
            Django ORM for models. User data is sacred - never delete without backup.
            Session data can be regenerated. Payment data requires extra care.
        """,
    },

    "frontend_react": {
        "first_principle": """
            React is a RENDERING library, not an application framework.
            It re-renders when state changes. Most bugs come from:
            1. Unexpected re-renders
            2. Stale closures capturing old state
            3. Effects running when they shouldn't
        """,
        "the_one_thing": """
            useEffect dependencies are NOT optional hints - they're contracts.
            If you lie about dependencies (empty array when you use state),
            you WILL have bugs. ESLint exhaustive-deps rule exists for a reason.
            When in doubt, extract the logic to a custom hook.
        """,
        "landmines": [
            "Object/array as dependency → infinite loop (new reference each render).",
            "Missing dependency → stale closure → bug that only appears sometimes.",
            "State updates are async - can't read new value on next line.",
            "Key prop on lists must be stable - don't use array index for dynamic lists.",
            "Conditional hooks break Rules of Hooks - always call same hooks same order.",
        ],
        "pattern": """
            VETERAN REACT PATTERN:
            1. State should be minimal - derive what you can
            2. Lift state only when needed for sharing
            3. useCallback/useMemo for expensive operations or stable references
            4. Custom hooks to encapsulate related state + effects
            5. Error boundaries for graceful failure
            6. Suspense for loading states
        """,
        "context": """
            This project: Next.js for landing, React Native for mobile monitor.
            Admin dashboard uses shadcn/ui components.
            State management: React Context for simple, Zustand if needed.
        """,
    },

    "git_version_control": {
        "first_principle": """
            Git is a DIRECTED ACYCLIC GRAPH of snapshots, not a timeline.
            Branches are just pointers. Commits are immutable.
            Understanding this mental model prevents most git disasters.
        """,
        "the_one_thing": """
            NEVER force push to main/master unless you're absolutely sure.
            Force push rewrites history - other people's work can be lost.
            If you need to undo a bad commit on main, use `git revert` instead.
            Revert creates a new commit that undoes the change - history preserved.
        """,
        "landmines": [
            "git reset --hard destroys uncommitted work forever - no recovery.",
            "Rebasing shared branches causes duplicate commits for others.",
            "Merge conflicts in binary files (images, etc.) can't be auto-resolved.",
            ".gitignore only ignores untracked files - already tracked files need git rm.",
            "Submodules are pointers - parent repo doesn't auto-update when submodule changes.",
        ],
        "pattern": """
            VETERAN GIT PATTERN:
            1. Commit often, push when stable
            2. Feature branches for any non-trivial work
            3. Rebase feature branch onto main before merge (cleaner history)
            4. Squash WIP commits before merge (meaningful history)
            5. Write commit messages that explain WHY, not just what
            6. Tag releases for easy rollback reference
        """,
        "context": """
            This repo has submodules: landing-page, v0-context-dna.
            Submodule changes require 2-step commit (inside submodule, then parent).
            Use ./scripts/deploy-landing.sh for landing page deploys.
        """,
    },

    "testing": {
        "first_principle": """
            Tests are DOCUMENTATION that happens to be executable.
            A test that passes but doesn't test the right thing is worse than no test -
            it gives false confidence. Test behavior, not implementation.
        """,
        "the_one_thing": """
            Test the PUBLIC CONTRACT, not the internal implementation.
            If you test private methods or internal state, your tests break
            every time you refactor - even when behavior is unchanged.
            Good tests let you refactor fearlessly.
        """,
        "landmines": [
            "Mocking too much → tests pass but real integration fails.",
            "Testing implementation details → brittle tests that break on refactor.",
            "Flaky tests (pass sometimes, fail sometimes) → worse than no tests.",
            "Slow tests → developers stop running them → bugs slip through.",
            "No assertions → test passes but verifies nothing.",
        ],
        "pattern": """
            VETERAN TESTING PATTERN:
            1. Unit tests for pure logic (fast, many)
            2. Integration tests for API contracts (medium speed, fewer)
            3. E2E tests for critical user flows only (slow, minimal)
            4. Test pyramid: many unit, fewer integration, minimal E2E
            5. Use fixtures/factories for test data
            6. Run tests in CI on every PR
        """,
        "context": """
            This project: pytest for Python, Jest for JavaScript.
            Voice pipeline is hard to test - use recorded audio fixtures.
            Database tests should use transactions that rollback.
        """,
    },
}

# Domain detection keywords - EXPANDED for better relevance detection
DOMAIN_KEYWORDS = {
    # Core technical domains
    "async_python": [
        "async", "asyncio", "await", "to_thread", "event loop", "blocking", "coroutine",
        "aiohttp", "aioboto", "concurrent", "threading", "multiprocessing"
    ],
    "docker_ecs": [
        "docker", "ecs", "container", "compose", "task", "service", "ecr",
        "dockerfile", "docker-compose", "image", "volume", "network", "restart",
        "dev server", "dev-server", "npm run dev", "npm start", "pnpm", "yarn dev",
        "next dev", "vite", "webpack", "hot reload", "hmr"
    ],
    "webrtc_livekit": [
        "webrtc", "livekit", "rtc", "turn", "stun", "ice", "sdp", "participant",
        "websocket", "ws://", "wss://", "realtime", "streaming"
    ],
    "aws_infrastructure": [
        "aws", "ec2", "lambda", "terraform", "asg", "nlb", "vpc", "iam",
        "cloudfront", "s3", "rds", "route53", "cloudwatch", "secrets manager"
    ],
    "voice_pipeline": [
        "voice", "stt", "tts", "llm", "whisper", "kyutai", "bedrock", "audio", "sample",
        "speech", "recognition", "synthesis", "microphone", "speaker"
    ],
    "django_backend": [
        "django", "gunicorn", "backend", "api", "wsgi", "manage.py", "migrations",
        "python manage", "runserver", "collectstatic", "makemigrations"
    ],
    "memory_system": [
        "memory", "acontext", "context-dna", "contextdna", "brain", "sop", "learning",
        "query.py", "context.py", "professor", "injection", "xbar", "menu bar",
        "work_dialogue", "dialogue_log", "helper agent", "helper-agent", "port 8080",
        "learnings", "consult", "webhook", "hook", "electron", "dashboard",
        # Meta-terms for self-referential queries about the system
        "synaptic", "8th intelligence", "section 6", "section 8", "signal strength",
        "context confidence", "foundation", "holistic", "awareness", "persistent_hook",
        "auto-memory", "ecosystem health", "restoration", "degraded", "voice quality",
        # Conversational/meta-query vocabulary (EXPANDED Feb 2, 2026)
        "full report", "evaluation", "assessment", "status", "current state",
        "contextual capacity", "gaps", "recommendations", "superhero mode", "superhero",
        "capacity", "your opinion", "your view", "your perspective", "what do you think",
        "how are you", "your awareness", "your evaluation", "your assessment",
        "current capability", "capability", "additional recommendations", "explore"
    ],
    # NEW: Frontend/React domains
    "frontend_react": [
        "react", "next", "nextjs", "component", "tsx", "jsx", "hook", "useState",
        "useEffect", "framer", "motion", "animation", "tailwind", "shadcn",
        "frontend", "ui", "v0", "admin.contextdna", "split panel", "resizable"
    ],
    # NEW: Git/Version Control (avoid generic words like "log" that match other contexts)
    "git_version_control": [
        "git", "commit", "push", "pull", "merge", "branch", "rebase", "stash",
        "checkout", "git diff", "git log", "remote", "origin", "git main", "git master",
        "git add", "git status", "git reset", "submodule"
    ],
    # NEW: Database
    "database": [
        "database", "db", "sql", "postgres", "postgresql", "mysql", "sqlite",
        "migration", "schema", "query", "index", "foreign key", "constraint"
    ],
    # NEW: Testing
    "testing": [
        "test", "pytest", "jest", "unittest", "mock", "fixture", "coverage",
        "assert", "expect", "spec", "e2e", "integration"
    ],
    # NEW: Build/Deployment
    "build_deploy": [
        "build", "deploy", "release", "ci", "cd", "pipeline", "github actions",
        "vercel", "netlify", "amplify", "production", "staging"
    ],
}


# =============================================================================
# THE PROFESSOR CLASS
# =============================================================================

@dataclass
class ProfessorGuidance:
    """The professor's complete guidance for a task."""
    task: str
    domains: list = field(default_factory=list)
    first_principles: list = field(default_factory=list)
    the_one_thing: str = ""
    landmines: list = field(default_factory=list)
    pattern: str = ""
    context: str = ""
    additional_learnings: list = field(default_factory=list)

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

        # Additional learnings from memory systems
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
    """

    def __init__(self):
        self.memory = None
        self.kg = None
        self.brain = None

        if CONTEXT_DNA_AVAILABLE:
            try:
                self.memory = ContextDNAClient()
            except Exception as e:
                print(f"[WARN] ContextDNAClient init failed: {e}")

        if KNOWLEDGE_GRAPH_AVAILABLE:
            try:
                self.kg = KnowledgeGraph()
            except Exception as e:
                print(f"[WARN] KnowledgeGraph init failed: {e}")

        if BRAIN_AVAILABLE:
            try:
                self.brain = ArchitectureBrain()
            except Exception as e:
                print(f"[WARN] ArchitectureBrain init failed: {e}")

    def consult(self, task: str = None, file: str = None, boundary_decision=None) -> ProfessorGuidance:
        """
        Consult the professor before starting work.

        This is THE function to call before any significant work.
        Returns distilled wisdom - not data, but insight.

        PRIORITY ORDER (domain wisdom over recent noise):
        1. FIRST: Domain templates for THE ONE THING (distilled expert wisdom)
        2. THEN: Actual learnings for LANDMINES (real mistakes to avoid)
        3. FINALLY: Additional learnings as "FROM EXPERIENCE" supplement

        Args:
            task: What you're about to do
            file: File you're about to modify
            boundary_decision: Optional BoundaryDecision for project-scoped filtering

        Returns:
            ProfessorGuidance with exactly what you need to know
        """
        query = task or file or ""

        # Detect relevant domains
        domains = self._detect_domains(query)

        # Build guidance
        guidance = ProfessorGuidance(task=query, domains=domains)

        # FIRST: Get domain-specific wisdom from templates (THE GOLD)
        # Templates contain distilled expertise - use these for THE ONE THING
        # Only override with actual learnings if they're highly domain-relevant
        for domain in domains:
            wisdom = PROFESSOR_WISDOM.get(domain, {})

            if wisdom.get("first_principle"):
                guidance.first_principles.append(wisdom["first_principle"])

            # Only use template THE ONE THING if we didn't get one from learnings
            if wisdom.get("the_one_thing") and not guidance.the_one_thing:
                guidance.the_one_thing = wisdom["the_one_thing"]

            if wisdom.get("landmines"):
                guidance.landmines.extend(wisdom["landmines"])

            # Only use template pattern if we didn't get one from learnings
            if wisdom.get("pattern") and not guidance.pattern:
                guidance.pattern = wisdom["pattern"]

            if wisdom.get("context"):
                if guidance.context:
                    guidance.context += "\n\n" + wisdom["context"]
                else:
                    guidance.context = wisdom["context"]

        # Add remaining learnings as "FROM EXPERIENCE"
        guidance.additional_learnings = self._get_additional_learnings(query, domains, boundary_decision=boundary_decision)

        # Augment with Markdown Memory Layer (if available)
        try:
            from memory.markdown_memory_layer import query_markdown_layer
            doc_results = query_markdown_layer(query, top_k=2, focus_filter=True)
            for doc in doc_results[:2]:
                guidance.additional_learnings.append(
                    f"[DOC: {doc['rel_path']}] {doc['summary'][:150]}"
                )
        except Exception:
            pass  # Layer not ready — degrade silently

        # Dedupe landmines
        guidance.landmines = list(dict.fromkeys(guidance.landmines))[:7]

        # Record consultation for evolution tracking
        try:
            evolution = get_evolution()
            wisdom_given = {
                "the_one_thing": bool(guidance.the_one_thing),
                "landmines": len(guidance.landmines),
                "pattern": bool(guidance.pattern),
                "context": bool(guidance.context),
            }
            evolution.record_consultation(query, domains, wisdom_given)
        except Exception:
            pass  # Don't fail consultation if evolution tracking fails

        return guidance

    def _get_specific_learnings(self, query: str) -> dict:
        """Get specific fixes and patterns from memory that match the query.

        Returns dict with 'fixes' and 'patterns' lists, prioritizing valuable learnings.
        """
        result = {'fixes': [], 'patterns': [], 'wins': []}

        if not self.memory:
            return result

        try:
            learnings = self.memory.get_relevant_learnings(query, limit=10)

            for learning in learnings:
                l_type = learning.get('type', '')
                title = learning.get('title', '')
                content = learning.get('content', '')

                # Skip low-value auto-captured noise
                if '[process SOP]' in title and 'docker containers' in title.lower():
                    continue
                if 'health check passed' in title.lower() and len(content) < 50:
                    continue

                if l_type == 'fix':
                    result['fixes'].append(learning)
                elif l_type == 'pattern':
                    result['patterns'].append(learning)
                elif l_type == 'win' and len(content) > 50:
                    result['wins'].append(learning)

        except Exception as e:
            print(f"[WARN] Learning categorization failed: {e}")

        return result

    def _detect_domains(self, query: str) -> list:
        """Detect relevant domains from the query."""
        query_lower = query.lower()
        domains = []

        for domain, keywords in DOMAIN_KEYWORDS.items():
            if any(kw in query_lower for kw in keywords):
                domains.append(domain)

        # Default to memory_system if nothing matches (always good advice)
        if not domains:
            domains = ["memory_system"]

        return domains[:3]  # Limit to 3 most relevant

    def _get_additional_learnings(self, query: str, domains: list, boundary_decision=None) -> list:
        """Get additional learnings from memory systems.

        Args:
            boundary_decision: Optional BoundaryDecision for workspace-scoped filtering.
                When provided, filters Acontext results to match active project.
                Domain wisdom templates (PROFESSOR_WISDOM) are NOT filtered — they're universal.
        """
        learnings = []
        raw_results = []  # Keep raw dicts for boundary filtering
        seen_cores = set()  # For dedup

        # Query Acontext
        if self.memory:
            try:
                results = self.memory.get_relevant_learnings(query, limit=10)

                # Provenance gate: exclude unverified gold-mined learnings from S2
                from memory.query import confidence_tier, _TIER_RANK
                min_rank = _TIER_RANK['observed']
                results = [r for r in results if _TIER_RANK.get(confidence_tier(r), 0) >= min_rank]

                # Apply boundary filtering BEFORE dedup/formatting
                if boundary_decision and results:
                    try:
                        from memory.boundary_intelligence import BoundaryIntelligence
                        bi = BoundaryIntelligence(use_llm=False)
                        # Convert to filter_learnings format (needs 'content' key)
                        filter_ready = []
                        for r in results:
                            fr = dict(r)
                            if 'content' not in fr:
                                fr['content'] = fr.get('use_when', '') or fr.get('title', '')
                            filter_ready.append(fr)
                        results = bi.filter_learnings(filter_ready, boundary_decision)
                    except Exception:
                        pass  # Filtering failed — use unfiltered results

                for r in results:
                    if r.get('distance', 1.0) < 0.6:
                        key = r.get('use_when', '') or r.get('title', '')
                        if not key or len(key) < 20:
                            continue

                        # Skip low-value auto-captured noise
                        key_lower = key.lower()
                        if 'docker containers running' in key_lower:
                            continue
                        if 'health check passed' in key_lower and '[process SOP]' in key:
                            continue

                        # Dedup by core (strip prefixes)
                        import re
                        core = re.sub(r'^\[process sop\]\s*', '', key_lower)
                        core = re.sub(r'^agent success:\s*', '', core)[:40]
                        if core in seen_cores:
                            continue
                        seen_cores.add(core)

                        learnings.append(key[:150])
            except Exception as e:
                print(f"[WARN] Memory learnings query failed: {e}")

        # Query Brain for recent patterns
        if self.brain:
            try:
                # Get recent wins that might be relevant
                state = self.brain.generate_brain_state()
                if state.recent_successes:
                    for success in state.recent_successes[:3]:
                        if isinstance(success, str) and any(d in success.lower() for d in domains):
                            learnings.append(f"Recent success: {success}")
            except Exception as e:
                print(f"[WARN] Brain state query failed: {e}")

        return learnings[:5]


# =============================================================================
# PUBLIC API
# =============================================================================

_professor = None

def consult(task: str = None, file: str = None, boundary_decision=None) -> str:
    """
    Consult the professor before starting work.

    This is THE function to call before any significant work.
    Returns distilled wisdom - not data, but insight.

    Args:
        task: What you're about to do
        file: File you're about to modify
        boundary_decision: Optional BoundaryDecision for project-scoped filtering.
            When provided, _get_additional_learnings() results are filtered
            to match the active project, preventing cross-project content leakage.

    Returns:
        Formatted guidance string
    """
    global _professor
    if _professor is None:
        _professor = Professor()

    guidance = _professor.consult(task=task, file=file, boundary_decision=boundary_decision)
    return guidance.format()


def get_wisdom(domain: str) -> dict:
    """
    Get the professor's wisdom for a specific domain.

    Args:
        domain: One of: async_python, docker_ecs, webrtc_livekit,
                aws_infrastructure, voice_pipeline, django_backend, memory_system

    Returns:
        Dict with first_principle, the_one_thing, landmines, pattern, context
    """
    return PROFESSOR_WISDOM.get(domain, {})


def list_domains() -> list:
    """List all domains the professor knows about."""
    return list(PROFESSOR_WISDOM.keys())


# =============================================================================
# PROFESSOR EVOLUTION - Feedback Loop for Continuous Improvement
# =============================================================================

EVOLUTION_FILE = MEMORY_DIR / ".professor_evolution.json"
DOMAIN_CONFIDENCE_FILE = MEMORY_DIR / ".professor_domain_confidence.json"

import logging as _logging
_professor_logger = _logging.getLogger(__name__)


def _load_domain_confidence() -> dict:
    """Load domain confidence scores from JSON sidecar.

    Structure: {domain: {score: float, adjustments: int, last_updated: str}}
    Default score for unknown domains = 0.7 (assume competent until proven otherwise).
    """
    if DOMAIN_CONFIDENCE_FILE.exists():
        try:
            with open(DOMAIN_CONFIDENCE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_domain_confidence(data: dict):
    """Save domain confidence scores to JSON sidecar."""
    try:
        with open(DOMAIN_CONFIDENCE_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        _professor_logger.warning(f"Domain confidence save failed: {e}")


class ProfessorEvolution:
    """
    Track professor guidance outcomes to evolve wisdom over time.

    Philosophy:
    - When professor guidance leads to SUCCESS → reinforce
    - When professor guidance leads to FAILURE → flag for review
    - When NEW patterns emerge from learnings → suggest additions

    This creates a feedback loop:
    Professor → Agent → Outcome → Evolution → Improved Professor
    """

    def __init__(self):
        self.data = self._load()

    def _load(self) -> dict:
        """Load evolution data from disk."""
        if EVOLUTION_FILE.exists():
            try:
                with open(EVOLUTION_FILE) as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] Evolution file load failed: {e}")
        return {
            "consultations": [],  # Recent consultations
            "domain_outcomes": {},  # domain -> {successes: n, failures: n}
            "suggested_additions": [],  # New wisdom to consider
            "flagged_for_review": [],  # Existing wisdom that led to failures
        }

    def _save(self):
        """Save evolution data to disk."""
        try:
            with open(EVOLUTION_FILE, "w") as f:
                json.dump(self.data, f, indent=2, default=str)
        except Exception as e:
            print(f"[WARN] Evolution file save failed: {e}")

    def record_consultation(self, task: str, domains: list, wisdom_given: dict):
        """
        Record that professor gave advice for a task.
        Called automatically when professor.consult() runs.
        """
        consultation = {
            "timestamp": datetime.now().isoformat(),
            "task": task[:200],
            "domains": domains,
            "wisdom_sections": list(wisdom_given.keys()),
            "outcome": None,  # To be filled by record_outcome
        }

        self.data["consultations"].append(consultation)

        # Keep last 100 consultations
        self.data["consultations"] = self.data["consultations"][-100:]
        self._save()

        return len(self.data["consultations"]) - 1  # Return index for outcome tracking

    def record_outcome(self, task_keywords: str, success: bool, insight: str = None):
        """
        Record outcome for a task that received professor advice.

        Called by:
        - PostToolUse hook on success → success=True
        - Error detection → success=False

        Args:
            task_keywords: Keywords to match against recent consultations
            success: Whether the task succeeded
            insight: Optional insight about what worked/failed
        """
        task_lower = task_keywords.lower()

        # Find most recent consultation matching this task
        for i in range(len(self.data["consultations"]) - 1, -1, -1):
            consultation = self.data["consultations"][i]
            if any(kw in consultation["task"].lower() for kw in task_lower.split()):
                consultation["outcome"] = "success" if success else "failure"

                # Update domain outcomes
                for domain in consultation["domains"]:
                    if domain not in self.data["domain_outcomes"]:
                        self.data["domain_outcomes"][domain] = {"successes": 0, "failures": 0}

                    if success:
                        self.data["domain_outcomes"][domain]["successes"] += 1
                    else:
                        self.data["domain_outcomes"][domain]["failures"] += 1

                        # Flag domain for review if failure rate > 30%
                        outcomes = self.data["domain_outcomes"][domain]
                        total = outcomes["successes"] + outcomes["failures"]
                        if total >= 5 and outcomes["failures"] / total > 0.3:
                            if domain not in self.data["flagged_for_review"]:
                                self.data["flagged_for_review"].append({
                                    "domain": domain,
                                    "reason": f"High failure rate: {outcomes['failures']}/{total}",
                                    "insight": insight,
                                    "timestamp": datetime.now().isoformat(),
                                })

                break

        self._save()

    def suggest_wisdom(self, domain: str, section: str, content: str, source: str = "learning"):
        """
        Suggest new wisdom to add to a domain.

        Called when:
        - A new gotcha is discovered (from fix captures)
        - A new pattern emerges (from consolidation)
        - An insight proves valuable repeatedly

        Args:
            domain: Which domain this belongs to
            section: "the_one_thing", "landmines", "pattern", or "context"
            content: The wisdom to add
            source: Where this suggestion came from
        """
        suggestion = {
            "domain": domain,
            "section": section,
            "content": content[:500],
            "source": source,
            "timestamp": datetime.now().isoformat(),
            "applied": False,
        }

        # Avoid duplicates
        for existing in self.data["suggested_additions"]:
            if existing["domain"] == domain and existing["content"][:100] == content[:100]:
                return  # Already suggested

        self.data["suggested_additions"].append(suggestion)
        self._save()

    def get_evolution_report(self) -> str:
        """Generate a report on professor evolution status."""
        lines = []
        lines.append("=" * 60)
        lines.append("PROFESSOR EVOLUTION REPORT")
        lines.append("=" * 60)

        # Domain success rates
        lines.append("\n📊 DOMAIN SUCCESS RATES:")
        for domain, outcomes in sorted(self.data["domain_outcomes"].items()):
            total = outcomes["successes"] + outcomes["failures"]
            if total > 0:
                rate = outcomes["successes"] / total * 100
                emoji = "✅" if rate >= 70 else "⚠️" if rate >= 50 else "❌"
                lines.append(f"  {emoji} {domain}: {rate:.0f}% ({outcomes['successes']}/{total})")

        # Flagged for review
        if self.data["flagged_for_review"]:
            lines.append("\n🚨 FLAGGED FOR REVIEW:")
            for flag in self.data["flagged_for_review"][-5:]:
                lines.append(f"  - {flag['domain']}: {flag['reason']}")

        # Suggested additions
        if self.data["suggested_additions"]:
            pending = [s for s in self.data["suggested_additions"] if not s["applied"]]
            if pending:
                lines.append(f"\n💡 PENDING WISDOM SUGGESTIONS: {len(pending)}")
                for sug in pending[-3:]:
                    lines.append(f"  - [{sug['domain']}] {sug['section']}: {sug['content'][:60]}...")

        lines.append("\n" + "=" * 60)
        return "\n".join(lines)

    def refine_from_outcomes(self, max_outcomes: int = 50) -> dict:
        """
        The Cerebellum loop — refine domain wisdom confidence from empirical outcomes.

        Reads completed outcomes from OutcomeTracker, correlates each outcome's
        task keywords with professor domains, and adjusts domain confidence scores.

        Confidence adjustment rules:
          - Success: +0.03 (reinforce — good advice)
          - Failure: -0.05 (penalize harder — bad advice is costly)
          - Clamped to [0.1, 0.95]
          - Domains below 0.4 after adjustment are flagged for review

        Stores results in .professor_domain_confidence.json sidecar.
        Only processes outcomes not yet fed back (feedback_applied=False from
        the refinement perspective — tracked via a local watermark).

        Returns:
            {
                "outcomes_processed": int,
                "domains_adjusted": {domain: {score, delta, direction}},
                "domains_flagged": [domain, ...],
                "refinement_log": [str, ...],
            }
        """
        try:
            from memory.outcome_tracker import OutcomeTracker, OUTCOME_DB
        except ImportError:
            return {"outcomes_processed": 0, "domains_adjusted": {},
                    "domains_flagged": [], "refinement_log": ["OutcomeTracker unavailable"]}

        # Load existing confidence scores and watermark
        confidence_data = _load_domain_confidence()
        watermark = confidence_data.get("_watermark", None)  # ISO timestamp of last processed outcome

        tracker = OutcomeTracker()
        completed = tracker.get_completed_outcomes(limit=max_outcomes, since=watermark)

        if not completed:
            return {"outcomes_processed": 0, "domains_adjusted": {},
                    "domains_flagged": [], "refinement_log": ["No new outcomes to process"]}

        refinement_log = []
        domains_adjusted = {}
        new_watermark = watermark

        for outcome in completed:
            task = outcome["task"]
            success = outcome["success"]
            end_time = outcome.get("end_time", "")

            # Update watermark to most recent end_time
            if end_time and (not new_watermark or end_time > new_watermark):
                new_watermark = end_time

            # Match outcome task to professor domains via keyword detection
            task_lower = task.lower()
            matched_domains = []
            for domain, keywords in DOMAIN_KEYWORDS.items():
                if any(kw in task_lower for kw in keywords):
                    matched_domains.append(domain)

            # Also check related_patterns for domain hints
            for pattern in outcome.get("related_patterns", []):
                pattern_lower = pattern.lower()
                for domain, keywords in DOMAIN_KEYWORDS.items():
                    if domain not in matched_domains and any(kw in pattern_lower for kw in keywords):
                        matched_domains.append(domain)

            if not matched_domains:
                continue  # Can't correlate — skip

            # Apply confidence adjustment
            delta = 0.03 if success else -0.05
            direction = "reinforced" if success else "penalized"

            for domain in matched_domains[:3]:  # Cap at 3 domains per outcome
                if domain not in confidence_data:
                    confidence_data[domain] = {
                        "score": 0.7,  # Default: assume competent
                        "adjustments": 0,
                        "successes": 0,
                        "failures": 0,
                        "last_updated": "",
                    }

                entry = confidence_data[domain]
                old_score = entry["score"]
                entry["score"] = max(0.1, min(0.95, old_score + delta))
                entry["adjustments"] = entry.get("adjustments", 0) + 1
                if success:
                    entry["successes"] = entry.get("successes", 0) + 1
                else:
                    entry["failures"] = entry.get("failures", 0) + 1
                entry["last_updated"] = end_time or datetime.now().isoformat()

                domains_adjusted[domain] = {
                    "score": round(entry["score"], 3),
                    "delta": round(entry["score"] - old_score, 3),
                    "direction": direction,
                }

                refinement_log.append(
                    f"{domain}: {old_score:.3f} -> {entry['score']:.3f} ({direction}, task='{task[:40]}')"
                )

        # Flag domains with low confidence for review
        domains_flagged = []
        for domain, entry in confidence_data.items():
            if domain.startswith("_"):
                continue
            if isinstance(entry, dict) and entry.get("score", 0.7) < 0.4:
                domains_flagged.append(domain)
                # Also add to evolution flagged_for_review if not already there
                already_flagged = any(
                    f.get("domain") == domain
                    for f in self.data.get("flagged_for_review", [])
                    if isinstance(f, dict)
                )
                if not already_flagged:
                    self.data.setdefault("flagged_for_review", []).append({
                        "domain": domain,
                        "reason": f"Outcome-based confidence dropped to {entry['score']:.2f}",
                        "timestamp": datetime.now().isoformat(),
                    })

        # Persist
        confidence_data["_watermark"] = new_watermark
        _save_domain_confidence(confidence_data)
        self._save()

        result = {
            "outcomes_processed": len(completed),
            "domains_adjusted": domains_adjusted,
            "domains_flagged": domains_flagged,
            "refinement_log": refinement_log,
        }

        if refinement_log:
            _professor_logger.info(
                f"Professor refinement: processed {len(completed)} outcomes, "
                f"adjusted {len(domains_adjusted)} domains, flagged {len(domains_flagged)}"
            )

        return result

    def decay_stale_confidence(self) -> dict:
        """
        NATURAL SELECTION — decay confidence for domains with no recent outcomes.

        Learnings that consistently help stay strong (reinforcement loop).
        Learnings that stop being relevant gradually fade (this method).
        Combined, this creates natural selection of wisdom.

        Decay tiers (based on time since last outcome):
          - 30+ days without outcome: -0.05
          - 60+ days without outcome: -0.10
          - 90+ days without outcome: -0.15

        Minimum confidence floor: 0.3 (never fully forget — old wisdom
        may still be relevant, just less prominently surfaced).

        Returns:
            {
                "domains_decayed": {domain: {old_score, new_score, days_stale, decay_applied}},
                "domains_at_floor": [domain, ...],
                "decay_log": [str, ...],
            }
        """
        confidence_data = _load_domain_confidence()
        now = datetime.now()

        domains_decayed = {}
        domains_at_floor = []
        decay_log = []
        floor = 0.3

        for domain, entry in confidence_data.items():
            if domain.startswith("_"):
                continue
            if not isinstance(entry, dict):
                continue

            last_updated = entry.get("last_updated", "")
            if not last_updated:
                # No timestamp — skip, can't determine staleness
                continue

            try:
                last_dt = datetime.fromisoformat(last_updated)
            except (ValueError, TypeError):
                decay_log.append(f"{domain}: invalid last_updated '{last_updated}', skipped")
                continue

            days_stale = (now - last_dt).days

            # Determine decay amount based on staleness tier
            if days_stale >= 90:
                decay = 0.15
            elif days_stale >= 60:
                decay = 0.10
            elif days_stale >= 30:
                decay = 0.05
            else:
                continue  # Fresh enough — no decay

            old_score = entry.get("score", 0.7)
            new_score = max(floor, old_score - decay)

            if new_score >= old_score:
                # Already at or below floor, no change needed
                if old_score <= floor:
                    domains_at_floor.append(domain)
                continue

            entry["score"] = round(new_score, 3)
            entry["last_decay"] = now.isoformat()
            entry["last_decay_days_stale"] = days_stale

            domains_decayed[domain] = {
                "old_score": round(old_score, 3),
                "new_score": round(new_score, 3),
                "days_stale": days_stale,
                "decay_applied": round(old_score - new_score, 3),
            }

            decay_log.append(
                f"{domain}: {old_score:.3f} -> {new_score:.3f} "
                f"(-{old_score - new_score:.3f}, {days_stale}d stale)"
            )

            if new_score <= floor:
                domains_at_floor.append(domain)

            # Flag heavily decayed domains for review
            if new_score < 0.4:
                already_flagged = any(
                    f.get("domain") == domain and "stale" in f.get("reason", "").lower()
                    for f in self.data.get("flagged_for_review", [])
                    if isinstance(f, dict)
                )
                if not already_flagged:
                    self.data.setdefault("flagged_for_review", []).append({
                        "domain": domain,
                        "reason": f"Confidence decayed to {new_score:.2f} ({days_stale}d stale)",
                        "timestamp": now.isoformat(),
                    })

        # Persist
        if domains_decayed:
            _save_domain_confidence(confidence_data)
            self._save()
            _professor_logger.info(
                f"Professor decay: {len(domains_decayed)} domains decayed, "
                f"{len(domains_at_floor)} at floor"
            )

        return {
            "domains_decayed": domains_decayed,
            "domains_at_floor": domains_at_floor,
            "decay_log": decay_log,
        }

    def merge_suggestions_to_runtime(self) -> int:
        """
        Merge approved/pending suggestions into the runtime PROFESSOR_WISDOM dict.

        This bridges the gap between suggest_wisdom() (which stores to JSON)
        and the runtime PROFESSOR_WISDOM dict (which was previously static).

        Suggestions are merged into the runtime dict so that subsequent
        professor queries in the same session benefit from accumulated learning.

        For 'landmines' section: appends to existing list (deduped).
        For other sections: appends with separator (preserves original).

        Returns:
            Number of suggestions merged into runtime.
        """
        global PROFESSOR_WISDOM

        pending = [s for s in self.data["suggested_additions"] if not s["applied"]]
        merged_count = 0

        for suggestion in pending:
            domain = suggestion["domain"]
            section = suggestion["section"]
            new_content = suggestion["content"]

            # Skip domains not in PROFESSOR_WISDOM
            if domain not in PROFESSOR_WISDOM:
                continue

            wisdom = PROFESSOR_WISDOM[domain]

            if section == "landmines":
                # Landmines is a list: append if not duplicate
                if isinstance(wisdom.get("landmines"), list):
                    # Dedup: check if similar content already exists
                    existing = [lm.lower().strip() for lm in wisdom["landmines"]]
                    if new_content.lower().strip()[:80] not in [e[:80] for e in existing]:
                        wisdom["landmines"].append(new_content)
                        suggestion["applied"] = True
                        merged_count += 1
            elif section in wisdom:
                # For string sections: append with separator (preserves original)
                existing = wisdom[section]
                if isinstance(existing, str) and new_content[:80] not in existing:
                    sep = " | [LEARNED] "
                    wisdom[section] = existing.rstrip() + sep + new_content
                    suggestion["applied"] = True
                    merged_count += 1

        if merged_count > 0:
            self._save()

        return merged_count

    def apply_learnings_to_wisdom(self) -> list:
        """
        Analyze recent learnings and suggest wisdom improvements.

        This is the EVOLUTION step - learning from actual outcomes.
        Called periodically by the brain consolidation cycle.

        Two sources feed wisdom:
        1. ContextDNA fix learnings -> landmine suggestions
        2. Observability store claims flagged_for_review -> domain-matched suggestions

        Returns:
            List of suggestions generated
        """
        suggestions = []

        # --- Source 1: ContextDNA fix learnings ---
        if CONTEXT_DNA_AVAILABLE:
            try:
                memory = ContextDNAClient()

                for domain in PROFESSOR_WISDOM.keys():
                    keywords = DOMAIN_KEYWORDS.get(domain, [domain])[:3]
                    query = " ".join(keywords)

                    learnings = memory.get_relevant_learnings(query, limit=5)

                    for learning in learnings:
                        l_type = learning.get("type", "")
                        content = learning.get("content", "")

                        # Fix learnings become landmines
                        if l_type == "fix" and len(content) > 50:
                            self.suggest_wisdom(
                                domain=domain,
                                section="landmines",
                                content=content,
                                source=f"learning:{learning.get('id', 'unknown')}"
                            )
                            suggestions.append({
                                "domain": domain,
                                "type": "landmine",
                                "from": learning.get("title", "")[:50]
                            })
            except Exception as e:
                print(f"[WARN] Wisdom suggestion extraction failed: {e}")

        # --- Source 2: Observability store flagged claims ---
        suggestions.extend(self._process_flagged_claims())

        # --- Merge pending suggestions into runtime PROFESSOR_WISDOM ---
        # This ensures the hardcoded dict evolves during the session,
        # bridging the gap between 'stored suggestions' and 'live wisdom'.
        merged = self.merge_suggestions_to_runtime()
        if merged > 0:
            print(f"[INFO] Professor wisdom: merged {merged} suggestions into runtime")

        return suggestions

    def _process_flagged_claims(self) -> list:
        """
        Pull claims with status='flagged_for_review' from observability store,
        match them to professor wisdom domains, suggest improvements, and
        mark as 'applied_to_wisdom'.

        Returns:
            List of suggestions generated from flagged claims
        """
        suggestions = []

        try:
            from memory.observability_store import get_observability_store
            store = get_observability_store()
        except Exception:
            return suggestions

        try:
            flagged = store.get_claims_by_status("flagged_for_review", limit=50)
        except Exception:
            return suggestions

        for claim in flagged:
            claim_id = claim.get("claim_id", "")
            statement = claim.get("statement", "")
            area = claim.get("area", "general")
            tags = claim.get("tags", [])
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []

            if not statement or len(statement) < 20:
                # Too short to be useful - mark processed and skip
                store.update_claim_status(claim_id, "applied_to_wisdom")
                continue

            # Match claim to professor domains using keywords + area
            search_text = f"{statement} {area} {' '.join(tags)}".lower()
            matched_domains = []
            for domain, keywords in DOMAIN_KEYWORDS.items():
                if any(kw in search_text for kw in keywords):
                    matched_domains.append(domain)

            if not matched_domains:
                matched_domains = ["memory_system"]  # Default domain

            # Determine which wisdom section this claim fits
            # High confidence claims -> pattern suggestions
            # Low confidence or failure-related -> landmine suggestions
            confidence = claim.get("weighted_confidence", claim.get("confidence", 0.5))
            section = "pattern" if confidence >= 0.7 else "landmines"

            for domain in matched_domains[:2]:  # Limit to 2 domains per claim
                self.suggest_wisdom(
                    domain=domain,
                    section=section,
                    content=statement,
                    source=f"observability_claim:{claim_id}"
                )
                suggestions.append({
                    "domain": domain,
                    "type": section,
                    "from": f"claim:{statement[:50]}",
                    "claim_id": claim_id,
                })

            # Mark claim as processed
            store.update_claim_status(claim_id, "applied_to_wisdom")

        return suggestions


# Global evolution tracker
_evolution = None

def get_evolution() -> ProfessorEvolution:
    """Get the global evolution tracker."""
    global _evolution
    if _evolution is None:
        _evolution = ProfessorEvolution()
    return _evolution


def record_professor_outcome(task: str, success: bool, insight: str = None):
    """
    Public API to record outcome for professor advice.

    Call this after a task completes to help the professor learn.

    Example:
        from memory.professor import record_professor_outcome
        record_professor_outcome("deploy django", success=True, insight="rollback plan saved us")
    """
    evolution = get_evolution()
    evolution.record_outcome(task, success, insight)


def get_evolution_report() -> str:
    """Get the professor evolution report."""
    return get_evolution().get_evolution_report()


def run_wisdom_update_loop() -> list:
    """
    Public API: Process flagged claims and apply learnings to wisdom.

    Pulls claims with status='flagged_for_review' from the observability store,
    matches them to professor wisdom domains, generates suggestions, and marks
    them as 'applied_to_wisdom'.

    Callable from:
    - agent_service background loop (brain.run_cycle -> ProfessorEvolution.apply_learnings_to_wisdom)
    - CLI: python professor.py --wisdom-loop
    - Cron/scheduler

    Returns:
        List of suggestions generated
    """
    evolution = get_evolution()
    return evolution.apply_learnings_to_wisdom()


def refine_from_outcomes(max_outcomes: int = 50) -> dict:
    """
    Public API: Run the Cerebellum refinement loop.

    Reads completed outcomes from OutcomeTracker, correlates with professor
    domains, adjusts confidence scores, flags underperforming domains.

    Callable from:
    - lite_scheduler.py (periodic job)
    - CLI: python professor.py --refine
    - Agent code: from memory.professor import refine_from_outcomes

    Returns:
        dict with outcomes_processed, domains_adjusted, domains_flagged,
        refinement_log
    """
    evolution = get_evolution()
    return evolution.refine_from_outcomes(max_outcomes=max_outcomes)


def decay_stale_confidence() -> dict:
    """
    Public API: Decay confidence for domains with no recent outcomes.

    Implements natural selection — learnings that stop being validated
    gradually fade, while reinforced learnings stay strong.

    Decay tiers: 30d=-0.05, 60d=-0.10, 90d=-0.15. Floor: 0.3.

    Callable from:
    - lite_scheduler.py (daily job: professor_confidence_decay)
    - CLI: python professor.py --decay
    - Agent code: from memory.professor import decay_stale_confidence

    Returns:
        dict with domains_decayed, domains_at_floor, decay_log
    """
    evolution = get_evolution()
    return evolution.decay_stale_confidence()


def get_domain_confidence(domain: str = None) -> dict:
    """
    Public API: Get current domain confidence scores.

    Args:
        domain: Specific domain, or None for all domains.

    Returns:
        Single domain entry or full confidence dict.
    """
    data = _load_domain_confidence()
    if domain:
        entry = data.get(domain, {"score": 0.7, "adjustments": 0})
        return {"domain": domain, **entry}
    # Filter out internal keys
    return {k: v for k, v in data.items() if not k.startswith("_")}


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("🎓 PROFESSOR - Nobel-Prize Level Context")
        print("")
        print("Usage:")
        print("  python professor.py <task description>")
        print("  python professor.py --file <filepath>")
        print("  python professor.py --domain <domain>")
        print("  python professor.py --list")
        print("  python professor.py --wisdom-loop")
        print("  python professor.py --refine")
        print("  python professor.py --decay")
        print("  python professor.py --domain-confidence")
        print("  python professor.py --evolution-report")
        print("")
        print("Examples:")
        print("  python professor.py 'configure LiveKit TURN server'")
        print("  python professor.py 'fix boto3 blocking in async code'")
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

    elif sys.argv[1] == "--wisdom-loop":
        print("Running wisdom update loop (flagged claims -> professor wisdom)...")
        results = run_wisdom_update_loop()
        if results:
            print(f"Processed {len(results)} suggestion(s):")
            for r in results:
                print(f"  [{r.get('domain')}] {r.get('type')}: {r.get('from', '')[:60]}")
        else:
            print("No flagged claims to process.")

    elif sys.argv[1] == "--refine":
        print("Running Cerebellum refinement loop (outcomes -> domain confidence)...")
        result = refine_from_outcomes()
        print(f"Outcomes processed: {result['outcomes_processed']}")
        if result["refinement_log"]:
            for line in result["refinement_log"]:
                print(f"  {line}")
        if result["domains_flagged"]:
            print(f"Domains flagged for review: {', '.join(result['domains_flagged'])}")
        if not result["refinement_log"]:
            print("No outcomes to refine from.")

    elif sys.argv[1] == "--decay":
        print("Running confidence decay (stale domains lose confidence)...")
        result = decay_stale_confidence()
        if result["decay_log"]:
            for line in result["decay_log"]:
                print(f"  {line}")
        if result["domains_at_floor"]:
            print(f"Domains at floor (0.3): {', '.join(result['domains_at_floor'])}")
        if not result["decay_log"]:
            print("No domains stale enough to decay.")

    elif sys.argv[1] == "--domain-confidence":
        scores = get_domain_confidence()
        if scores:
            print("Domain Confidence Scores:")
            for domain, entry in sorted(scores.items()):
                if isinstance(entry, dict):
                    s = entry.get("score", 0.7)
                    adj = entry.get("adjustments", 0)
                    marker = "LOW" if s < 0.4 else "OK" if s < 0.7 else "GOOD"
                    print(f"  {domain}: {s:.3f} ({adj} adjustments) [{marker}]")
        else:
            print("No domain confidence data yet. Run --refine first.")

    elif sys.argv[1] == "--evolution-report":
        print(get_evolution_report())

    else:
        task = " ".join(sys.argv[1:])
        print(consult(task=task))
