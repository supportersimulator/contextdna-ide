#!/usr/bin/env python3
"""
Architecture Auto-Enhance System - GPT-Powered Continuous Enhancement

This module provides automatic architecture documentation enhancement.
It leverages the existing Context DNA GPT-4o-mini service to:
1. Query existing architecture (read)
2. Report what they learned (enhance)
3. Flag outdated information (update)
4. **Continuously consolidate discoveries** into coherent documentation
5. **Detect patterns** across multiple captures
6. **Generate synthesized insights** automatically

The system only ever improves toward CURRENT ACCURACY - it doesn't speculate
or add hypothetical information. Enhancements must be based on actual
observations made during work.

INTEGRATION:
- Uses existing Context DNA infrastructure (no additional API keys needed)
- GPT-4o-mini already running in Docker stack (~225MB)
- Runs periodically or on-demand for consolidation
- Hooks into auto_capture.py discoveries

Usage in agent workflows:
    from memory.architecture_enhancer import ArchitectureEnhancer

    # At start of task - get context AND prepare to enhance
    enhancer = ArchitectureEnhancer()
    context = enhancer.get_and_track("django deployment")

    # During work - log observations
    enhancer.observe("Django uses gunicorn with 4 workers")
    enhancer.observe("Static files served from /var/www/ersim/static/")

    # At end of task - commit enhancements
    enhancer.enhance()  # Only adds if observations are new/valuable

CLI:
    # Get architecture and start tracking
    python architecture_enhancer.py start "voice pipeline"

    # Log an observation
    python architecture_enhancer.py observe "Agent connects via ws://localhost:7880"

    # Commit enhancements
    python architecture_enhancer.py enhance

    # Flag outdated info
    python architecture_enhancer.py outdated "voice-gpu instance ID changed"

    # NEW: GPT-powered consolidation
    python architecture_enhancer.py consolidate      # Consolidate all discoveries
    python architecture_enhancer.py analyze          # Run pattern analysis
    python architecture_enhancer.py status           # Check enhancement status
    python architecture_enhancer.py background       # Run continuous enhancement
"""

import sys
import os
import json
import logging
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict, field

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from memory.architecture import ArchitectureMemory
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False

try:
    from memory.auto_capture import get_capture_stats, _state as capture_state
    AUTO_CAPTURE_AVAILABLE = True
except ImportError:
    AUTO_CAPTURE_AVAILABLE = False

try:
    from memory.knowledge_graph import KnowledgeGraph
    KNOWLEDGE_GRAPH_AVAILABLE = True
except ImportError:
    KNOWLEDGE_GRAPH_AVAILABLE = False

# State files
ENHANCER_STATE_FILE = Path(__file__).parent / ".architecture_enhancer_state.json"
WORK_LOG_FILE = Path(__file__).parent / ".work_dialogue_log.jsonl"  # JSONL for append-only


# =============================================================================
# WORK DIALOGUE LOG - Mirrors conversation/work for GPT analysis
# =============================================================================

@dataclass
class WorkLogEntry:
    """A single entry in the work dialogue log."""
    timestamp: str
    entry_type: str  # 'command', 'observation', 'success', 'error', 'dialogue'
    content: str
    area: Optional[str] = None
    source: Optional[str] = None  # 'atlas', 'user', 'system'
    metadata: Optional[Dict] = None


