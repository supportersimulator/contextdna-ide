#!/usr/bin/env python3
"""
Performance Tracker - Measure and Evaluate LLM Config Optimization

Tracks:
1. Actual performance (tok/s, latency, memory)
2. Success/failure rates
3. Optimization score (vs theoretical hardware potential)
4. Community feedback

Evaluates:
- Is this config optimized for the hardware?
- Could we do better?
- What's the best alternative?

Database:
- performance_metrics table (actual measurements)
- optimization_evaluations table (Python-evaluated scores)

Usage:
    from memory.performance_tracker import track_llm_execution
    
    # After LLM generation
    track_llm_execution(
        config_id="qwen25-14b-mlx",
        tokens_generated=50,
        latency_ms=3200,
        memory_mb=950,
        success=True
    )
    
    # System automatically:
    # 1. Records metrics
    # 2. Evaluates optimization
    # 3. Updates community stats
    # 4. Suggests improvements if suboptimal
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent
PERF_DB = MEMORY_DIR / ".performance_metrics.db"


@dataclass
class ExecutionMetrics:
    """Single LLM execution measurement."""
    execution_id: str
    config_id: str
    hardware_hash: str
    
    tokens_generated: int
    latency_ms: int
    memory_mb: int
    tokens_per_sec: float
    
    success: bool
    error_message: Optional[str]
    
    timestamp: str


class PerformanceTracker:
    """Track and evaluate LLM performance."""
    
    def __init__(self, db_path: Path = PERF_DB):
        self.db_path = db_path
        self._ensure_schema()
    
    def _ensure_schema(self):
        """Create performance tracking schema."""
        with sqlite3.connect(str(self.db_path)) as conn:
            # Raw execution metrics
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_executions (
                    execution_id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    hardware_hash TEXT NOT NULL,
                    
                    tokens_generated INTEGER,
                    latency_ms INTEGER,
                    memory_mb INTEGER,
                    tokens_per_sec REAL,
                    
                    success BOOLEAN,
                    error_message TEXT,
                    
                    timestamp TEXT NOT NULL
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_exec_config_hw 
                ON llm_executions(config_id, hardware_hash, timestamp DESC)
            """)
            
            # Aggregated metrics (computed periodically)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS performance_aggregates (
                    aggregate_id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    hardware_hash TEXT NOT NULL,
                    
                    sample_size INTEGER,
                    success_count INTEGER,
                    failure_count INTEGER,
                    success_rate REAL,
                    
                    avg_tokens_per_sec REAL,
                    p50_tokens_per_sec REAL,
                    p95_tokens_per_sec REAL,
                    
                    avg_latency_ms INTEGER,
                    avg_memory_mb INTEGER,
                    
                    optimization_score REAL,
                    
                    last_updated TEXT,
                    
                    UNIQUE(config_id, hardware_hash)
                )
            """)
            
            # Optimization evaluations (Python-computed)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS optimization_evaluations (
                    eval_id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    hardware_hash TEXT NOT NULL,
                    
                    is_optimized BOOLEAN,
                    optimization_score REAL,
                    
                    bottleneck TEXT,
                    suggestion TEXT,
                    
                    evaluated_at TEXT
                )
            """)
            
            conn.commit()
    
    def track_execution(
        self,
        config_id: str,
        tokens_generated: int,
        latency_ms: int,
        memory_mb: int,
        success: bool,
        error_message: Optional[str] = None
    ):
        """Track a single LLM execution."""
        from memory.hardware_fingerprint import get_hardware_profile
        
        profile = get_hardware_profile()
        execution_id = f"exec_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        
        tokens_per_sec = (tokens_generated / latency_ms * 1000) if latency_ms > 0 else 0
        
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO llm_executions (
                    execution_id, config_id, hardware_hash,
                    tokens_generated, latency_ms, memory_mb, tokens_per_sec,
                    success, error_message, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id, config_id, profile.profile_hash,
                tokens_generated, latency_ms, memory_mb, tokens_per_sec,
                success, error_message,
                datetime.now(timezone.utc).isoformat()
            ))
            
            conn.commit()
        
        # Trigger optimization evaluation (async)
        self._evaluate_optimization(config_id, profile.profile_hash)
    
    def _evaluate_optimization(self, config_id: str, hardware_hash: str):
        """Evaluate if config is optimized for hardware (Python evaluation)."""
        from memory.hardware_fingerprint import evaluate_config_optimization, get_hardware_profile
        
        # Get recent performance
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT AVG(tokens_per_sec), AVG(memory_mb), 
                       AVG(latency_ms), COUNT(*),
                       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END)
                FROM llm_executions
                WHERE config_id = ? AND hardware_hash = ?
                  AND timestamp > datetime('now', '-7 days')
            """, (config_id, hardware_hash))
            
            row = cursor.fetchone()
            if not row or row[3] < 3:  # Need at least 3 samples
                return
            
            avg_tok_s, avg_mem, avg_lat, total, successes = row
            
            measured_performance = {
                'tokens_per_sec': avg_tok_s or 0,
                'memory_mb': avg_mem or 0,
                'first_token_ms': avg_lat or 0,
                'error_rate': (total - successes) / total if total > 0 else 0
            }
        
        # Evaluate optimization
        profile = get_hardware_profile()
        config = {}  # TODO: Load actual config
        
        opt_score = evaluate_config_optimization(profile, config, measured_performance)
        
        # Determine if optimized
        is_optimized = opt_score >= 0.75
        
        # Identify bottleneck
        bottleneck = "None"
        suggestion = "Config is well-optimized"
        
        if opt_score < 0.75:
            if measured_performance['tokens_per_sec'] < 5:
                bottleneck = "Speed"
                suggestion = "Try smaller model or adjust batch size"
            elif measured_performance['memory_mb'] / (profile.ram_gb * 1024) > 0.5:
                bottleneck = "Memory"
                suggestion = "Reduce context window or use smaller model"
        
        # Store evaluation
        eval_id = f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO optimization_evaluations (
                    eval_id, config_id, hardware_hash,
                    is_optimized, optimization_score,
                    bottleneck, suggestion, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                eval_id, config_id, hardware_hash,
                is_optimized, opt_score,
                bottleneck, suggestion,
                datetime.now(timezone.utc).isoformat()
            ))
            
            conn.commit()


def track_llm_execution(
    config_id: str,
    tokens_generated: int,
    latency_ms: int,
    memory_mb: int,
    success: bool = True,
    error_message: Optional[str] = None
):
    """Convenience function - track an LLM execution."""
    tracker = PerformanceTracker()
    tracker.track_execution(
        config_id, tokens_generated, latency_ms,
        memory_mb, success, error_message
    )


if __name__ == "__main__":
    print("Performance Tracker - Test\n")
    
    # Simulate tracking an execution
    track_llm_execution(
        config_id="qwen25-14b-mlx",
        tokens_generated=50,
        latency_ms=3200,
        memory_mb=950,
        success=True
    )
    
    print("✅ Execution tracked")
    
    # Show stored metrics
    with sqlite3.connect(str(PERF_DB)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM llm_executions")
        count = cursor.fetchone()[0]
        print(f"📊 Total executions tracked: {count}")
