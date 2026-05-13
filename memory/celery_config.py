#!/usr/bin/env python3
"""
Context DNA Celery Configuration - The Subconscious Nervous System

This module configures Celery to connect to RabbitMQ (message broker) and Redis
(result backend + cache). It's the nervous system that enables background agents
to work autonomously.

ARCHITECTURE (from ChatGPT's Synaptic foundation):
- Celery = nervous system (scheduling, routing, backpressure, retries)
- Agents = brains (scanner, distiller, relevance finder, LLM manager)
- PostgreSQL/Redis = memory + coordination
- Local LLM = cognition engine

SERVICES USED (from context-dna/docker-compose.yml):
- context-dna-rabbitmq: Message broker (://:password@
- context-dna-redis: Cache + pub/sub (redis://localhost:6379)
- context-dna-postgres: PostgreSQL (localhost:5432, databases: context_dna + contextdna)

Usage:
    # Start a worker
    celery -A memory.celery_config worker --loglevel=info

    # Start beat scheduler (for periodic tasks)
    celery -A memory.celery_config beat --loglevel=info

    # Start both together
    celery -A memory.celery_config worker --beat --loglevel=info
"""

import os
from celery import Celery
from celery.schedules import crontab
from kombu import Queue, Exchange

# =============================================================================
# ENVIRONMENT CONFIGURATION
# =============================================================================

# RabbitMQ configuration (broker)
# Matches context-dna/docker-compose.yml (context-dna-rabbitmq on standard port 5672)
RABBITMQ_USER = os.environ.get('RABBITMQ_USER', 'context_dna')
RABBITMQ_PASSWORD = os.environ.get('RABBITMQ_PASSWORD', 'context_dna_dev')
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', '127.0.0.1')
RABBITMQ_PORT = os.environ.get('RABBITMQ_PORT', '5672')
RABBITMQ_VHOST = os.environ.get('RABBITMQ_VHOST', '/')

# Redis configuration (result backend + cache)
# CD_REDIS_* avoids .env hijacking (REDIS_* is for contextdna-redis on 16379)
# Python code connects to context-dna-redis on 6379, no auth
REDIS_PASSWORD = os.environ.get('CD_REDIS_PASSWORD', '')
REDIS_HOST = os.environ.get('CD_REDIS_HOST', '127.0.0.1')
REDIS_PORT = os.environ.get('CD_REDIS_PORT', '6379')

# Build connection URLs
BROKER_URL = f"amqp://{RABBITMQ_USER}:YOUR_PASSWORD@{RABBITMQ_HOST}:{RABBITMQ_PORT}/{RABBITMQ_VHOST}"
RESULT_BACKEND = f"redis://{REDIS_HOST}:{REDIS_PORT}/0" if not REDIS_PASSWORD else f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"

# =============================================================================
# CELERY APP INITIALIZATION
# =============================================================================

app = Celery(
    'contextdna',
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=[
        'memory.celery_tasks',  # Our background agent tasks
    ]
)

# =============================================================================
# CELERY CONFIGURATION
# =============================================================================

