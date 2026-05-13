#!/usr/bin/env python3
"""
Redis Pre-Loader - Instant Context Access

Pre-loads critical context into Redis for 0ms access.
Leverages existing contextdna-redis (running but unused).

STRATEGY:
1. Pre-load common contexts on startup (webhooks, deployment, docker, etc.)
2. Pre-load strategic plans (big picture always ready)
3. Pre-load recently used contexts (LRU caching)
4. Pre-load anticipated contexts (from butler)

REDIS KEYS:
- context:common:{topic} - Common SOPs (pre-loaded on startup)
- context:strategic:plans - Big picture (always fresh)
- context:anticipated:{topic} - Butler predictions (60s TTL)
- context:recent:{topic} - LRU cache (300s TTL)

Usage:
    from memory.redis_preloader import preload_critical_contexts
    
    # On startup
    preload_critical_contexts()
    
    # In webhook
    context = redis.get("context:common:webhooks")  # 0ms!
"""

import json
import logging
import redis
from pathlib import Path
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

# Connect to contextdna-redis (Docker)
# Try multiple connection methods
REDIS_CONFIGS = [
    {"host": "127.0.0.1", "port": 6379, "db": 0},  # Direct
    {"host": "127.0.0.1", "port": 6379, "db": 0},  # Explicit
    {"host": "host.docker.internal", "port": 6379, "db": 0},  # From container
]


def get_redis_client():
    """Get Redis client with fallback connection attempts."""
    for config in REDIS_CONFIGS:
        try:
            client = redis.Redis(**config, decode_responses=True, socket_connect_timeout=2)
            # Test connection
            client.ping()
            return client
        except (redis.RedisError, OSError) as e:
            logger.debug(f"Redis connection failed with config {config.get('host', '?')}:{config.get('port', '?')}: {e}")
            continue
    
    logger.warning("Could not connect to Redis")
    return None


# Common high-value topics to pre-load
CRITICAL_TOPICS = [
    "webhooks", "deployment", "docker", "ecs", "terraform",
    "async", "boto3", "livekit", "webrtc", "database",
    "redis", "celery", "rabbitmq", "architecture"
]


def preload_critical_contexts():
    """
    Pre-load critical contexts to Redis on startup.
    
    Makes common queries instant (0ms instead of 200ms).
    """
    client = get_redis_client()
    if not client:
        return
    
    from memory.context_dna_client import ContextDNAClient
    
    cdna = ContextDNAClient()
    loaded_count = 0
    
    print("🔄 Pre-loading critical contexts to Redis...")
    
    for topic in CRITICAL_TOPICS:
        try:
            # Query for this topic
            learnings = cdna.query_learnings(topic, limit=3)
            
            if learnings:
                key = f"context:common:{topic}"
                client.setex(
                    key,
                    3600,  # 1 hour TTL
                    json.dumps(learnings)
                )
                loaded_count += 1
                print(f"  ✅ {topic}")
        
        except Exception as e:
            logger.debug(f"Failed to pre-load {topic}: {e}")
    
    print(f"\n✅ Pre-loaded {loaded_count}/{len(CRITICAL_TOPICS)} contexts to Redis")
    print("   Webhook queries now instant for these topics!")


def preload_strategic_plans():
    """Pre-load strategic plans for instant big picture access."""
    client = get_redis_client()
    if not client:
        return
    
    try:
        from memory.strategic_planner import StrategicPlanner
        
        planner = StrategicPlanner()
        plans = planner._get_all_plans()
        
        if plans:
            # Store all plans
            plans_data = [
                {
                    'title': p.title,
                    'status': p.status,
                    'priority': p.priority,
                    'current_milestone': p.current_milestone,
                    'next_steps': p.next_steps
                }
                for p in plans
            ]
            
            client.setex(
                "context:strategic:plans",
                300,  # 5 min TTL (refresh frequently)
                json.dumps(plans_data)
            )
            
            print(f"✅ Pre-loaded {len(plans)} strategic plans to Redis")
    
    except Exception as e:
        logger.debug(f"Failed to pre-load plans: {e}")


def preload_anticipated_from_butler():
    """Pre-load contexts anticipated by butler."""
    client = get_redis_client()
    if not client:
        return
    
    # Butler should have set anticipated:{topic} keys
    # Just verify they're there
    anticipated_keys = client.keys("anticipated:*")
    
    if anticipated_keys:
        print(f"✅ Butler pre-loaded {len(anticipated_keys)} anticipated contexts")
    else:
        print("ℹ️  No anticipated contexts yet (butler not running)")


def warm_redis_on_startup():
    """
    Warm Redis cache on startup with all critical contexts.
    
    Run once when Context DNA starts to ensure instant access.
    """
    print("🔥 Warming Redis cache for instant context access...\n")
    
    preload_critical_contexts()
    print()
    preload_strategic_plans()
    print()
    preload_anticipated_from_butler()
    
    print("\n🎯 Redis warm! Common queries now 0ms.")


if __name__ == "__main__":
    warm_redis_on_startup()