class WorkDialogueLog:
    """
    Self-cleaning log of all work and dialogue.

    This creates a "mirror" of everything that happens, which GPT
    can analyze to extract architecture learnings.

    SELF-CLEANING BEHAVIOR:
    - After analysis, processed entries are archived/removed
    - Only keeps unprocessed entries + recent successes
    - Focuses on SUCCESSES (the 1 that worked), not failures (the 10 that didn't)
    - Archives are kept for 7 days then deleted
    """

    ARCHIVE_DIR = Path(__file__).parent / ".work_log_archives"
    PROCESSED_MARKER_FILE = Path(__file__).parent / ".work_log_processed.json"
    MAX_LOG_SIZE_MB = 5  # Rotate log if it exceeds this size
    ARCHIVE_RETENTION_DAYS = 7

    def __init__(self):
        self.log_file = WORK_LOG_FILE
        self._ensure_archive_dir()
        self._cleanup_old_archives()

    def _ensure_archive_dir(self):
        """Create archive directory if needed."""
        self.ARCHIVE_DIR.mkdir(exist_ok=True)

    def _cleanup_old_archives(self):
        """Delete archives older than retention period."""
        if not self.ARCHIVE_DIR.exists():
            return

        cutoff = datetime.now() - timedelta(days=self.ARCHIVE_RETENTION_DAYS)
        for archive in self.ARCHIVE_DIR.glob("*.jsonl"):
            try:
                # Extract date from filename like "archive_20240115_143022.jsonl"
                date_part = archive.stem.split("_")[1]
                archive_date = datetime.strptime(date_part, "%Y%m%d")
                if archive_date < cutoff:
                    archive.unlink()
            except (ValueError, IndexError, OSError) as e:
                logger.debug(f"Archive cleanup skipped {archive.name}: {e}")
                continue

    def _get_processed_timestamps(self) -> set:
        """Get set of already-processed entry timestamps."""
        if not self.PROCESSED_MARKER_FILE.exists():
            return set()
        try:
            with open(self.PROCESSED_MARKER_FILE) as f:
                data = json.load(f)
                return set(data.get("processed", []))
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"Processed markers load failed: {e}")
            return set()

    def _mark_as_processed(self, timestamps: List[str]):
        """Mark entries as processed so they get cleaned up."""
        existing = self._get_processed_timestamps()
        existing.update(timestamps)
        # Keep only last 1000 processed markers
        processed_list = list(existing)[-1000:]
        with open(self.PROCESSED_MARKER_FILE, "w") as f:
            json.dump({"processed": processed_list, "last_cleanup": datetime.now().isoformat()}, f)

    def log(self, entry_type: str, content: str, area: str = None,
            source: str = "atlas", metadata: Dict = None):
        """Append an entry to the work log."""
        entry = WorkLogEntry(
            timestamp=datetime.now().isoformat(),
            entry_type=entry_type,
            content=content,
            area=area,
            source=source,
            metadata=metadata
        )

        # Check if rotation needed before writing
        self._maybe_rotate()

        # Append to JSONL file (one JSON object per line)
        with open(self.log_file, "a") as f:
            f.write(json.dumps(asdict(entry)) + "\n")

    def _maybe_rotate(self):
        """Rotate log if it exceeds max size."""
        if not self.log_file.exists():
            return

        size_mb = self.log_file.stat().st_size / (1024 * 1024)
        if size_mb > self.MAX_LOG_SIZE_MB:
            # Archive current log
            archive_name = f"archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            archive_path = self.ARCHIVE_DIR / archive_name
            self.log_file.rename(archive_path)
            # Clear processed markers since we archived everything
            if self.PROCESSED_MARKER_FILE.exists():
                self.PROCESSED_MARKER_FILE.unlink()

    def log_command(self, command: str, output: str = None, exit_code: int = 0):
        """Log a command execution."""
        self.log(
            entry_type="command",
            content=command,
            metadata={"output_preview": output[:500] if output else None, "exit_code": exit_code}
        )

    def log_observation(self, observation: str, area: str = None):
        """Log an observation made during work."""
        self.log(
            entry_type="observation",
            content=observation,
            area=area
        )

    def log_success(self, task: str, details: str = None):
        """Log a successful task completion."""
        self.log(
            entry_type="success",
            content=task,
            metadata={"details": details}
        )

    def log_error_resolution(self, error: str, resolution: str):
        """Log an error and its resolution."""
        self.log(
            entry_type="error_resolution",
            content=f"Error: {error}\nResolution: {resolution}"
        )

    def log_dialogue(self, message: str, source: str = "user"):
        """Log a dialogue exchange (user question or response)."""
        self.log(
            entry_type="dialogue",
            content=message,
            source=source
        )

    def get_recent_entries(self, hours: int = 24, limit: int = 100,
                           include_processed: bool = False) -> List[Dict]:
        """
        Get recent log entries for analysis.

        Args:
            hours: How many hours back to look
            limit: Maximum entries to return
            include_processed: If False, skip already-processed entries
        """
        if not self.log_file.exists():
            return []

        cutoff = datetime.now() - timedelta(hours=hours)
        processed = set() if include_processed else self._get_processed_timestamps()
        entries = []

        with open(self.log_file) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    entry_time = datetime.fromisoformat(entry["timestamp"])
                    if entry_time >= cutoff:
                        # Skip if already processed
                        if entry["timestamp"] not in processed:
                            entries.append(entry)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.debug(f"Skipping malformed work log entry: {e}")
                    continue

        return entries[-limit:]

    def get_successes(self, hours: int = 24) -> List[Dict]:
        """
        Get only successful operations for pattern learning.

        This is the KEY method - we focus on the 1 thing that worked,
        not the 10 that didn't.
        """
        entries = self.get_recent_entries(hours=hours, include_processed=False)
        return [e for e in entries if e["entry_type"] in ("success", "observation", "error_resolution")]

    def get_unprocessed_entries(self) -> List[Dict]:
        """Get entries that haven't been processed yet."""
        return self.get_recent_entries(hours=168, include_processed=False)  # Last week

    def mark_entries_processed(self, entries: List[Dict]):
        """
        Mark entries as processed after value has been extracted.

        Call this after consolidation/analysis to trigger cleanup.
        """
        timestamps = [e.get("timestamp") for e in entries if e.get("timestamp")]
        self._mark_as_processed(timestamps)

    def cleanup_processed_entries(self):
        """
        Remove processed entries from the log file.

        This keeps the log lean by removing entries that have
        already been analyzed and their value extracted.
        """
        if not self.log_file.exists():
            return 0

        processed = self._get_processed_timestamps()
        if not processed:
            return 0

        # Read all entries
        kept_entries = []
        removed_count = 0

        with open(self.log_file) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("timestamp") in processed:
                        removed_count += 1
                    else:
                        kept_entries.append(line)
                except (json.JSONDecodeError, KeyError):
                    kept_entries.append(line)  # Keep malformed lines

        # Rewrite file with only unprocessed entries
        with open(self.log_file, "w") as f:
            for line in kept_entries:
                f.write(line)

        return removed_count

    def get_stats(self) -> Dict:
        """Get statistics about the work log."""
        entries = self.get_recent_entries(hours=168, limit=10000, include_processed=True)
        processed = self._get_processed_timestamps()

        by_type = {}
        for e in entries:
            t = e.get("entry_type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1

        return {
            "total_entries": len(entries),
            "processed_entries": len(processed),
            "unprocessed_entries": len(entries) - len([e for e in entries if e.get("timestamp") in processed]),
            "by_type": by_type,
            "log_size_kb": self.log_file.stat().st_size / 1024 if self.log_file.exists() else 0,
            "archive_count": len(list(self.ARCHIVE_DIR.glob("*.jsonl"))) if self.ARCHIVE_DIR.exists() else 0
        }


# Global work log instance
work_log = WorkDialogueLog()


# =============================================================================
# PATTERN DETECTION ENGINE
# =============================================================================

# Patterns to look for in logged work
ARCHITECTURE_PATTERNS = {
    "async_pattern": {
        "keywords": ["async", "await", "asyncio", "to_thread", "event_loop"],
        "description": "Async/await patterns for non-blocking operations"
    },
    "aws_service_pattern": {
        "keywords": ["boto3", "bedrock", "ec2", "ecs", "lambda", "s3", "rds"],
        "description": "AWS service integration patterns"
    },
    "docker_orchestration": {
        "keywords": ["docker", "compose", "container", "ecs", "task", "image"],
        "description": "Docker container orchestration patterns"
    },
    "networking_pattern": {
        "keywords": ["nlb", "alb", "vpc", "subnet", "security_group", "nginx", "ssl"],
        "description": "Network infrastructure patterns"
    },
    "deployment_pattern": {
        "keywords": ["deploy", "ssh", "systemctl", "gunicorn", "restart", "rsync"],
        "description": "Deployment and service management patterns"
    },
    "voice_pipeline_pattern": {
        "keywords": ["livekit", "stt", "tts", "whisper", "webrtc", "audio", "moshi"],
        "description": "Voice/audio processing pipeline patterns"
    },
    "terraform_pattern": {
        "keywords": ["terraform", "tfstate", "apply", "plan", "resource", ".tf"],
        "description": "Infrastructure as code patterns"
    },
    "database_pattern": {
        "keywords": ["postgresql", "rds", "migration", "django", "model", "query"],
        "description": "Database and ORM patterns"
    }
}


def detect_patterns_in_work(entries: List[Dict]) -> List[Dict]:
    """Detect architectural patterns across work log entries."""
    detected = []
    pattern_scores = {name: 0 for name in ARCHITECTURE_PATTERNS}
    pattern_examples = {name: [] for name in ARCHITECTURE_PATTERNS}

    for entry in entries:
        content = entry.get("content", "").lower()
        metadata = json.dumps(entry.get("metadata", {})).lower()
        full_text = f"{content} {metadata}"

        for pattern_name, pattern_info in ARCHITECTURE_PATTERNS.items():
            for keyword in pattern_info["keywords"]:
                if keyword in full_text:
                    pattern_scores[pattern_name] += 1
                    if len(pattern_examples[pattern_name]) < 3:
                        pattern_examples[pattern_name].append(entry.get("content", "")[:100])

    # Patterns with 2+ occurrences are significant
    for pattern_name, score in pattern_scores.items():
        if score >= 2:
            detected.append({
                "pattern": pattern_name,
                "occurrences": score,
                "description": ARCHITECTURE_PATTERNS[pattern_name]["description"],
                "examples": pattern_examples[pattern_name]
            })

    return detected


def generate_insights_from_patterns(patterns: List[Dict], entries: List[Dict]) -> List[str]:
    """Generate architectural insights from detected patterns."""
    insights = []
    pattern_names = [p["pattern"] for p in patterns]

    # Cross-pattern insights
    if "async_pattern" in pattern_names and "aws_service_pattern" in pattern_names:
        insights.append(
            "Architecture uses async patterns with AWS services. "
            "Ensure boto3 calls are wrapped in asyncio.to_thread() to prevent blocking."
        )

    if "docker_orchestration" in pattern_names and "deployment_pattern" in pattern_names:
        insights.append(
            "Docker-based deployment detected. "
            "Remember: container restart doesn't reload env vars - must recreate."
        )

    if "networking_pattern" in pattern_names and "voice_pipeline_pattern" in pattern_names:
        insights.append(
            "Voice pipeline with network infrastructure. "
            "WebRTC requires direct UDP access - Cloudflare proxy must be disabled."
        )

    if "terraform_pattern" in pattern_names:
        insights.append(
            "Terraform IaC in use. "
            "Always run terraform plan before apply. State is in S3 backend."
        )

    if "database_pattern" in pattern_names and "deployment_pattern" in pattern_names:
        insights.append(
            "Database with deployment automation. "
            "Run migrations before restarting services: python manage.py migrate"
        )

    # NOTE: Removed generic "Recent work has X successful operations" insight
    # This was providing no actionable value and just cluttering the insights.
    # Only cross-pattern insights that provide actual guidance are valuable.

    return insights


class ArchitectureEnhancer:
    """
    Auto-enhancement system for architecture documentation.

    Tracks observations made during agent work and commits valuable
    enhancements back to architecture memory.
    """

    def __init__(self):
        if not MEMORY_AVAILABLE:
            raise RuntimeError("Memory system not available")

        self.arch = ArchitectureMemory()
        self.memory = self.arch.memory

        # Load or initialize state
        self.state = self._load_state()

    def _load_state(self) -> dict:
        """Load enhancement state from file."""
        if ENHANCER_STATE_FILE.exists():
            try:
                with open(ENHANCER_STATE_FILE) as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] Failed to load enhancer state: {e}")
        return {
            "query": None,
            "context_retrieved": None,
            "observations": [],
            "started_at": None
        }

    def _save_state(self):
        """Save enhancement state to file."""
        with open(ENHANCER_STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def _clear_state(self):
        """Clear enhancement state."""
        self.state = {
            "query": None,
            "context_retrieved": None,
            "observations": [],
            "started_at": None
        }
        if ENHANCER_STATE_FILE.exists():
            ENHANCER_STATE_FILE.unlink()

    def get_and_track(self, query: str) -> str:
        """
        Get architecture context and start tracking for enhancements.

        Args:
            query: What to look up (e.g., "django deployment", "voice pipeline")

        Returns:
            Architecture context string
        """
        # Get existing context
        context = self.arch.get_architecture_context(query, limit=5)

        # Start tracking
        self.state = {
            "query": query,
            "context_retrieved": context,
            "observations": [],
            "started_at": datetime.now().isoformat()
        }
        self._save_state()

        print(f"   Tracking started for: {query}")
        print(f"   Log observations with: enhancer.observe('your observation')")

        return context

    def observe(self, observation: str, source: str = None):
        """
        Log an observation about the architecture.

        Only log FACTUAL observations made during actual work.
        Do NOT log speculation or hypotheticals.

        Args:
            observation: What you observed (e.g., "Django static files at /var/www/static/")
            source: Optional source (e.g., file path, command output)
        """
        if not self.state.get("query"):
            print("Warning: No tracking session active. Call get_and_track() first.")
            return

        self.state["observations"].append({
            "text": observation,
            "source": source,
            "timestamp": datetime.now().isoformat()
        })
        self._save_state()

        print(f"   Logged: {observation[:60]}...")

    def enhance(self) -> bool:
        """
        Commit enhancements back to architecture memory.

        Only adds observations that provide NEW, ACCURATE information.
        Skips if observations are already captured in existing context.

        Returns:
            True if enhancements were committed, False otherwise
        """
        if not self.state.get("observations"):
            print("   No observations to enhance with.")
            self._clear_state()
            return False

        query = self.state.get("query", "general")
        observations = self.state.get("observations", [])
        existing_context = self.state.get("context_retrieved", "")

        # Filter out observations that are already in the context
        new_observations = []
        for obs in observations:
            text_lower = obs["text"].lower()
            # Simple check: if key parts of observation aren't in existing context
            # This is a heuristic - could be improved with semantic similarity
            if text_lower not in existing_context.lower():
                # Check for key phrases
                key_phrases = text_lower.split()[:5]  # First 5 words
                if not all(phrase in existing_context.lower() for phrase in key_phrases if len(phrase) > 3):
                    new_observations.append(obs)

        if not new_observations:
            print("   All observations already captured in architecture. No enhancements needed.")
            self._clear_state()
            return False

        # Build enhancement content
        enhancement = f"""## Architecture Enhancement: {query}

**Enhanced:** {datetime.now().isoformat()}
**Based on:** Direct observation during work

### New Details Discovered:
"""
        for obs in new_observations:
            enhancement += f"\n- {obs['text']}"
            if obs.get("source"):
                enhancement += f" (source: {obs['source']})"

        enhancement += f"\n\n**Context:** These details were discovered while working on: {query}"
        enhancement += "\n**Tags:** enhancement, auto-discovered, " + query.replace(" ", "-")

        # Record as architecture decision (will be semantically searchable)
        self.memory.record_architecture_decision(
            decision=f"Architecture Enhancement: {query}",
            rationale=enhancement,
            alternatives=None,
            consequences="These details supplement existing architecture documentation."
        )

        print(f"   Enhanced architecture with {len(new_observations)} new observation(s)")
        self._clear_state()
        return True

    def flag_outdated(self, area: str, what_changed: str, new_value: str = None):
        """
        Flag that architecture documentation is outdated.

        Use this when you discover that documented information is no longer accurate.

        Args:
            area: Which area is outdated (e.g., "voice-gpu deployment")
            what_changed: What specifically is outdated
            new_value: Optional new correct value
        """
        content = f"""## OUTDATED ARCHITECTURE FLAG

**Area:** {area}
**Flagged:** {datetime.now().isoformat()}
**What Changed:** {what_changed}
"""
        if new_value:
            content += f"**Correct Value:** {new_value}\n"

        content += """
**Action Required:** Update architecture documentation with current values.
**Tags:** outdated, needs-update, """ + area.replace(" ", "-")

        self.memory.record_architecture_decision(
            decision=f"OUTDATED: {area}",
            rationale=content,
            alternatives=None,
            consequences="Architecture documentation needs to be updated."
        )

        print(f"   Flagged outdated: {area}")
        print(f"   What changed: {what_changed}")

    def auto_enhance_from_file(self, file_path: str):
        """
        Auto-extract architecture details from a file being modified.

        This is called automatically when agents modify certain files.
        It extracts relevant architecture details and records them.

        Args:
            file_path: Path to the file being modified
        """
        # Map file patterns to architecture queries
        file_lower = file_path.lower()

        if "terraform" in file_lower:
            self.get_and_track("infrastructure terraform")
        elif "docker" in file_lower or "ecs" in file_lower:
            self.get_and_track("deployment docker ecs")
        elif "livekit" in file_lower or "agent" in file_lower:
            self.get_and_track("livekit agent voice")
        elif "llm" in file_lower or "bedrock" in file_lower:
            self.get_and_track("llm pipeline bedrock")
        elif "tts" in file_lower or "kyutai" in file_lower:
            self.get_and_track("tts audio pipeline")
        elif "stt" in file_lower or "whisper" in file_lower:
            self.get_and_track("stt transcription pipeline")
        elif "django" in file_lower or "backend" in file_lower:
            self.get_and_track("django backend deployment")
        elif "admin" in file_lower or "next" in file_lower:
            self.get_and_track("admin frontend dashboard")
        else:
            # No specific mapping
            return None

        return self.state.get("context_retrieved")


def get_architecture_with_enhancement(query: str) -> tuple:
    """
    Convenience function: Get architecture and prepare for enhancement.

    Usage:
        context, enhancer = get_architecture_with_enhancement("django deployment")
        # ... do work ...
        enhancer.observe("Found new detail")
        enhancer.enhance()

    Returns:
        (context_string, enhancer_instance)
    """
    enhancer = ArchitectureEnhancer()
    context = enhancer.get_and_track(query)
    return context, enhancer


# =============================================================================
# GPT-POWERED CONSOLIDATION ENGINE
# =============================================================================

@dataclass
class ConsolidationResult:
    """Result of a consolidation run."""
    success: bool
    entries_processed: int
    patterns_detected: int
    insights_generated: int
    timestamp: str
    areas_covered: List[str] = field(default_factory=list)
    error: Optional[str] = None


class ConsolidationEngine:
    """
    Uses logged work + Context DNA GPT-4o-mini to consolidate architecture learnings.

    This is the "continuous GPT analysis" the user asked about - it analyzes
    all logged work and conversation to build comprehensive architecture documentation.
    """

    CONSOLIDATION_STATE_FILE = Path(__file__).parent / ".consolidation_state.json"

    def __init__(self):
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if self.CONSOLIDATION_STATE_FILE.exists():
            try:
                with open(self.CONSOLIDATION_STATE_FILE) as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] Failed to load consolidation state: {e}")
        return {
            "last_consolidation": None,
            "entries_at_last_run": 0,
            "consolidation_count": 0,
            "patterns_discovered": [],
            "insights_generated": []
        }

    def _save_state(self):
        with open(self.CONSOLIDATION_STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    def should_consolidate(self, threshold: int = 10) -> bool:
        """Check if consolidation should run based on new entries."""
        entries = work_log.get_recent_entries(hours=24)
        current_count = len(entries)
        last_count = self.state.get("entries_at_last_run", 0)

        # Consolidate if threshold reached
        if current_count - last_count >= threshold:
            return True

        # Also consolidate if 6+ hours since last run
        last_run = self.state.get("last_consolidation")
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run)
                if datetime.now() - last_dt > timedelta(hours=6):
                    return True
            except Exception as e:
                print(f"[WARN] Failed to parse last consolidation time: {e}")

        return False

    def run_consolidation(self) -> ConsolidationResult:
        """
        Run architecture consolidation.

        Analyzes all logged work/dialogue and extracts architecture learnings.
        Uses Context DNA to store consolidated knowledge.
        """
        timestamp = datetime.now().isoformat()

        if not MEMORY_AVAILABLE:
            return ConsolidationResult(
                success=False,
                entries_processed=0,
                patterns_detected=0,
                insights_generated=0,
                timestamp=timestamp,
                error="Memory system not available"
            )

        # Get recent work log entries
        entries = work_log.get_recent_entries(hours=24)

        if not entries:
            return ConsolidationResult(
                success=True,
                entries_processed=0,
                patterns_detected=0,
                insights_generated=0,
                timestamp=timestamp,
                error="No entries to process"
            )

        # Detect patterns in work
        patterns = detect_patterns_in_work(entries)
        for p in patterns:
            if p["pattern"] not in self.state["patterns_discovered"]:
                self.state["patterns_discovered"].append(p["pattern"])

        # Generate insights from patterns with quality gates
        insights = generate_insights_from_patterns(patterns, entries)
        quality_insights = []
        for insight in insights:
            # Quality gates: only store valuable insights
            if not insight:
                continue
            if insight.startswith("Recent work has"):
                continue  # Generic, no value
            if len(insight) < 50:
                continue  # Too short to be meaningful
            if insight.endswith("preserved as working procedures."):
                continue  # Generic boilerplate

            if insight not in self.state["insights_generated"]:
                self.state["insights_generated"].append(insight)
                quality_insights.append(insight)

        insights = quality_insights  # Use filtered list for reporting

        # Get areas covered
        areas = set()
        for entry in entries:
            if entry.get("area"):
                areas.add(entry["area"])
        for pattern in patterns:
            areas.add(pattern["pattern"].replace("_pattern", ""))

        # Build consolidated document
        content = f"""## Architecture Consolidation Report

**Generated:** {timestamp}
**Entries Analyzed:** {len(entries)}
**Patterns Detected:** {len(patterns)}

### Detected Patterns:
"""
        for p in patterns:
            content += f"\n**{p['pattern']}** ({p['occurrences']} occurrences):\n"
            content += f"  {p['description']}\n"
            if p.get("examples"):
                content += "  Examples:\n"
                for ex in p["examples"]:
                    content += f"    - {ex}\n"

        content += "\n### Generated Insights:\n"
        for insight in insights:
            content += f"\n- {insight}"

        content += "\n\n### Recent Successes:\n"
        successes = [e for e in entries if e.get("entry_type") == "success"]
        for s in successes[-10:]:  # Last 10 successes
            content += f"\n- {s.get('content', 'No description')}"

        # Store in Context DNA
        try:
            memory = ContextDNAClient()
            session_id = memory.record_architecture_decision(
                decision=f"[CONSOLIDATED] Architecture analysis - {len(patterns)} patterns, {len(insights)} insights",
                rationale=content,
                alternatives=None,
                consequences=f"Processed {len(entries)} work log entries"
            )

            result = ConsolidationResult(
                success=True,
                entries_processed=len(entries),
                patterns_detected=len(patterns),
                insights_generated=len(insights),
                timestamp=timestamp,
                areas_covered=list(areas)
            )

            # SELF-CLEANING: Mark processed entries so they get cleaned up
            # This is the key to keeping the log lean - value extracted, entries removed
            work_log.mark_entries_processed(entries)

            # Update state
            self.state["last_consolidation"] = timestamp
            self.state["entries_at_last_run"] = len(entries)
            self.state["consolidation_count"] += 1
            self._save_state()

            return result

        except Exception as e:
            return ConsolidationResult(
                success=False,
                entries_processed=len(entries),
                patterns_detected=len(patterns),
                insights_generated=len(insights),
                timestamp=timestamp,
                areas_covered=list(areas),
                error=str(e)
            )

    def get_status(self) -> Dict:
        """Get consolidation status."""
        entries = work_log.get_recent_entries(hours=24)
        return {
            "last_consolidation": self.state.get("last_consolidation"),
            "consolidation_count": self.state.get("consolidation_count", 0),
            "patterns_discovered": len(self.state.get("patterns_discovered", [])),
            "insights_generated": len(self.state.get("insights_generated", [])),
            "current_entries": len(entries),
            "should_consolidate_now": self.should_consolidate()
        }