app.conf.update(
    # Task serialization
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',

    # Timezone
    timezone='UTC',
    enable_utc=True,

    # Task execution settings
    task_acks_late=True,  # Acknowledge after task completes (reliability)
    task_reject_on_worker_lost=True,  # Requeue if worker dies

    # Result settings
    result_expires=3600,  # Results expire after 1 hour

    # CRITICAL: Fast-fail settings to prevent webhook timeouts
    # When Redis backend is unavailable, don't block for 20+ seconds retrying
    broker_connection_retry_on_startup=False,  # Don't retry broker on startup
    broker_connection_max_retries=2,           # Max 2 retries (not 20!)
    redis_retry_on_timeout=False,              # Don't retry on Redis timeout
    redis_socket_connect_timeout=1.0,          # 1 second connect timeout
    redis_socket_timeout=1.0,                  # 1 second socket timeout
    result_backend_transport_options={
        'retry_policy': {
            'max_retries': 1,                  # Only 1 retry for result backend
            'interval_start': 0,               # Start immediately
            'interval_step': 0.1,              # 100ms between retries
            'interval_max': 0.5,               # Max 500ms total
        },
        'socket_connect_timeout': 1,           # 1 second connect timeout
        'socket_timeout': 1,                   # 1 second timeout
    },

    # Worker settings
    worker_prefetch_multiplier=1,  # One task at a time per worker (for heavy tasks)
    worker_concurrency=4,  # 4 concurrent workers per process

    # Task routing - direct specific tasks to specific queues
    task_routes={
        # Scanner tasks - high priority, fast
        'memory.celery_tasks.scan_project': {'queue': 'scanner'},
        'memory.celery_tasks.detect_hierarchy': {'queue': 'scanner'},
        'memory.celery_tasks.save_hierarchy_profile': {'queue': 'scanner'},

        # Distiller tasks - lower priority, can be slow
        'memory.celery_tasks.distill_skills': {'queue': 'distiller'},
        'memory.celery_tasks.consolidate_patterns': {'queue': 'distiller'},

        # Relevance tasks - medium priority
        'memory.celery_tasks.refresh_relevance': {'queue': 'relevance'},
        'memory.celery_tasks.update_context_pack': {'queue': 'relevance'},

        # LLM tasks - separate queue (can be slow and resource-intensive)
        'memory.celery_tasks.llm_analyze': {'queue': 'llm'},
        'memory.celery_tasks.llm_health_check': {'queue': 'llm'},

        # Brain tasks - orchestration
        'memory.celery_tasks.brain_cycle': {'queue': 'brain'},
        'memory.celery_tasks.success_detection': {'queue': 'brain'},

        # Boundary Intelligence tasks - A/B feedback loop
        'memory.celery_tasks.record_boundary_decision': {'queue': 'brain'},
        'memory.celery_tasks.process_boundary_feedback': {'queue': 'brain'},
        'memory.celery_tasks.decay_boundary_associations': {'queue': 'brain'},
        'memory.celery_tasks.record_clarification_response': {'queue': 'brain'},
        'memory.celery_tasks.track_file_activity': {'queue': 'scanner'},

        # Dialogue Mirror tasks - Synaptic's eyes and ears
        'memory.celery_tasks.mirror_dialogue': {'queue': 'relevance'},
        'memory.celery_tasks.analyze_dialogue_patterns': {'queue': 'distiller'},
        'memory.celery_tasks.cleanup_old_dialogue': {'queue': 'distiller'},
        'memory.celery_tasks.sync_dialogue_to_synaptic': {'queue': 'relevance'},
        'memory.celery_tasks.get_dialogue_context': {'queue': 'relevance'},

        # Evidence pipeline tasks - Critical wisdom promotion bridge
        'memory.celery_tasks.promote_trusted_to_wisdom': {'queue': 'brain'},
        'memory.celery_tasks.evaluate_quarantine': {'queue': 'brain'},
        'memory.celery_tasks.compute_rollups': {'queue': 'brain'},
        'memory.celery_tasks.ttl_decay': {'queue': 'brain'},
        'memory.celery_tasks.injection_health': {'queue': 'brain'},

        # Butler tasks - Autonomous maintenance
        'memory.celery_tasks.hindsight_check': {'queue': 'distiller'},
        'memory.celery_tasks.failure_pattern_analysis': {'queue': 'distiller'},
        'memory.celery_tasks.skeletal_integrity': {'queue': 'distiller'},
        'memory.celery_tasks.mmotw_repair_mining': {'queue': 'distiller'},
        'memory.celery_tasks.sop_dedup_analysis': {'queue': 'distiller'},
        'memory.celery_tasks.post_session_meta_analysis': {'queue': 'distiller'},
        'memory.celery_tasks.codebase_map_refresh': {'queue': 'scanner'},
    },

    # Define queues
    task_queues=(
        Queue('scanner', Exchange('scanner'), routing_key='scanner'),
        Queue('distiller', Exchange('distiller'), routing_key='distiller'),
        Queue('relevance', Exchange('relevance'), routing_key='relevance'),
        Queue('llm', Exchange('llm'), routing_key='llm'),
        Queue('brain', Exchange('brain'), routing_key='brain'),
        Queue('dialogue', Exchange('dialogue'), routing_key='dialogue'),  # Dialogue mirror
        Queue('celery', Exchange('celery'), routing_key='celery'),  # Default
    ),

    # Default queue
    task_default_queue='celery',
)

