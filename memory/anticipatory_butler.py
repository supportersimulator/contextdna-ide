#!/usr/bin/env python3
"""
Anticipatory Butler - Follows the River, Sees the Map

Combines:
1. Strategic Planning (big picture reminders)
2. Anticipatory Context (predicts immediate needs)
3. LLM Thinking Mode (generative, not programmatic)
4. Redis Pre-Loading (ready before you ask)

THE VISION:
Butler follows dialogue like a river, anticipates what's needed around
the next bend, AND keeps the big map in view so you don't get lost.

Usage:
    from memory.anticipatory_butler import AnticipatorButler
    
    butler = AnticipatorButler()
    
    # Start following dialogue (background)
    butler.start_following()
    
    # When webhook fires, get anticipated + big picture
    context = butler.get_ready_context(current_prompt)
    
    # Returns:
    {
        "anticipated": {...},  # Pre-loaded immediate needs
        "big_picture": "...",  # Concise strategic reminder (if helpful)
        "source": "redis_preloaded"  # or "freshly_queried"
    }
"""

import asyncio
import json
import logging
import re
import requests
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent
# ALL LLM access routes through priority queue — NO direct HTTP to port 5044


class AnticipatorButler:
    """
    The Butler that follows conversation and anticipates needs.
    
    NOT programmatic - uses LLM thinking mode to be truly intelligent.
    """
    
    def __init__(self):
        try:
            import redis
            self.redis = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)
        except (ImportError, OSError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Redis unavailable in butler init: {e}")
            self.redis = None
        
        self.following = False
    
    async def start_following(self):
        """
        Start following dialogue in real-time (background process).
        
        Continuously:
        1. Watches for new messages
        2. LLM analyzes conversation flow
        3. Predicts what's needed next
        4. Pre-loads to Redis
        5. Keeps big picture in mind
        """
        self.following = True
        logger.info("🌊 Butler started following the river...")
        
        last_message_time = datetime.now()
        
        while self.following:
            try:
                # Check dialogue mirror for new messages
                from memory.dialogue_mirror import get_dialogue_mirror
                mirror = get_dialogue_mirror()
                
                # Get recent conversation
                # (In production, would use file watcher for instant detection)
                recent_messages = self._get_recent_dialogue(limit=5)
                
                if recent_messages and len(recent_messages) > 0:
                    latest = recent_messages[-1]
                    
                    # New message since last check?
                    message_time = datetime.fromisoformat(latest.get('timestamp', datetime.now().isoformat()))
                    
                    if message_time > last_message_time:
                        # New message! Analyze and anticipate
                        await self._analyze_and_anticipate(recent_messages)
                        last_message_time = message_time
                
                await asyncio.sleep(5)  # Check every 5 seconds
            
            except Exception as e:
                logger.error(f"Butler following error: {e}")
                await asyncio.sleep(10)
    
    def _get_recent_dialogue(self, limit: int = 5) -> List[Dict]:
        """Get recent dialogue messages."""
        try:
            from memory.db_utils import safe_conn

            from memory.db_utils import get_unified_db_path, unified_table
            db_path = get_unified_db_path(MEMORY_DIR / ".dialogue_mirror.db")
            if not db_path.exists():
                return []

            with safe_conn(str(db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT role, content, timestamp 
                    FROM messages 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (limit,))
                
                return [
                    {'role': row[0], 'content': row[1], 'timestamp': row[2]}
                    for row in reversed(cursor.fetchall())
                ]
        
        except Exception:
            return []
    
    async def _analyze_and_anticipate(self, recent_messages: List[Dict]):
        """
        Use LLM to analyze conversation and anticipate needs.
        
        LLM uses /think mode to reason about:
        1. What is user working on now?
        2. What will they likely need next?
        3. What context should be ready?
        4. Is this a good time for big picture reminder?
        """
        # Build conversation context
        dialogue = "\n".join([
            f"{msg['role']}: {msg['content'][:150]}"
            for msg in recent_messages
        ])
        
        prompt = f"""Recent conversation flow:

{dialogue}

ANALYZE with /think:
1. What is user working on RIGHT NOW?
2. What will they likely need in next 2-5 minutes?
3. What context should be pre-loaded to Redis?

Return JSON:
{{
  "current_task": "...",
  "anticipated_needs": ["topic1", "topic2", "topic3"],
  "big_picture_relevant": true/false
}}

/think then respond"""
        
        try:
            from memory.llm_priority_queue import butler_query
            content = butler_query(
                "You analyze conversation flow and anticipate user needs.",
                prompt,
                profile="extract"
            )

            if content:
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

                # Parse JSON
                match = re.search(r'```json\n(.*?)\n```', content, re.DOTALL)
                if match:
                    data = json.loads(match.group(1))
                else:
                    data = json.loads(content)

                # Pre-load anticipated contexts
                anticipated = data.get('anticipated_needs', [])
                if anticipated and self.redis:
                    await self._pre_load_contexts(anticipated)
                    logger.info(f"Pre-loaded {len(anticipated)} contexts: {anticipated}")

        except Exception as e:
            logger.debug(f"Anticipation analysis failed: {e}")
    
    async def _pre_load_contexts(self, topics: List[str]):
        """Pre-fetch and cache contexts in Redis."""
        if not self.redis:
            return
        
        from memory.context_dna_client import ContextDNAClient
        
        client = ContextDNAClient()
        
        for topic in topics[:5]:  # Max 5 topics
            try:
                # Query for this topic
                learnings = client.query_learnings(topic, limit=3)
                
                if learnings:
                    # Store in Redis with 120s TTL
                    key = f"anticipated:{topic}"
                    self.redis.setex(
                        key,
                        120,  # 2 minutes
                        json.dumps(learnings)
                    )
                    
                    logger.debug(f"✅ Pre-loaded: {topic}")
            
            except Exception as e:
                logger.debug(f"Failed to pre-load {topic}: {e}")
    
    def get_ready_context(self, prompt: str) -> Dict[str, Any]:
        """
        Get context that's ready (anticipated or fresh).
        
        Checks Redis first (0ms if anticipated),
        falls back to fresh query if not.
        
        Also includes big picture reminder if helpful.
        """
        result = {
            "anticipated": {},
            "big_picture": None,
            "source": "none"
        }
        
        # Check Redis for anticipated context
        if self.redis:
            # Try to find relevant anticipated context
            # (In production, would use semantic matching)
            anticipated_keys = self.redis.keys("anticipated:*")
            
            if anticipated_keys:
                result["source"] = "redis_preloaded"
                result["anticipated"] = {
                    key.replace("anticipated:", ""): json.loads(self.redis.get(key))
                    for key in anticipated_keys[:3]  # Top 3
                }
        
        # Get big picture reminder (LLM decides if helpful)
        try:
            from memory.strategic_planner import StrategicPlanner
            
            planner = StrategicPlanner()
            big_picture = planner.get_big_picture_reminder(prompt)
            
            if big_picture:
                result["big_picture"] = big_picture
        
        except Exception as e:
            logger.debug(f"Big picture failed: {e}")
        
        return result


if __name__ == "__main__":
    print("🧠 Anticipatory Butler - Test\n")
    
    butler = AnticipatorButler()
    
    # Test getting ready context
    context = butler.get_ready_context("working on webhook quality")
    
    print("📊 Ready Context:")
    print(f"  Source: {context['source']}")
    print(f"  Anticipated contexts: {len(context.get('anticipated', {}))}")
    
    if context.get('big_picture'):
        print(f"\n🗺️  Big Picture:")
        print(f"  {context['big_picture']}")
    else:
        print("\n  (No big picture reminder - not helpful right now)")