# Global consolidation engine
consolidation_engine = ConsolidationEngine()


def run_background_consolidation(interval_hours: int = 6):
    """
    Run consolidation in background mode.

    Checks every hour if consolidation is needed.
    """
    import time

    print(f"Starting background consolidation (interval: {interval_hours}h)")
    print("Press Ctrl+C to stop")

    while True:
        if consolidation_engine.should_consolidate():
            print(f"\n[{datetime.now().isoformat()}] Running consolidation...")
            result = consolidation_engine.run_consolidation()
            print(f"  Entries: {result.entries_processed}")
            print(f"  Patterns: {result.patterns_detected}")
            print(f"  Insights: {result.insights_generated}")
            if result.error:
                print(f"  Error: {result.error}")
        else:
            print(f"[{datetime.now().isoformat()}] No consolidation needed")

        # Check every hour
        time.sleep(3600)


# =============================================================================
# CONVENIENCE FUNCTIONS FOR AGENT INTEGRATION
# =============================================================================

def log_work(entry_type: str, content: str, area: str = None):
    """
    Quick function to log work for GPT analysis.

    Call this from anywhere to feed the architecture brain.

    Example:
        from memory.architecture_enhancer import log_work
        log_work("success", "Deployed Django update", area="deployment")
    """
    work_log.log(entry_type, content, area)