# =============================================================================
# BEAT SCHEDULE (Periodic Tasks)
# =============================================================================
# This is the "heartbeat" of the subconscious mind

app.conf.beat_schedule = {
    # Scanner: Check for file changes every 30 seconds
    'scan-projects-every-30s': {
        'task': 'memory.celery_tasks.scan_project',
        'schedule': 30.0,
        'args': (),
        'options': {'queue': 'scanner'},
    },

    # Brain cycle: Run consolidation every 5 minutes
    'brain-cycle-every-5m': {
        'task': 'memory.celery_tasks.brain_cycle',
        'schedule': 300.0,
        'args': (),
        'options': {'queue': 'brain'},
    },

    # Success detection: Check work log every 60 seconds
    'success-detection-every-60s': {
        'task': 'memory.celery_tasks.success_detection',
        'schedule': 60.0,
        'args': (),
        'options': {'queue': 'brain'},
    },

    # Relevance refresh: Update context packs every 2 minutes
    'relevance-refresh-every-2m': {
        'task': 'memory.celery_tasks.refresh_relevance',
        'schedule': 120.0,
        'args': (),
        'options': {'queue': 'relevance'},
    },

    # LLM health check: Verify Ollama is responsive every 5 minutes
    'llm-health-every-5m': {
        'task': 'memory.celery_tasks.llm_health_check',
        'schedule': 300.0,
        'args': (),
        'options': {'queue': 'llm'},
    },

    # Skill distillation: Run every 30 minutes
    'distill-skills-every-30m': {
        'task': 'memory.celery_tasks.distill_skills',
        'schedule': 1800.0,
        'args': (),
        'options': {'queue': 'distiller'},
    },

    # Pattern consolidation: Run every hour
    'consolidate-patterns-hourly': {
        'task': 'memory.celery_tasks.consolidate_patterns',
        'schedule': 3600.0,
        'args': (),
        'options': {'queue': 'distiller'},
    },

    # Boundary Intelligence: Decay associations every 5 minutes
    'boundary-decay-every-5m': {
        'task': 'memory.celery_tasks.decay_boundary_associations',
        'schedule': 300.0,
        'args': (),
        'options': {'queue': 'brain'},
    },

    # =================================================================
    # DIALOGUE MIRROR TASKS - Synaptic's Eyes and Ears
    # =================================================================

    # Sync dialogue context to Synaptic awareness every 2 minutes
    'sync-dialogue-every-2m': {
        'task': 'memory.celery_tasks.sync_dialogue_to_synaptic',
        'schedule': 120.0,
        'args': (),
        'options': {'queue': 'relevance'},
    },

    # Analyze dialogue patterns every 15 minutes
    'analyze-dialogue-every-15m': {
        'task': 'memory.celery_tasks.analyze_dialogue_patterns',
        'schedule': 900.0,
        'args': (),
        'options': {'queue': 'distiller'},
    },

    # Clean up old dialogue daily at 3 AM
    'cleanup-dialogue-daily': {
        'task': 'memory.celery_tasks.cleanup_old_dialogue',
        'schedule': crontab(hour=3, minute=0),
        'args': (30,),  # Keep 30 days of dialogue
        'options': {'queue': 'distiller'},
    },

    # =================================================================
    # EVIDENCE PIPELINE TASKS - Critical bridge for wisdom promotion
    # =================================================================

    # Promote trusted claims → flagged_for_review (every 10 minutes)
    # Bridges quarantine promotion to professor wisdom review
    'promote-trusted-to-wisdom-every-10m': {
        'task': 'memory.celery_tasks.promote_trusted_to_wisdom',
        'schedule': 600.0,
        'args': (),
        'options': {'queue': 'brain'},
    },

    # Evaluate quarantine status (every 5 minutes)
    # Promotes quarantined → trusted when outcome thresholds met
    'evaluate-quarantine-every-5m': {
        'task': 'memory.celery_tasks.evaluate_quarantine',
        'schedule': 300.0,
        'args': (),
        'options': {'queue': 'brain'},
    },

    # Compute A/B rollups + SOP outcome scores (every 60 seconds)
    'compute-rollups-every-60s': {
        'task': 'memory.celery_tasks.compute_rollups',
        'schedule': 60.0,
        'args': (),
        'options': {'queue': 'brain'},
    },

    # TTL decay: expire stale claims (every hour)
    'ttl-decay-hourly': {
        'task': 'memory.celery_tasks.ttl_decay',
        'schedule': 3600.0,
        'args': (),
        'options': {'queue': 'brain'},
    },

    # =================================================================
    # WEBHOOK HEALTH MONITORING
    # =================================================================

    # Injection health monitor (every 60 seconds)
    'injection-health-every-60s': {
        'task': 'memory.celery_tasks.injection_health',
        'schedule': 60.0,
        'args': (),
        'options': {'queue': 'brain'},
    },

    # =================================================================
    # BUTLER TASKS - Autonomous maintenance agents
    # =================================================================

    # Hindsight validation: verify wins, emit negative signals (every 10 min)
    'hindsight-check-every-10m': {
        'task': 'memory.celery_tasks.hindsight_check',
        'schedule': 600.0,
        'args': (),
        'options': {'queue': 'distiller'},
    },

    # Failure pattern analysis: LANDMINE warnings for Section 2 (every 4 hours)
    'failure-patterns-every-4h': {
        'task': 'memory.celery_tasks.failure_pattern_analysis',
        'schedule': 14400.0,
        'args': (),
        'options': {'queue': 'distiller'},
    },

    # Skeletal integrity: self-healing DB repair (every 30 minutes)
    'skeletal-integrity-every-30m': {
        'task': 'memory.celery_tasks.skeletal_integrity',
        'schedule': 1800.0,
        'args': (),
        'options': {'queue': 'distiller'},
    },

    # MMOTW repair mining: extract repair SOPs from dialogue (every 2 hours)
    'mmotw-mining-every-2h': {
        'task': 'memory.celery_tasks.mmotw_repair_mining',
        'schedule': 7200.0,
        'args': (),
        'options': {'queue': 'distiller'},
    },

    # SOP deduplication analysis: library hygiene (every 4 hours)
    'sop-dedup-every-4h': {
        'task': 'memory.celery_tasks.sop_dedup_analysis',
        'schedule': 14400.0,
        'args': (),
        'options': {'queue': 'distiller'},
    },

    # Post-session meta-analysis: Evidence-Based Section 11 (every 30 minutes)
    'meta-analysis-every-30m': {
        'task': 'memory.celery_tasks.post_session_meta_analysis',
        'schedule': 1800.0,
        'args': (),
        'options': {'queue': 'distiller'},
    },

    # Codebase map refresh: architecture graph cache (every 5 minutes)
    'codebase-map-refresh-every-5m': {
        'task': 'memory.celery_tasks.codebase_map_refresh',
        'schedule': 300.0,
        'args': (),
        'options': {'queue': 'scanner'},
    },
}

