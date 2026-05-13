#!/usr/bin/env python3
"""
Async Context Loader - Leverage Celery + RabbitMQ + Redis

Uses existing async infrastructure for INSTANT webhook context:

INFRASTRUCTURE (already running):
✅ Celery workers (contextdna-celery-worker)
✅ RabbitMQ (contextdna-rabbitmq) 
✅ Redis (contextdna-redis)

STRATEGY:
1. Celery tasks pre-load contexts in background (async)
2. Results stored in Redis (password-protected)
3. Webhook checks Redis first (0ms if cached)
4. Falls back to direct query if not cached

EXAMPLE FLOW:
User types message
    ↓ (in background, continuously)
Celery task: Pre-load common contexts every 60s
    ↓
Stores in Redis: context:webhooks, context:deployment, etc.
    ↓
User: "Let's deploy"
    ↓
Webhook: Check Redis for "deployment" (0ms - already there!)
    ↓
Returns instant context (no query delay)

Usage:
    from memory.async_context_loader import trigger_background_preload
    
    # Trigger async pre-loading (returns immediately)
    trigger_background_preload()
    
    # In webhook (check Redis first)
    context = get_from_redis_or_query("deployment")
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent

# Redis connection (with auth)
def get_redis_with_auth():
    """Get Redis client with password from environment."""
    import redis
    
    # Load password from Docker env
    env_file = MEMORY_DIR.parent / "context-dna" / "infra" / ".env"
    redis_password = None
    
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                if line.startswith('REDIS_PASSWORD='):
                    redis_password = line.split('=')[1].strip()
                    break
    
    if not redis_password:
        logger.warning("Redis password not found")
        return None
    
    try:
        client = redis.Redis(
            host='127.0.0.1',
            port=6379,
            password=redis_password,
            decode_responses=True,
            socket_connect_timeout=2
        )
        
        # Test connection
        client.ping()
        return client
    
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")
        return None


def trigger_background_preload(topics: List[str] = None):
    """
    Trigger Celery task to pre-load contexts in background.
    
    Returns immediately (async via RabbitMQ).
    """
    try:
        # Check if we're in Docker environment (has celery module)
        # If not, use HTTP API to trigger task
        
        # Method 1: Direct Celery (if in Docker)
        try:
            from memory.celery_tasks import refresh_relevance
            from memory.celery_config import safe_call_celery_task
            
            # Fire async (returns immediately)
            success = safe_call_celery_task(refresh_relevance)
            
            if success:
                logger.info("✅ Triggered background context pre-load via Celery")
                return True
        
        except ImportError:
            pass
        
        # Method 2: HTTP API to agent_service (triggers Celery tasks)
        import requests
        
        resp = requests.post(
            "http://127.0.0.1:8029/api/background/preload-contexts",
            json={"topics": topics or []},
            timeout=2
        )
        
        if resp.ok:
            logger.info("✅ Triggered background pre-load via API")
            return True
    
    except Exception as e:
        logger.debug(f"Background pre-load trigger failed: {e}")
    
    return False


def get_from_redis_or_query(topic: str) -> Optional[Dict]:
    """
    Get context from Redis (instant) or query if not cached.
    
    Returns:
        Context dict or None
    """
    client = get_redis_with_auth()
    if not client:
        return None
    
    # Check Redis first
    key = f"context:common:{topic}"
    
    try:
        cached = client.get(key)
        
        if cached:
            logger.info(f"✅ Redis hit: {topic} (0ms)")
            return json.loads(cached)
    
    except Exception as e:
        logger.debug(f"Redis get failed: {e}")
    
    # Not in Redis - query and cache
    try:
        from memory.context_dna_client import ContextDNAClient
        
        cdna = ContextDNAClient()
        learnings = cdna.query_learnings(topic, limit=5)
        
        if learnings:
            # Cache for next time
            client.setex(key, 3600, json.dumps(learnings))
            logger.info(f"📊 Queried and cached: {topic}")
            return learnings
    
    except Exception as e:
        logger.debug(f"Query failed: {e}")
    
    return None


if __name__ == "__main__":
    print("🚀 Async Context Loader - Leveraging Celery + Redis\n")
    
    # Test Redis connection
    client = get_redis_with_auth()
    if client:
        print("✅ Connected to Redis (with auth)")
        
        # Check what's cached
        keys = client.keys("context:*")
        print(f"📊 Cached contexts: {len(keys)}")
        
        for key in keys[:5]:
            print(f"   - {key}")
    else:
        print("❌ Redis connection failed")
    
    print("\n🔄 Triggering background pre-load...")
    success = trigger_background_preload()
    
    if success:
        print("✅ Background task queued (Celery will process)")
    else:
        print("⚠️  Could not trigger background task")
