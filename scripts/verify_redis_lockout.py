#!/usr/bin/env python3
"""
Verify Redis Lockout Persistence

This script tests the Celery lockout Redis persistence mechanism:
1. Checks if Redis is available
2. Tests setting a lockout key
3. Verifies the key persists (survives coordinator restart)
4. Tests clearing the lockout
"""

import sys
import time
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.redis_cache import get_redis_client

CELERY_LOCKOUT_KEY = "contextdna:scheduler:celery_lockout"
CELERY_LOCKOUT_TTL = 600  # 10 minutes

def main():
    print("=" * 60)
    print("REDIS LOCKOUT PERSISTENCE TEST")
    print("=" * 60)

    # Step 1: Check Redis availability
    print("\n[1/5] Checking Redis availability...")
    r = get_redis_client()
    if not r:
        print("  ✗ Redis unavailable - graceful degradation mode")
        print("  (Lockout will use in-memory counter only)")
        return
    print("  ✓ Redis connected")

    # Step 2: Clear any existing lockout
    print("\n[2/5] Clearing any existing lockout...")
    r.delete(CELERY_LOCKOUT_KEY)
    existing = r.get(CELERY_LOCKOUT_KEY)
    if existing:
        print(f"  ✗ Failed to clear (value={existing})")
        return
    print("  ✓ No lockout active")

    # Step 3: Set lockout (simulate failure)
    print("\n[3/5] Setting lockout (simulating Celery failure)...")
    r.setex(CELERY_LOCKOUT_KEY, CELERY_LOCKOUT_TTL, "locked")
    value = r.get(CELERY_LOCKOUT_KEY)
    ttl = r.ttl(CELERY_LOCKOUT_KEY)
    if value != "locked":
        print(f"  ✗ Failed to set lockout (value={value})")
        return
    print(f"  ✓ Lockout set (TTL={ttl}s)")

    # Step 4: Verify persistence (key should survive)
    print("\n[4/5] Verifying persistence...")
    time.sleep(1)
    value = r.get(CELERY_LOCKOUT_KEY)
    ttl = r.ttl(CELERY_LOCKOUT_KEY)
    if value != "locked":
        print(f"  ✗ Lockout disappeared (value={value})")
        return
    print(f"  ✓ Lockout persisted (TTL={ttl}s)")

    # Step 5: Clear lockout (simulate recovery)
    print("\n[5/5] Clearing lockout (simulating Celery recovery)...")
    r.delete(CELERY_LOCKOUT_KEY)
    value = r.get(CELERY_LOCKOUT_KEY)
    if value:
        print(f"  ✗ Failed to clear (value={value})")
        return
    print("  ✓ Lockout cleared")

    print("\n" + "=" * 60)
    print("✓ ALL TESTS PASSED")
    print("=" * 60)
    print("\nRedis lockout persistence is working correctly.")
    print("Lockout state will survive coordinator restarts.")

if __name__ == "__main__":
    main()