# =============================================================================
# TASK ERROR HANDLING
# =============================================================================

app.conf.task_annotations = {
    # Scanner tasks: fast, retry quickly
    'memory.celery_tasks.scan_project': {
        'rate_limit': '10/m',  # Max 10 per minute (debounce)
        'max_retries': 3,
        'default_retry_delay': 5,
    },

    # Distiller tasks: slow, retry with backoff
    'memory.celery_tasks.distill_skills': {
        'rate_limit': '2/m',
        'max_retries': 2,
        'default_retry_delay': 60,
    },

    # LLM tasks: resource-intensive, careful retry
    'memory.celery_tasks.llm_analyze': {
        'rate_limit': '5/m',
        'max_retries': 2,
        'default_retry_delay': 30,
    },
}

# =============================================================================
# CELERY SIGNALS (Hooks)
# =============================================================================

from celery.signals import task_success, task_failure, worker_ready

@task_success.connect
def task_success_handler(sender=None, result=None, **kwargs):
    """Log successful task completion."""
    import logging
    logger = logging.getLogger('contextdna.celery')
    logger.info(f"Task {sender.name} completed successfully")

@task_failure.connect
def task_failure_handler(sender=None, exception=None, **kwargs):
    """Log task failures for debugging."""
    import logging
    logger = logging.getLogger('contextdna.celery')
    logger.error(f"Task {sender.name} failed: {exception}")

@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Log when worker is ready."""
    import logging
    logger = logging.getLogger('contextdna.celery')
    logger.info("Context DNA Celery worker ready - Subconscious mind activated")


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    app.start()