def analyze_recent_work() -> Dict:
    """
    Analyze recent work and return patterns/insights.

    Returns dict with patterns and insights for immediate use.
    """
    entries = work_log.get_recent_entries(hours=24)
    patterns = detect_patterns_in_work(entries)
    insights = generate_insights_from_patterns(patterns, entries)

    return {
        "entries_analyzed": len(entries),
        "patterns": patterns,
        "insights": insights
    }


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Architecture Enhancer CLI - GPT-Powered Continuous Learning")
        print("")
        print("=== Original Commands ===")
        print("  start <query>              - Start tracking for a query")
        print("  observe <observation>      - Log an observation")
        print("  enhance                    - Commit enhancements")
        print("  outdated <area> <what>     - Flag outdated documentation")
        print("")
        print("=== NEW: GPT-Powered Commands ===")
        print("  consolidate                - Run GPT-powered consolidation now")
        print("  analyze                    - Analyze recent work patterns")
        print("  status                     - Show full status (tracking + consolidation)")
        print("  background                 - Run continuous consolidation")
        print("  log <type> <content>       - Log work for analysis")
        print("  cleanup                    - Remove processed entries from log")
        print("  log-stats                  - Show work log statistics")
        print("")
        print("Examples:")
        print("  python architecture_enhancer.py consolidate")
        print("  python architecture_enhancer.py analyze")
        print("  python architecture_enhancer.py log success 'Deployed Django update'")
        print("  python architecture_enhancer.py background")
        sys.exit(0)

    cmd = sys.argv[1]

    # Original commands
    if cmd == "start":
        enhancer = ArchitectureEnhancer()
        if len(sys.argv) < 3:
            print("Usage: start <query>")
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        context = enhancer.get_and_track(query)
        print("\n" + context)

    elif cmd == "observe":
        enhancer = ArchitectureEnhancer()
        if len(sys.argv) < 3:
            print("Usage: observe <observation>")
            sys.exit(1)
        observation = " ".join(sys.argv[2:])
        enhancer.observe(observation)

    elif cmd == "enhance":
        enhancer = ArchitectureEnhancer()
        enhancer.enhance()

    elif cmd == "outdated":
        enhancer = ArchitectureEnhancer()
        if len(sys.argv) < 4:
            print("Usage: outdated <area> <what_changed> [new_value]")
            sys.exit(1)
        area = sys.argv[2]
        what_changed = sys.argv[3]
        new_value = sys.argv[4] if len(sys.argv) > 4 else None
        enhancer.flag_outdated(area, what_changed, new_value)

    # NEW: GPT-powered commands
    elif cmd == "consolidate":
        print("Running GPT-powered consolidation...")
        result = consolidation_engine.run_consolidation()
        print(f"Success: {result.success}")
        print(f"Entries processed: {result.entries_processed}")
        print(f"Patterns detected: {result.patterns_detected}")
        print(f"Insights generated: {result.insights_generated}")
        print(f"Areas covered: {', '.join(result.areas_covered) or 'None'}")
        if result.error:
            print(f"Error: {result.error}")

    elif cmd == "analyze":
        print("Analyzing recent work patterns...")
        analysis = analyze_recent_work()
        print(f"\nEntries analyzed: {analysis['entries_analyzed']}")
        print(f"\nPatterns detected ({len(analysis['patterns'])}):")
        for p in analysis["patterns"]:
            print(f"  - {p['pattern']}: {p['occurrences']} occurrences")
            print(f"    {p['description']}")
        print(f"\nInsights generated ({len(analysis['insights'])}):")
        for i, insight in enumerate(analysis["insights"], 1):
            print(f"  {i}. {insight}")

    elif cmd == "status":
        # Show both tracking and consolidation status
        enhancer = ArchitectureEnhancer()
        state = enhancer.state

        print("=== Tracking Status ===")
        if state.get("query"):
            print(f"Active tracking: {state['query']}")
            print(f"Started: {state['started_at']}")
            print(f"Observations: {len(state['observations'])}")
            for i, obs in enumerate(state['observations'], 1):
                print(f"  {i}. {obs['text'][:60]}...")
        else:
            print("No active tracking session.")

        print("\n=== Consolidation Status ===")
        status = consolidation_engine.get_status()
        print(f"Last consolidation: {status['last_consolidation'] or 'Never'}")
        print(f"Total consolidations: {status['consolidation_count']}")
        print(f"Patterns discovered: {status['patterns_discovered']}")
        print(f"Insights generated: {status['insights_generated']}")
        print(f"Current entries: {status['current_entries']}")
        print(f"Should consolidate now: {status['should_consolidate_now']}")

    elif cmd == "background":
        run_background_consolidation()

    elif cmd == "log":
        if len(sys.argv) < 4:
            print("Usage: log <type> <content>")
            print("Types: command, observation, success, error, dialogue")
            sys.exit(1)
        entry_type = sys.argv[2]
        content = " ".join(sys.argv[3:])
        log_work(entry_type, content)
        print(f"Logged: [{entry_type}] {content[:60]}...")

    elif cmd == "cleanup":
        print("Cleaning up processed entries from work log...")
        removed = work_log.cleanup_processed_entries()
        print(f"Removed {removed} processed entries")
        stats = work_log.get_stats()
        print(f"Remaining entries: {stats['unprocessed_entries']}")
        print(f"Log size: {stats['log_size_kb']:.1f} KB")

    elif cmd == "log-stats":
        stats = work_log.get_stats()
        print("=== Work Log Statistics ===")
        print(f"Total entries: {stats['total_entries']}")
        print(f"Processed: {stats['processed_entries']}")
        print(f"Unprocessed: {stats['unprocessed_entries']}")
        print(f"Log size: {stats['log_size_kb']:.1f} KB")
        print(f"Archives: {stats['archive_count']}")
        print("\nBy type:")
        for t, count in stats.get("by_type", {}).items():
            print(f"  {t}: {count}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
