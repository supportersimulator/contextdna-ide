#!/usr/bin/env python3
"""
Seed the ER Simulator memory database from existing documentation.

This script extracts learnings from:
- BENCHMARK-RECORD.md (performance optimizations)
- LIVEKIT-CPU-MIGRATION.md (architecture decisions)
- Various code comments and docs

Run this once to populate the initial knowledge base.
"""

import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from ersim_memory import Memory


def seed_memory():
    """Seed the memory database with known learnings."""
    memory = Memory()

    # Clear existing (for fresh seed)
    memory.clear_all()

    print("Seeding ER Simulator memory database...\n")

    # =========================================================================
    # BUG FIXES
    # =========================================================================

    print("Adding bug fixes...")

    # boto3 async fix (critical)
    memory.add_bug_fix(
        symptom="6-concurrent LLM requests taking 14.56s total (should be ~1s)",
        root_cause="Synchronous boto3.converse() call blocks the asyncio event loop, causing sequential processing",
        resolution="Wrap boto3 calls in asyncio.to_thread(): response = await asyncio.to_thread(bedrock_client.converse, **params)",
        tags=["llm", "async", "boto3", "bedrock", "performance", "critical"],
        file_path="ersim-voice-stack/services/llm/app/main.py",
        line_number=212
    )

    # Whisper async fix
    memory.add_bug_fix(
        symptom="STT requests blocking other concurrent requests",
        root_cause="faster_whisper model.transcribe() is synchronous and blocks event loop",
        resolution="Wrap in asyncio.to_thread(): segments, info = await asyncio.to_thread(_transcribe)",
        tags=["stt", "async", "whisper", "performance"],
        file_path="ersim-voice-stack/services/stt/app/main.py",
        line_number=49
    )

    # Soundfile async fix
    memory.add_bug_fix(
        symptom="WAV encoding/decoding blocking audio pipeline",
        root_cause="soundfile sf.read() and sf.write() are blocking I/O operations",
        resolution="Wrap soundfile operations in asyncio.to_thread()",
        tags=["audio", "async", "soundfile", "wav"],
        file_path="ersim-voice-stack/services/agent/app/main.py",
        line_number=562
    )

    # TTS sample rate mismatch
    memory.add_bug_fix(
        symptom="InvalidState - sample_rate and num_channels don't match",
        root_cause="Agent code initializes LiveKit playout at 48000 Hz but KYUTAI_TTS_SAMPLE_RATE env var was 24000",
        resolution="Set KYUTAI_TTS_SAMPLE_RATE=48000 in agent .env (WebRTC standard is 48kHz)",
        tags=["tts", "audio", "sample-rate", "webrtc", "livekit", "kyutai"]
    )

    # GPU IP change on restart
    memory.add_bug_fix(
        symptom="Agent can't connect to GPU services after GPU restart",
        root_cause="GPU instance gets new private IP when ASG launches new instance",
        resolution="Use the 'Refresh Agent Config' button in voice toggle, or update agent .env with new GPU IP via SSM",
        tags=["gpu", "networking", "ecs", "asg", "agent"]
    )

    # Agent API key missing
    memory.add_bug_fix(
        symptom="Voice agent using generic fallback prompt instead of case-specific prompt",
        root_cause="LIVEKIT_AGENT_API_KEY not set in agent .env",
        resolution="Add LIVEKIT_AGENT_API_KEY to /opt/agent/.env with Django API key value",
        tags=["agent", "prompt", "django", "api-key"]
    )

    # Docker restart doesn't reload env
    memory.add_bug_fix(
        symptom="Docker restart doesn't pick up new environment variables",
        root_cause="docker restart only restarts the container, doesn't reload --env-file",
        resolution="Use docker stop && docker rm && docker run (recreate container) to reload env vars",
        tags=["docker", "env", "configuration"]
    )

    # ECS task not starting
    memory.add_bug_fix(
        symptom="ECS voice-inference service stuck at 0 running tasks",
        root_cause="GPU instance not registered with ECS cluster after ASG launch",
        resolution="Wait for ECS agent to register (~60s), then force new deployment: aws ecs update-service --force-new-deployment",
        tags=["ecs", "gpu", "asg", "deployment"]
    )

    # Bedrock model access denied
    memory.add_bug_fix(
        symptom="AccessDeniedException when calling Bedrock models",
        root_cause="Some models have agreementAvailability: NOT_AVAILABLE (Sonnet, Claude Instant)",
        resolution="Only use models with agreementAvailability: AVAILABLE (Haiku variants work)",
        tags=["bedrock", "llm", "aws", "model-access"]
    )

    # Lambda timing too fast
    memory.add_bug_fix(
        symptom="Lambda GPU toggle times out waiting for GPU to be ready",
        root_cause="GPU startup takes 3-5 minutes (ASG launch, ECS agent, image pull, container start)",
        resolution="Use 300s max wait with 15s intervals (20 attempts) instead of 120s/10s",
        tags=["lambda", "gpu", "timing", "asg"],
        file_path="ersim-voice-stack/infra/lambda/gpu_toggle.py"
    )

    # Cloudflare proxy breaks API Gateway
    memory.add_bug_fix(
        symptom="API Gateway custom domain returns 403 or SSL errors through Cloudflare",
        root_cause="Cloudflare proxy rewrites requests and breaks API Gateway's TLS termination",
        resolution="Set proxied=false for API Gateway DNS records (let API Gateway handle TLS with ACM)",
        tags=["cloudflare", "api-gateway", "dns", "tls"]
    )

    # =========================================================================
    # ARCHITECTURE DECISIONS
    # =========================================================================

    print("Adding architecture decisions...")

    memory.add_architecture_decision(
        decision="Separate LiveKit/Agent onto CPU instance (c6i.xlarge), keep STT/TTS/LLM on GPU (g5.xlarge)",
        rationale="WebRTC competes with CUDA workloads causing latency spikes and WebSocket connection issues",
        alternatives=["All services on GPU", "Kubernetes with resource limits", "ECS Fargate for LiveKit"],
        consequences="Adds ~$80/month but eliminates resource contention"
    )

    memory.add_architecture_decision(
        decision="Use hybrid LLM routing: direct models for low load (<=2 concurrent), cross-region for high load (>=3)",
        rationale="Direct regional models have lowest latency (~0.5s), but limited throughput. Cross-region provides spillover capacity with AWS availability-based routing",
        alternatives=["Single model", "Random rotation", "All cross-region"],
        consequences="Best latency for single users, automatic scaling for concurrent users"
    )

    memory.add_architecture_decision(
        decision="Use Internal NLB for GPU services instead of hardcoded private IPs",
        rationale="GPU IP changes on ASG restart. NLB provides stable DNS name and auto target registration",
        alternatives=["Hardcoded IP with manual updates", "Service discovery", "Route53 private hosted zone"]
    )

    memory.add_architecture_decision(
        decision="Agent connects to LiveKit on localhost (ws://localhost:7880), not public URL",
        rationale="Agent runs on same instance as LiveKit server, localhost avoids TLS overhead and external routing",
        alternatives=["wss://livekit.ersimulator.com (public)", "Internal DNS"]
    )

    memory.add_architecture_decision(
        decision="Use Cloudflare DNS NOT proxied for LiveKit (WebRTC needs direct UDP access)",
        rationale="WebRTC requires direct UDP connections for media. Cloudflare proxy only supports HTTP/HTTPS",
        alternatives=["Cloudflare proxied (doesn't work)", "No Cloudflare"],
        consequences="LiveKit DNS record must be orange cloud OFF"
    )

    memory.add_architecture_decision(
        decision="Use API Gateway custom domain with ACM certificate for Lambda endpoints",
        rationale="Provides clean URLs (voice.ersimulator.com) and handles TLS automatically with regional certificate",
        alternatives=["Raw API Gateway URL", "ALB with Lambda target", "CloudFront distribution"],
        consequences="Requires ACM wildcard cert (*.ersimulator.com) and Cloudflare proxied=false"
    )

    # =========================================================================
    # PERFORMANCE LESSONS
    # =========================================================================

    print("Adding performance lessons...")

    memory.add_performance_lesson(
        metric="6-concurrent LLM latency",
        before="14.56s average",
        after="0.52s average",
        technique="asyncio.to_thread() wrapper for boto3 calls",
        file_path="ersim-voice-stack/services/llm/app/main.py",
        tags=["llm", "async", "boto3", "critical"]
    )

    memory.add_performance_lesson(
        metric="Full pipeline (STT->LLM->TTS) at 6-concurrent",
        before="15.23s average",
        after="1.17s average",
        technique="All async fixes: boto3, whisper, soundfile",
        tags=["pipeline", "async", "performance"]
    )

    memory.add_performance_lesson(
        metric="20-concurrent LLM latency",
        before="3.41s with 26s outliers",
        after="1.00s with no outliers",
        technique="Expanded from 5 to 9 Bedrock models (more parallel capacity)",
        file_path="ersim-voice-stack/services/llm/app/main.py",
        tags=["llm", "bedrock", "scaling", "throughput"]
    )

    memory.add_performance_lesson(
        metric="LLM response length",
        before="Default 300 tokens",
        after="80 tokens for simulation phase",
        technique="Phase-based max_tokens: intro=200, simulation=80, debrief=250",
        file_path="ersim-voice-stack/services/agent/app/pipeline.py",
        tags=["llm", "tokens", "latency"]
    )

    memory.add_performance_lesson(
        metric="LLM inference parameters",
        before="Default temperature 0.7, top_p 1.0",
        after="temperature 0.4, top_p 0.9",
        technique="Lower temperature for more consistent, focused responses in voice simulation",
        file_path="ersim-voice-stack/services/llm/app/main.py",
        tags=["llm", "inference", "quality"]
    )

    memory.add_performance_lesson(
        metric="LLM streaming TTFB",
        before="Expected faster with streaming",
        after="12.8s streaming vs 1.3s non-streaming",
        technique="ABANDONED: boto3 streaming is synchronous per-chunk. Non-streaming with asyncio.to_thread is optimal",
        tags=["llm", "streaming", "abandoned"]
    )

    # =========================================================================
    # SUMMARY
    # =========================================================================

    all_memories = memory.get_all()
    counts = {}
    for m in all_memories:
        counts[m["kind"]] = counts.get(m["kind"], 0) + 1

    print(f"\nSeeded {len(all_memories)} memories:")
    for kind, count in counts.items():
        print(f"  - {kind}: {count}")

    print(f"\nMemory database seeded successfully!")
    print(f"   Database location: {Path(__file__).parent / 'ersim_memory.db'}")


if __name__ == "__main__":
    seed_memory()
