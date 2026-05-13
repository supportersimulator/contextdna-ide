#!/usr/bin/env python3
"""
Session Historian — Live Session Learner + LLM Analysis

Butler job that:
1. Incrementally extracts from ALL sessions (active + stale) every 15 min
2. Tracks last-read line per session — never re-extracts same messages
3. Preserves code artifacts (Write/Edit operations) with full content
4. LLM-analyzes via Qwen14B at P4 BACKGROUND priority
5. Feeds insights to evidence pipeline in real-time
6. Cleans raw files ONLY for stale, fully-archived sessions

Storage: ~/.context-dna/session_archive.db (on-disk, zero RAM footprint)
Anatomy: Hippocampus (long-term memory formation from session experiences)

Runs every 15 minutes via lite_scheduler.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from memory.db_utils import connect_wal
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Storage on disk — NOT in the project dir, NOT in RAM-mapped locations
ARCHIVE_DB_PATH = str(Path.home() / ".context-dna" / "session_archive.db")
SESSIONS_DIR = Path.home() / ".claude" / "projects"
# The main project sessions — adaptive: pick the dir with the most recent JSONL
_SUPERREPO_CANDIDATES = [
    "-Users-aarontjomsland-dev-er-simulator-superrepo",
    "-Users-aarontjomsland-Documents-er-simulator-superrepo",
]

def _resolve_superrepo_key() -> str:
    """Pick the candidate project dir with the most recent JSONL file."""
    best_key = _SUPERREPO_CANDIDATES[0]  # default to dev path
    best_mtime = 0.0
    for key in _SUPERREPO_CANDIDATES:
        proj = SESSIONS_DIR / key
        if not proj.is_dir():
            continue
        for f in proj.iterdir():
            if f.suffix == ".jsonl":
                mt = f.stat().st_mtime
                if mt > best_mtime:
                    best_mtime = mt
                    best_key = key
                    break  # one recent file is enough signal
    return best_key

SUPERREPO_KEY = _resolve_superrepo_key()

# Thresholds
EXTRACT_AFTER_MINS = 15   # Extract from sessions >15 min old (including active)
ACTIVE_EXTRACT_MINS = 2   # Extract from ACTIVE sessions every 2 min (near real-time)
STALE_HOURS = 1            # Safety sweep fallback (event-triggered cleanup is primary)
TAB_OPEN_MINS = 5         # Session modified within 5 min = tab still open (don't delete)
MIN_SIZE_KB = 5           # Skip trivial sessions
MAX_LLM_INPUT_CHARS = 80000  # ~20K tokens. Qwen3-4B: 32K ctx, reserve 2K output + 4K system/thinking margin.
MAX_LLM_CHUNK = MAX_LLM_INPUT_CHARS // 2  # Head/tail halves for truncation
LLM_BATCH_SIZE = 3        # Max sessions to LLM-analyze per run (don't hog queue)
MAX_EXTRACT_PER_RUN = 10  # Max sessions to extract per run (avoid long runs)
VAULT_EXPORT_INTERVAL = 300  # Export to vault every 5 min for active sessions (not every 2-min tick)

# Code artifact storage (separate from gold text — preserves full code)
CODE_ARTIFACTS_DIR = Path.home() / ".context-dna" / "session_code_artifacts"

# ─── Project Detection Map ────────────────────────────────────
# Maps sub-projects to their directory prefixes and keyword signals.
# Detection priority: code artifact file paths > gold text keywords.
SUPERREPO_ROOT = "/Users/aarontjomsland/dev/er-simulator-superrepo/"

PROJECT_MAP = {
    'context-dna': {
        'paths': ['memory/', 'context-dna/', 'context-dna-data/', 'acontext/'],
        'keywords': ['context_dna', 'contextdna', 'webhook', 'injection', 'historian',
                     'brain.py', 'professor', 'evidence pipeline', 'observability',
                     'learnings', 'butler', 'synaptic', 'section 0', 'section 8'],
    },
    'er-simulator': {
        'paths': ['engines/', 'components/', 'hooks/', 'assets/', 'app/',
                  'simulator-core/', 'mobile/', 'src/'],
        'keywords': ['monitor', 'waveform', 'ecg', 'vitals', 'adaptive salience',
                     'spo2', 'heart rate', 'patient', 'scenario'],
    },
    'landing-page': {
        'paths': ['landing-page/'],
        'keywords': ['landing page', 'website', 'deploy-landing'],
    },
    'voice-stack': {
        'paths': ['ersim-voice-stack/'],
        'keywords': ['voice', 'livekit', 'whisper', 'tts', 'stt', 'webrtc'],
    },
    'infrastructure': {
        'paths': ['infra/', 'system/'],
        'keywords': ['terraform', 'docker compose', 'aws', 'ec2', 'ecs', 'lambda',
                     'cloudfront', 'route53', 'nlb'],
    },
    'web-app': {
        'paths': ['web-app/', 'sim-frontend/', 'backend/'],
        'keywords': ['frontend', 'django', 'next.js', 'react app'],
    },
    'admin': {
        'paths': ['admin.contextdna.io/', 'admin.ersimulator.com/'],
        'keywords': ['admin dashboard', 'admin panel'],
    },
}


class SessionHistorian:
    """Extracts, analyzes, and archives Claude Code sessions."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or ARCHIVE_DB_PATH
        self._last_vault_export = {}  # session_id → monotonic timestamp
        self._ensure_db()

    def _ensure_db(self):
        """Create archive database if needed."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(str(CODE_ARTIFACTS_DIR), exist_ok=True)
        conn = connect_wal(self.db_path)
        try:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS archived_sessions (
                    session_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    extracted_at TEXT NOT NULL,
                    session_date TEXT,
                    raw_size_mb REAL,
                    gold_size_kb REAL,
                    user_messages INTEGER DEFAULT 0,
                    assistant_messages INTEGER DEFAULT 0,
                    subagent_count INTEGER DEFAULT 0,
                    gold_text TEXT,
                    llm_summary TEXT,
                    llm_insights TEXT,
                    llm_analyzed_at TEXT,
                    usefulness_score REAL,
                    key_topics TEXT,
                    raw_deleted INTEGER DEFAULT 0,
                    code_artifact_count INTEGER DEFAULT 0,
                    embedding_vector BLOB
                );

                CREATE TABLE IF NOT EXISTS session_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    insight_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    fed_to_pipeline INTEGER DEFAULT 0,
                    FOREIGN KEY (session_id) REFERENCES archived_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS code_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    file_path TEXT,
                    language TEXT,
                    code TEXT NOT NULL,
                    context_before TEXT,
                    context_after TEXT,
                    artifact_type TEXT DEFAULT 'written',
                    size_bytes INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES archived_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS session_embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    chunk_index INTEGER DEFAULT 0,
                    chunk_text TEXT NOT NULL,
                    embedding BLOB,
                    model TEXT DEFAULT 'local',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES archived_sessions(session_id)
                );

                -- Tracks incremental extraction progress per session
                CREATE TABLE IF NOT EXISTS extraction_progress (
                    session_id TEXT PRIMARY KEY,
                    last_line_read INTEGER DEFAULT 0,
                    last_file_size INTEGER DEFAULT 0,
                    last_extracted_at TEXT,
                    is_active INTEGER DEFAULT 1,
                    extract_count INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_date
                    ON archived_sessions(session_date);
                CREATE INDEX IF NOT EXISTS idx_sessions_analyzed
                    ON archived_sessions(llm_analyzed_at);
                CREATE INDEX IF NOT EXISTS idx_insights_type
                    ON session_insights(insight_type);
                CREATE INDEX IF NOT EXISTS idx_code_session
                    ON code_artifacts(session_id);
                CREATE INDEX IF NOT EXISTS idx_code_path
                    ON code_artifacts(file_path);
                CREATE INDEX IF NOT EXISTS idx_embed_session
                    ON session_embeddings(session_id);

                PRAGMA journal_mode=WAL;
            ''')
            # Auto-migrate: add project_tag column if missing
            cols = [r[1] for r in conn.execute("PRAGMA table_info(archived_sessions)").fetchall()]
            if 'project_tag' not in cols:
                conn.execute("ALTER TABLE archived_sessions ADD COLUMN project_tag TEXT DEFAULT ''")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON archived_sessions(project_tag)")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _detect_project(session_id: str, gold_text: str = None,
                        code_artifacts: list = None) -> str:
        """Detect which sub-project a session primarily worked on.

        Priority: code artifact file paths (strongest signal) > gold text keywords.
        Returns the project key (e.g. 'context-dna') or 'mixed' if no dominant project.
        """
        scores = {proj: 0 for proj in PROJECT_MAP}

        # Signal 1: Code artifact file paths (weight 3 per match)
        if code_artifacts:
            for artifact in code_artifacts:
                fp = artifact if isinstance(artifact, str) else artifact.get('file_path', '')
                if not fp:
                    continue
                # Strip superrepo root to get relative path
                rel = fp.replace(SUPERREPO_ROOT, '')
                for proj, cfg in PROJECT_MAP.items():
                    for prefix in cfg['paths']:
                        if rel.startswith(prefix):
                            scores[proj] += 3
                            break

        # Signal 2: Gold text keywords (weight 1 per match)
        if gold_text:
            gold_lower = gold_text.lower()
            for proj, cfg in PROJECT_MAP.items():
                for kw in cfg['keywords']:
                    if kw in gold_lower:
                        scores[proj] += 1

        # Pick dominant project
        if not any(scores.values()):
            return ''
        top_proj = max(scores, key=scores.get)
        top_score = scores[top_proj]
        if top_score == 0:
            return ''

        # Check if it's truly dominant (>50% of total signal)
        total = sum(scores.values())
        if top_score / total >= 0.4:
            return top_proj
        return 'mixed'

    def _update_project_tag(self, session_id: str):
        """Recompute and store project_tag for a session based on its artifacts."""
        conn = connect_wal(self.db_path)
        try:
            # Get code artifact paths
            artifacts = [r[0] for r in conn.execute(
                'SELECT file_path FROM code_artifacts WHERE session_id = ? AND file_path IS NOT NULL',
                (session_id,)
            ).fetchall()]

            # Get gold text
            row = conn.execute(
                'SELECT gold_text FROM archived_sessions WHERE session_id = ?',
                (session_id,)
            ).fetchone()
            gold = row[0] if row else ''

            tag = self._detect_project(session_id, gold_text=gold, code_artifacts=artifacts)
            conn.execute(
                'UPDATE archived_sessions SET project_tag = ? WHERE session_id = ?',
                (tag, session_id)
            )
            conn.commit()
        finally:
            conn.close()

    def should_run(self) -> bool:
        """Check if any sessions have new content to extract."""
        project_dir = SESSIONS_DIR / SUPERREPO_KEY
        if not project_dir.exists():
            return False

        cutoff = time.time() - (EXTRACT_AFTER_MINS * 60)

        for jsonl in project_dir.glob("*.jsonl"):
            if jsonl.stem.startswith("agent-"):
                continue
            stat = jsonl.stat()
            if stat.st_size < MIN_SIZE_KB * 1024:
                continue
            # Session must be at least EXTRACT_AFTER_MINS old
            if stat.st_mtime > cutoff:
                # File was modified very recently — but check if it was
                # CREATED long enough ago (session could be active but old)
                if stat.st_ctime > cutoff:
                    continue
            # Check if there's new content since last extraction
            if self._has_new_content(jsonl.stem, stat.st_size):
                return True

        # Also check for un-analyzed archived sessions
        if self._count_unanalyzed() > 0:
            return True

        return False

    def run(self) -> dict:
        """Main entry point — full pipeline (extract + analyze + cleanup)."""
        start = time.monotonic()
        results = {
            'extracted': 0,
            'analyzed': 0,
            'insights': 0,
            'reclaimed_mb': 0,
            'errors': [],
        }

        # Pre-check: validate archive DB is accessible and intact
        try:
            conn = connect_wal(self.db_path)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            if integrity != "ok":
                logger.error(f"Archive DB integrity check FAILED: {integrity}")
                results['errors'].append(f"DB integrity: {integrity}")
                return results
        except Exception as e:
            logger.error(f"Archive DB inaccessible: {e}")
            results['errors'].append(f"DB access: {str(e)[:80]}")
            return results

        # Phase 1: Extract from ALL sessions (active + stale)
        extracted = self._phase1_extract()
        results['extracted'] = len(extracted)

        # Phase 2: LLM analysis on unanalyzed sessions
        analyzed, insights = self._phase2_llm_analyze()
        results['analyzed'] = analyzed
        results['insights'] = insights

        # Phase 2.5: Segment gold_text for rich 16-pass mining
        segments = self._phase2_segment_gold()
        results['segments'] = segments

        # Phase 3: Feed insights to evidence pipeline
        fed = self._phase3_feed_pipeline()

        # Phase 4: Cleanup raw files for already-archived sessions
        reclaimed = self._phase4_cleanup_archived()
        results['reclaimed_mb'] = reclaimed

        results['duration_ms'] = int((time.monotonic() - start) * 1000)
        return results

    def run_active_only(self) -> dict:
        """Fast cycle — extract from active sessions + clean newly-dead ones.

        Runs every 2 minutes. Extracts from active tabs (near real-time gold capture).
        Also cleans sessions that died since last cycle (event-triggered on death detection).
        """
        start = time.monotonic()
        results = {'extracted': 0, 'cleaned': 0, 'errors': [], 'duration_ms': 0}

        project_dir = SESSIONS_DIR / SUPERREPO_KEY
        if not project_dir.exists():
            return results

        active_ids = self._get_active_session_ids()

        # EVENT: Clean newly-dead sessions (were alive, now inactive, gold already saved)
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute('''
                SELECT session_id FROM archived_sessions
                WHERE raw_deleted = 0 AND gold_text IS NOT NULL
            ''')
            archived_not_cleaned = [r[0] for r in cursor.fetchall()]
        finally:
            conn.close()

        for session_id in archived_not_cleaned:
            if session_id not in active_ids:
                mb = self._cleanup_session(session_id, project_dir)
                if mb > 0:
                    results['cleaned'] += 1

        if not active_ids:
            results['duration_ms'] = int((time.monotonic() - start) * 1000)
            return results

        count = 0
        for session_id in active_ids:
            jsonl = project_dir / f"{session_id}.jsonl"
            if not jsonl.exists():
                continue
            stat = jsonl.stat()
            if stat.st_size < MIN_SIZE_KB * 1024:
                continue
            if not self._has_new_content(session_id, stat.st_size):
                continue

            last_line = self._get_last_line(session_id)
            try:
                gold = self._extract_gold_incremental(jsonl, start_line=last_line)
                if gold and (gold['user_messages'] > 0 or gold['assistant_messages'] > 0):
                    self._store_gold_incremental(session_id, gold, is_active=True)
                    self._update_progress(session_id, gold['lines_read'],
                                         stat.st_size, is_active=True)
                    count += 1
                    logger.info(f"[realtime] {session_id[:8]} "
                               f"+{gold['user_messages']}u/{gold['assistant_messages']}a "
                               f"(lines {last_line}→{gold['lines_read']})")
                else:
                    self._update_progress(session_id, last_line,
                                         stat.st_size, is_active=True)
            except Exception as e:
                results['errors'].append(str(e)[:80])

        results['extracted'] = count

        # Segment any new gold_text so gold_segments are available for mining
        # within ~2 minutes of new content arriving (not waiting 15min for full cycle)
        if count > 0:
            try:
                segments = self._phase2_segment_gold()
                results['segments'] = segments
            except Exception as e:
                results['errors'].append(f"segment: {str(e)[:60]}")

        results['duration_ms'] = int((time.monotonic() - start) * 1000)
        return results

    # ─── Phase 1: Extract ──────────────────────────────────────

    def _phase1_extract(self) -> list:
        """Incrementally extract from ALL sessions (active + stale).

        Tracks last-read line per session so we only process new messages.
        Active sessions get extracted but never deleted.
        """
        project_dir = SESSIONS_DIR / SUPERREPO_KEY
        if not project_dir.exists():
            return []

        active_ids = self._get_active_session_ids()
        extracted = []
        count = 0

        for jsonl in sorted(project_dir.glob("*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            if count >= MAX_EXTRACT_PER_RUN:
                break
            if jsonl.stem.startswith("agent-"):
                continue

            stat = jsonl.stat()
            if stat.st_size < MIN_SIZE_KB * 1024:
                continue

            # Check if there's new content
            if not self._has_new_content(jsonl.stem, stat.st_size):
                continue

            is_active = jsonl.stem in active_ids
            last_line = self._get_last_line(jsonl.stem)

            try:
                gold = self._extract_gold_incremental(jsonl, start_line=last_line)
                if gold and (gold['user_messages'] > 0 or gold['assistant_messages'] > 0):
                    self._store_gold_incremental(jsonl.stem, gold, is_active)
                    self._update_progress(jsonl.stem, gold['lines_read'],
                                         stat.st_size, is_active)
                    extracted.append(jsonl.stem)
                    count += 1

                    # Also extract from subagent files (deepest first)
                    subdir = jsonl.parent / jsonl.stem
                    if subdir.exists() and not is_active:
                        try:
                            self._extract_subagent_gold(jsonl.stem, subdir)
                        except Exception as sa_err:
                            logger.debug(f"Subagent extraction skipped: {sa_err}")

                    mode = "live" if is_active else "archive"
                    logger.info(f"[{mode}] {jsonl.stem[:8]} "
                               f"+{gold['user_messages']}u/{gold['assistant_messages']}a "
                               f"msgs, +{len(gold.get('code_artifacts', []))} code artifacts "
                               f"(lines {last_line}→{gold['lines_read']})")

                    # EVENT-TRIGGERED CLEANUP: gold saved → clean raw immediately
                    if not is_active:
                        self._cleanup_session(jsonl.stem, project_dir)
                else:
                    # No new messages, but update progress to avoid re-scanning
                    self._update_progress(jsonl.stem, last_line,
                                         stat.st_size, is_active)
            except Exception as e:
                logger.warning(f"Failed to extract {jsonl.stem[:8]}: {e}")

        return extracted

    def _extract_gold_incremental(self, jsonl_path: Path,
                                    start_line: int = 0) -> Optional[dict]:
        """Extract NEW messages from a session, starting at start_line.

        Only reads lines after start_line, so active sessions get
        incrementally extracted without re-processing old messages.
        """
        return self._extract_gold(jsonl_path, start_line=start_line)

    def _extract_gold(self, jsonl_path: Path,
                      start_line: int = 0) -> Optional[dict]:
        """Extract user/assistant messages AND code artifacts from a session JSONL."""
        messages = []
        code_artifacts = []
        user_count = 0
        assistant_count = 0
        lines_read = start_line

        with open(jsonl_path, 'r', errors='replace') as f:
            # Skip to start_line
            for _ in range(start_line):
                f.readline()

            for line in f:
                lines_read += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get('type', '')
                if msg_type not in ('user', 'assistant'):
                    continue

                msg = obj.get('message', {})
                role = msg.get('role', '')
                content = msg.get('content', '')

                text_parts = []
                if isinstance(content, str):
                    # Skip webhook injections
                    if '<system-reminder>' not in content:
                        text_parts.append(content[:2000])
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get('type') == 'text':
                            text = item.get('text', '')
                            if '<system-reminder>' in text:
                                continue
                            text_parts.append(text[:2000])
                        elif isinstance(item, dict) and item.get('type') == 'tool_use':
                            tool = item.get('name', '?')
                            inp = item.get('input', {})
                            if tool == 'Write':
                                # PRESERVE full code artifacts from Write operations
                                file_path = inp.get('file_path', '?')
                                code_content = inp.get('content', '')
                                if code_content and len(code_content) > 50:
                                    lang = self._detect_language(file_path)
                                    code_artifacts.append({
                                        'file_path': file_path,
                                        'language': lang,
                                        'code': code_content,
                                        'artifact_type': 'written',
                                        'size_bytes': len(code_content.encode('utf-8')),
                                    })
                                text_parts.append(f"[Write: {file_path} ({len(code_content)} chars)]")
                            elif tool == 'Edit':
                                file_path = inp.get('file_path', '?')
                                old_str = inp.get('old_string', '')
                                new_str = inp.get('new_string', '')
                                # Preserve non-trivial edits as code artifacts
                                if new_str and len(new_str) > 50:
                                    lang = self._detect_language(file_path)
                                    code_artifacts.append({
                                        'file_path': file_path,
                                        'language': lang,
                                        'code': new_str,
                                        'context_before': old_str[:500] if old_str else None,
                                        'artifact_type': 'edited',
                                        'size_bytes': len(new_str.encode('utf-8')),
                                    })
                                text_parts.append(f"[Edit: {file_path}]")
                            elif tool == 'Bash':
                                text_parts.append(f"[Bash: {str(inp.get('command', ''))[:150]}]")
                            else:
                                text_parts.append(f"[{tool}]")

                combined = "\n".join(text_parts).strip()
                if not combined:
                    continue

                if role == 'user':
                    user_count += 1
                    messages.append(f"USER: {combined}")
                elif role == 'assistant':
                    assistant_count += 1
                    if len(combined) > 3000:
                        combined = combined[:3000] + " [truncated]"
                    messages.append(f"ATLAS: {combined}")

        if not messages:
            # Return with lines_read so progress tracking updates even on empty
            return {
                'gold_text': '', 'code_artifacts': [], 'raw_size_mb': 0,
                'gold_size_kb': 0, 'user_messages': 0, 'assistant_messages': 0,
                'subagent_count': 0, 'session_date': '', 'lines_read': lines_read,
            }

        # Count subagents
        subagent_dir = jsonl_path.parent / jsonl_path.stem / "subagents"
        subagent_count = len(list(subagent_dir.glob("*.jsonl"))) if subagent_dir.exists() else 0

        gold_text = "\n\n".join(messages)
        raw_size = jsonl_path.stat().st_size / (1024 * 1024)
        mod_time = datetime.fromtimestamp(jsonl_path.stat().st_mtime)

        return {
            'gold_text': gold_text,
            'code_artifacts': code_artifacts,
            'raw_size_mb': raw_size,
            'gold_size_kb': len(gold_text.encode('utf-8')) / 1024,
            'user_messages': user_count,
            'assistant_messages': assistant_count,
            'subagent_count': subagent_count,
            'session_date': mod_time.isoformat(),
            'lines_read': lines_read,
        }

    @staticmethod
    def _detect_language(file_path: str) -> str:
        """Detect programming language from file extension."""
        ext_map = {
            '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
            '.tsx': 'typescript', '.jsx': 'javascript', '.json': 'json',
            '.md': 'markdown', '.sh': 'bash', '.yml': 'yaml', '.yaml': 'yaml',
            '.sql': 'sql', '.html': 'html', '.css': 'css', '.toml': 'toml',
            '.cfg': 'ini', '.env': 'env', '.dockerfile': 'dockerfile',
        }
        ext = os.path.splitext(file_path)[1].lower()
        if os.path.basename(file_path).lower() == 'dockerfile':
            return 'dockerfile'
        return ext_map.get(ext, 'text')

    def _store_gold(self, session_id: str, gold: dict):
        """Store extracted gold + code artifacts in archive DB."""
        code_artifacts = gold.get('code_artifacts', [])

        conn = connect_wal(self.db_path)
        try:
            conn.execute('''
                INSERT OR REPLACE INTO archived_sessions
                (session_id, project, extracted_at, session_date, raw_size_mb,
                 gold_size_kb, user_messages, assistant_messages, subagent_count,
                 gold_text, code_artifact_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                session_id, SUPERREPO_KEY, datetime.now().isoformat(),
                gold['session_date'], gold['raw_size_mb'], gold['gold_size_kb'],
                gold['user_messages'], gold['assistant_messages'],
                gold['subagent_count'], gold['gold_text'],
                len(code_artifacts),
            ))

            # Store code artifacts
            for artifact in code_artifacts:
                conn.execute('''
                    INSERT INTO code_artifacts
                    (session_id, file_path, language, code, context_before,
                     artifact_type, size_bytes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    session_id, artifact.get('file_path'),
                    artifact.get('language', 'text'),
                    artifact['code'],
                    artifact.get('context_before'),
                    artifact.get('artifact_type', 'written'),
                    artifact.get('size_bytes', 0),
                    datetime.now().isoformat(),
                ))

            conn.commit()

            if code_artifacts:
                logger.info(f"  Preserved {len(code_artifacts)} code artifacts "
                           f"from session {session_id[:8]}")
        finally:
            conn.close()

        # Generate embeddings for semantic search
        self._generate_embeddings(session_id, gold['gold_text'])

        # Detect and store project tag
        try:
            self._update_project_tag(session_id)
        except Exception as _e:
            logger.debug("_update_project_tag failed (non-critical): %s", _e)

        # Export summary to .projectdna/raw/sessions/ (vault integration)
        try:
            self._export_to_vault(session_id, gold)
        except Exception as e:
            logger.warning(f"Vault export failed for {session_id[:12]}: {e}")

    def _export_to_vault(self, session_id: str, gold: dict):
        """Write session summary to .projectdna/raw/sessions/ for vault integration.

        Format: YAML frontmatter per Markdown-memory-layer spec (section 7.1).
        Reads accumulated totals from archive DB (not just the delta in `gold`),
        so vault files reflect the full session regardless of which storage path called us.
        """
        vault_dir = Path(__file__).parent.parent / ".projectdna" / "raw" / "sessions"
        vault_dir.mkdir(parents=True, exist_ok=True)
        short_id = session_id[:12]

        # Pull accumulated data from archive DB for accurate totals
        conn = connect_wal(self.db_path)
        try:
            row = conn.execute(
                'SELECT session_date, user_messages, assistant_messages, '
                'code_artifact_count, gold_size_kb, gold_text, project_tag '
                'FROM archived_sessions WHERE session_id = ?',
                (session_id,)
            ).fetchone()
        finally:
            conn.close()

        if not row:
            # Fallback to gold dict (first extraction via _store_gold)
            date_str = gold.get('session_date', datetime.now().strftime('%Y-%m-%d'))[:10]
            full_gold = gold.get('gold_text', '')
            user_msgs = gold.get('user_messages', 0)
            asst_msgs = gold.get('assistant_messages', 0)
            artifact_count = len(gold.get('code_artifacts', []))
            gold_kb = gold.get('gold_size_kb', 0)
            project_tag = ''
        else:
            date_str = (row[0] or datetime.now().strftime('%Y-%m-%d'))[:10]
            user_msgs = row[1] or 0
            asst_msgs = row[2] or 0
            artifact_count = row[3] or 0
            gold_kb = row[4] or 0
            full_gold = row[5] or ''
            project_tag = row[6] or ''

        out_path = vault_dir / f"{date_str}_claude-code_{short_id}.md"

        # Clean up old-format file (pre-YAML frontmatter: {date}_{id}.md)
        old_path = vault_dir / f"{date_str}_{short_id}.md"
        if old_path.exists() and old_path != out_path:
            old_path.unlink()

        # Last 2000 chars (most recent context, more useful than first 2000)
        preview = ('...' + full_gold[-2000:]) if len(full_gold) > 2000 else full_gold
        now_iso = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')

        content = (
            f"---\n"
            f"source: claude-code\n"
            f"timestamp: \"{now_iso}\"\n"
            f"agent: claude\n"
            f"repo: er-simulator-superrepo\n"
            f"session_id: \"{session_id}\"\n"
            f"project: \"{project_tag}\"\n"
            f"---\n\n"
            f"# Session {short_id}\n"
            f"- Date: {date_str}\n"
            f"- Project: {project_tag}\n"
            f"- User messages: {user_msgs}\n"
            f"- Assistant messages: {asst_msgs}\n"
            f"- Code artifacts: {artifact_count}\n"
            f"- Gold size: {gold_kb:.1f}KB\n\n"
            f"## Extract (recent)\n{preview}\n"
        )
        out_path.write_text(content, encoding='utf-8')
        logger.debug(f"[VAULT] Exported session {short_id} to {out_path.name}")

        # Log to EventedWriteService (Movement 2 chain)
        try:
            from memory.evented_write import EventedWriteService
            ews = EventedWriteService.get_instance()
            ews._emit_event(
                "session_historian", "export_to_vault",
                {"session_id": short_id, "file": out_path.name,
                 "gold_kb": round(gold_kb, 1), "messages": user_msgs + asst_msgs,
                 "movement": 3},
            )
        except Exception as _e:
            logger.debug("event logging failed (fail-open): %s", _e)

    def _store_gold_incremental(self, session_id: str, gold: dict, is_active: bool):
        """Append new gold to existing archive (incremental extraction)."""
        code_artifacts = gold.get('code_artifacts', [])
        gold_text = gold.get('gold_text', '')

        if not gold_text:
            return

        conn = connect_wal(self.db_path)
        try:
            # Check if session already exists
            existing = conn.execute(
                'SELECT gold_text, user_messages, assistant_messages, code_artifact_count FROM archived_sessions WHERE session_id = ?',
                (session_id,)
            ).fetchone()

            now = datetime.now().isoformat()

            if existing:
                # APPEND new gold to existing
                old_gold = existing[0] or ''
                combined_gold = old_gold + "\n\n--- [incremental extract] ---\n\n" + gold_text
                new_user = (existing[1] or 0) + gold['user_messages']
                new_asst = (existing[2] or 0) + gold['assistant_messages']
                new_artifacts = (existing[3] or 0) + len(code_artifacts)

                conn.execute('''
                    UPDATE archived_sessions SET
                        gold_text = ?, extracted_at = ?,
                        user_messages = ?, assistant_messages = ?,
                        code_artifact_count = ?, raw_size_mb = ?,
                        gold_size_kb = ?
                    WHERE session_id = ?
                ''', (
                    combined_gold, now,
                    new_user, new_asst,
                    new_artifacts, gold['raw_size_mb'],
                    len(combined_gold.encode('utf-8')) / 1024,
                    session_id,
                ))

                # Reset LLM analysis flag so new content gets re-analyzed.
                # Growth-based: if gold_text grew by >30% since last analysis,
                # re-analyze to capture long-session content (fixes 85% unanalyzed gap).
                old_len = len(old_gold)
                new_len = len(combined_gold)
                growth_pct = ((new_len - old_len) / old_len * 100) if old_len > 0 else 100
                if gold['user_messages'] + gold['assistant_messages'] >= 3 or growth_pct > 30:
                    conn.execute(
                        'UPDATE archived_sessions SET llm_analyzed_at = NULL WHERE session_id = ?',
                        (session_id,)
                    )
            else:
                # First extraction — INSERT
                conn.execute('''
                    INSERT INTO archived_sessions
                    (session_id, project, extracted_at, session_date, raw_size_mb,
                     gold_size_kb, user_messages, assistant_messages, subagent_count,
                     gold_text, code_artifact_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    session_id, SUPERREPO_KEY, now,
                    gold['session_date'], gold['raw_size_mb'], gold['gold_size_kb'],
                    gold['user_messages'], gold['assistant_messages'],
                    gold['subagent_count'], gold_text,
                    len(code_artifacts),
                ))

            # Always store new code artifacts
            for artifact in code_artifacts:
                conn.execute('''
                    INSERT INTO code_artifacts
                    (session_id, file_path, language, code, context_before,
                     artifact_type, size_bytes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    session_id, artifact.get('file_path'),
                    artifact.get('language', 'text'),
                    artifact['code'],
                    artifact.get('context_before'),
                    artifact.get('artifact_type', 'written'),
                    artifact.get('size_bytes', 0),
                    now,
                ))

            conn.commit()
        finally:
            conn.close()

        # Generate embeddings for the new content
        if gold_text:
            self._generate_embeddings(session_id, gold_text)

        # Recompute project tag (based on all accumulated artifacts)
        try:
            self._update_project_tag(session_id)
        except Exception as _e:
            logger.debug("_update_project_tag failed (non-critical): %s", _e)

        # Export to vault: always on session death, throttled during active (~5min)
        should_export = not is_active
        if is_active:
            last = self._last_vault_export.get(session_id, 0)
            if time.monotonic() - last >= VAULT_EXPORT_INTERVAL:
                should_export = True
        if should_export:
            try:
                self._export_to_vault(session_id, gold)
                self._last_vault_export[session_id] = time.monotonic()
            except Exception as _e:
                logger.debug("vault export failed (best-effort): %s", _e)

    # ─── Progress Tracking ─────────────────────────────────────

    def _has_new_content(self, session_id: str, current_size: int) -> bool:
        """Check if session file has grown since last extraction."""
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute(
                'SELECT last_file_size FROM extraction_progress WHERE session_id = ?',
                (session_id,)
            )
            row = cursor.fetchone()
            if not row:
                return True  # Never extracted
            return current_size > row[0]
        finally:
            conn.close()

    def _get_last_line(self, session_id: str) -> int:
        """Get last line number read for a session."""
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute(
                'SELECT last_line_read FROM extraction_progress WHERE session_id = ?',
                (session_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def _update_progress(self, session_id: str, lines_read: int,
                         file_size: int, is_active: bool):
        """Update extraction progress for a session."""
        conn = connect_wal(self.db_path)
        try:
            conn.execute('''
                INSERT OR REPLACE INTO extraction_progress
                (session_id, last_line_read, last_file_size, last_extracted_at,
                 is_active, extract_count)
                VALUES (?, ?, ?, ?,  ?,
                    COALESCE((SELECT extract_count FROM extraction_progress WHERE session_id = ?), 0) + 1
                )
            ''', (
                session_id, lines_read, file_size,
                datetime.now().isoformat(),
                1 if is_active else 0,
                session_id,
            ))
            conn.commit()
        finally:
            conn.close()

    # ─── Phase 2: LLM Analysis ─────────────────────────────────

    def _phase2_llm_analyze(self) -> tuple:
        """Use Qwen14B to analyze unanalyzed sessions."""
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute('''
                SELECT session_id, gold_text, raw_size_mb, user_messages,
                       assistant_messages, subagent_count, session_date
                FROM archived_sessions
                WHERE llm_analyzed_at IS NULL AND gold_text IS NOT NULL
                ORDER BY extracted_at DESC
                LIMIT ?
            ''', (LLM_BATCH_SIZE,))
            sessions = cursor.fetchall()
        finally:
            conn.close()

        if not sessions:
            return 0, 0

        analyzed = 0
        total_insights = 0

        for row in sessions:
            session_id, gold_text, raw_mb, user_msgs, asst_msgs, subagents, date = row

            # Smart chunking: fit within Qwen3-4B's 32K token context window
            # System prompt ~1K chars, thinking overhead ~4K tokens → safe input budget: ~80K chars
            if len(gold_text) <= MAX_LLM_INPUT_CHARS:
                # Fits in single pass — maximum context quality
                chunk = gold_text
            elif len(gold_text) <= MAX_LLM_INPUT_CHARS * 2:
                # Slightly too large — head+tail truncation preserves start/end context
                chunk = gold_text[:MAX_LLM_CHUNK] + \
                    f"\n\n[... {len(gold_text) - MAX_LLM_INPUT_CHARS} chars truncated ...]\n\n" + \
                    gold_text[-MAX_LLM_CHUNK:]
            else:
                # Very large session — multi-pass: analyze chunks, merge insights
                logger.info(f"Large session {session_id[:8]}: {len(gold_text)} chars, using multi-pass analysis")
                chunk = gold_text[:MAX_LLM_CHUNK] + \
                    f"\n\n[... {len(gold_text) - MAX_LLM_INPUT_CHARS} chars truncated ...]\n\n" + \
                    gold_text[-MAX_LLM_CHUNK:]

            summary, insights, score, topics = self._llm_analyze_session(
                chunk, session_id[:8], raw_mb, user_msgs, asst_msgs, subagents, date
            )

            if summary:
                self._store_analysis(session_id, summary, insights, score, topics)
                analyzed += 1
                total_insights += len(insights) if insights else 0
                logger.info(f"LLM analyzed {session_id[:8]}: "
                           f"score={score:.1f}, insights={len(insights) if insights else 0}")

        return analyzed, total_insights

    def _llm_analyze_session(self, text: str, short_id: str,
                              raw_mb: float, user_msgs: int, asst_msgs: int,
                              subagents: int, date: str):
        """Send session to Qwen14B for analysis."""
        try:
            from memory.llm_priority_queue import butler_query
        except ImportError:
            return self._fallback_analyze(text, short_id)

        system_prompt = """You are a session historian analyzing Atlas (Claude Code) coding sessions.

Analyze the session to extract actionable insights:
- **Summary**: What happened in 2-3 sentences?
- **Accomplishments**: What was completed?
- **Failures**: What didn't work or was abandoned?
- **Patterns**: What recurring patterns did you notice (techniques that worked, common blockers)?
- **Lessons**: What wisdom from this session helps future work?
- **Usefulness**: How much value was extracted?

Usefulness guide:
- 1.0 = Major feature shipped, critical bug fixed
- 0.7 = Good progress, multiple tasks completed
- 0.4 = Mixed results, some progress some failures
- 0.2 = Mostly spinning wheels, prompt-too-long crashes
- 0.0 = Empty/trivial session

Share your analysis in whatever way makes sense. Both structured JSON and natural language analyses are equally useful."""

        user_prompt = f"""Session {short_id} | Date: {date} | Size: {raw_mb:.0f}MB | Messages: {user_msgs} user, {asst_msgs} assistant | Subagents: {subagents}

Transcript:
{text}"""

        try:
            result = butler_query(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                profile="deep",  # 2048 tokens — full session deserves rich analysis
            )

            if result:
                return self._parse_llm_result(result)
        except Exception as e:
            logger.warning(f"LLM analysis failed for {short_id}: {e}")

        return self._fallback_analyze(text, short_id)

    def _parse_llm_result(self, result: str):
        """Parse LLM response (JSON or natural language).

        Tries JSON first for backward compatibility, then extracts from natural language.
        """
        # === ATTEMPT 1: Try JSON parsing (backward compatibility) ===
        try:
            # Handle markdown code blocks
            if "```json" in result:
                result_text = result.split("```json")[1].split("```")[0]
            elif "```" in result:
                result_text = result.split("```")[1].split("```")[0]
            else:
                result_text = result

            data = json.loads(result_text.strip())

            summary = data.get('summary', '')
            insights = []
            for cat in ('insights', 'patterns', 'failures'):
                for item in data.get(cat, []):
                    insights.append({'type': cat, 'content': item, 'confidence': 0.6})

            score = float(data.get('usefulness_score', 0.5))
            topics = data.get('key_topics', [])

            return summary, insights, score, json.dumps(topics)

        except (json.JSONDecodeError, ValueError, KeyError) as _e:
            logger.debug("JSON LLM parse failed, falling back to NL extraction: %s", _e)

        # === ATTEMPT 2: Extract from natural language response ===
        # Use first paragraph or first 200 chars as summary
        summary = result.split('\n')[0][:300]

        # Extract score from response (look for decimals or keywords)
        score = 0.5  # default
        score_match = re.search(r'\b(0?\.\d+|[0-9]+\s*(?:out of|/)\s*[0-9])(?:\b|[.\s])', result, re.IGNORECASE)
        if score_match:
            try:
                score_text = score_match.group(1)
                # Handle "X/100" or "X out of 100" formats
                if '/' in score_text or 'out of' in score_match.group(0):
                    parts = re.findall(r'\d+(?:\.\d+)?', score_text)
                    if len(parts) >= 2:
                        score = float(parts[0]) / float(parts[1])
                    else:
                        score = float(parts[0]) / 100
                else:
                    score = float(parts[0]) if (parts := re.findall(r'\d+(?:\.\d+)?', score_text)) else 0.5
                score = max(0.0, min(1.0, score))
            except (ValueError, IndexError):
                score = 0.5

        # Extract patterns, accomplishments, failures via keyword matching
        insights = []
        result_lower = result.lower()

        # Look for accomplishments
        if re.search(r'accomplish|completed?|shipped|fixed|done', result_lower):
            insights.append({
                'type': 'accomplishments',
                'content': 'Work completed successfully',
                'confidence': 0.4
            })

        # Look for failures
        if re.search(r'fail|failed?|blocked?|stuck|crash|error', result_lower):
            insights.append({
                'type': 'failures',
                'content': 'Some issues encountered',
                'confidence': 0.4
            })

        # Look for patterns
        if re.search(r'pattern|technique|approach|method|process', result_lower):
            insights.append({
                'type': 'patterns',
                'content': 'Useful patterns identified',
                'confidence': 0.4
            })

        # Topic extraction (just pull capitalized phrases)
        topics = ['analysis', 'session_review']
        topic_candidates = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', result)
        if topic_candidates:
            topics = list(set(topic_candidates[:5]))

        return summary, insights, score, json.dumps(topics)

    def _fallback_analyze(self, text: str, short_id: str):
        """Keyword-based analysis when LLM unavailable."""
        text_lower = text.lower()

        # Simple keyword scoring
        score = 0.3
        topics = set()

        positive = ['fixed', 'completed', 'deployed', 'success', 'working', 'verified', 'done']
        negative = ['error', 'failed', 'broken', 'prompt too long', 'crash', 'timeout']

        for word in positive:
            if word in text_lower:
                score += 0.05
                topics.add(word)
        for word in negative:
            if word in text_lower:
                score -= 0.05
                topics.add(word)

        # Domain detection
        domains = {
            'webhook': ['webhook', 'injection', 'section'],
            'database': ['sqlite', 'postgres', 'db', 'migration'],
            'docker': ['docker', 'container', 'compose'],
            'llm': ['qwen', 'vllm', 'llm', 'butler'],
            'scheduler': ['scheduler', 'celery', 'cron', 'job'],
            'evidence': ['evidence', 'pipeline', 'outcome', 'claim'],
        }
        for domain, keywords in domains.items():
            if any(kw in text_lower for kw in keywords):
                topics.add(domain)

        score = max(0.0, min(1.0, score))
        summary = f"[keyword-only] Session {short_id}, score={score:.1f}"

        return summary, [], score, json.dumps(list(topics)[:10])

    def _store_analysis(self, session_id: str, summary: str,
                        insights: list, score: float, topics: str):
        """Store LLM analysis results."""
        conn = connect_wal(self.db_path)
        try:
            conn.execute('''
                UPDATE archived_sessions
                SET llm_summary = ?, llm_analyzed_at = ?,
                    usefulness_score = ?, key_topics = ?,
                    llm_insights = ?
                WHERE session_id = ?
            ''', (
                summary, datetime.now().isoformat(),
                score, topics,
                json.dumps(insights) if insights else None,
                session_id,
            ))

            # Store individual insights
            for insight in (insights or []):
                conn.execute('''
                    INSERT INTO session_insights
                    (session_id, insight_type, content, confidence, created_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    session_id, insight.get('type', 'general'),
                    insight.get('content', ''),
                    insight.get('confidence', 0.5),
                    datetime.now().isoformat(),
                ))

            conn.commit()
        finally:
            conn.close()

    # ─── Phase 2.5: Segment Gold Text for Rich Mining ──────────

    # Segmentation constants
    TARGET_SEGMENT_CHARS = 20000   # ~5K tokens — optimal for Qwen3-4B narrow extract
    MIN_SEGMENT_CHARS = 8000       # Don't create tiny trailing segments
    MAX_SEGMENT_CHARS = 28000      # Allow flexibility at conversation boundaries

    def _phase2_segment_gold(self) -> int:
        """Segment gold_text into ~20K char conversation-boundary chunks for 16-pass mining.

        Incremental: only creates new segments for content beyond last segment's end offset.
        Splits on USER: boundaries so segments contain complete conversation arcs.
        """
        conn = connect_wal(self.db_path)
        try:
            # Get sessions with gold_text
            sessions = conn.execute('''
                SELECT session_id, gold_text, length(gold_text) as chars
                FROM archived_sessions
                WHERE gold_text IS NOT NULL AND length(gold_text) > ?
            ''', (self.MIN_SEGMENT_CHARS,)).fetchall()
        finally:
            conn.close()

        if not sessions:
            return 0

        total_new = 0
        for session_id, gold_text, chars in sessions:
            new_segs = self._segment_session(session_id, gold_text, chars)
            total_new += new_segs

        if total_new > 0:
            logger.info(f"[segment] Created {total_new} new gold segments")
        return total_new

    def _segment_session(self, session_id: str, gold_text: str, total_chars: int) -> int:
        """Create conversation-boundary segments for a single session. Returns count of new segments."""
        conn = connect_wal(self.db_path)
        try:
            # Find where we left off
            row = conn.execute('''
                SELECT MAX(char_offset_end), MAX(segment_index)
                FROM gold_segments WHERE session_id = ?
            ''', (session_id,)).fetchone()
            last_end = row[0] or 0
            last_index = row[1] if row[1] is not None else -1

            # Nothing new to segment
            if total_chars <= last_end + self.MIN_SEGMENT_CHARS:
                return 0

            # Find USER: boundaries in the new content
            new_content = gold_text[last_end:]
            boundaries = [m.start() + last_end for m in re.finditer(r'(?:^|\n)USER:', new_content)]

            # If no USER: boundaries, use incremental extract markers as fallback
            if not boundaries:
                boundaries = [m.start() + last_end
                              for m in re.finditer(r'--- \[incremental extract\] ---', new_content)]

            # If still no boundaries, create one big segment
            if not boundaries:
                boundaries = [last_end]

            # Group boundaries into ~TARGET_SEGMENT_CHARS chunks
            segments = []
            seg_start = last_end
            for i, boundary in enumerate(boundaries):
                seg_len = boundary - seg_start
                # If this boundary pushes us past target, finalize current segment
                if seg_len >= self.TARGET_SEGMENT_CHARS and boundary > seg_start:
                    segments.append((seg_start, boundary))
                    seg_start = boundary

            # Final segment: remaining content
            if total_chars - seg_start >= self.MIN_SEGMENT_CHARS:
                segments.append((seg_start, total_chars))
            elif segments and total_chars > seg_start:
                # Too small — extend last segment
                segments[-1] = (segments[-1][0], total_chars)
            elif not segments and total_chars > seg_start:
                # Only content and it's below MIN but above nothing — create it
                segments.append((seg_start, total_chars))

            # Insert segments
            new_count = 0
            now = datetime.now().isoformat()
            for offset, (start, end) in enumerate(segments):
                seg_index = last_index + 1 + offset
                seg_text = gold_text[start:end]
                user_turns = seg_text.count('USER:')
                atlas_turns = seg_text.count('ATLAS:')

                try:
                    conn.execute('''
                        INSERT OR IGNORE INTO gold_segments
                        (session_id, segment_index, segment_text, char_offset_start,
                         char_offset_end, user_turns, atlas_turns, created_at,
                         gold_text_size_at_creation)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        session_id, seg_index, seg_text, start, end,
                        user_turns, atlas_turns, now, total_chars,
                    ))
                    new_count += 1
                except sqlite3.IntegrityError as _e:
                    logger.debug("segment insert skipped (UNIQUE conflict): %s", _e)

            conn.commit()
            return new_count
        finally:
            conn.close()

    # ─── Phase 3: Feed Evidence Pipeline ───────────────────────

    def _phase3_feed_pipeline(self) -> int:
        """Feed high-confidence insights to the evidence pipeline."""
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute('''
                SELECT id, session_id, insight_type, content, confidence
                FROM session_insights
                WHERE fed_to_pipeline = 0 AND confidence >= 0.6
                ORDER BY confidence DESC
                LIMIT 10
            ''')
            insights = cursor.fetchall()
        finally:
            conn.close()

        fed = 0
        for row in insights:
            insight_id, session_id, itype, content, confidence = row

            try:
                self._emit_to_pipeline(session_id, itype, content, confidence)
                conn = connect_wal(self.db_path)
                try:
                    conn.execute(
                        'UPDATE session_insights SET fed_to_pipeline = 1 WHERE id = ?',
                        (insight_id,)
                    )
                    conn.commit()
                finally:
                    conn.close()
                fed += 1
            except Exception as e:
                logger.warning(f"Failed to feed insight {insight_id}: {e}")

        return fed

    def _emit_to_pipeline(self, session_id: str, itype: str,
                          content: str, confidence: float):
        """Emit insight to observability evidence pipeline."""
        try:
            from memory.observability_store import get_observability_store
            store = get_observability_store()

            import hashlib
            content_hash = hashlib.md5(content.encode()).hexdigest()[:12]
            item_id = f"sh_{session_id[:8]}_{content_hash}"

            notes = f"[{itype}] {content[:500]}"
            promotion_rules = {
                "source": "session_historian",
                "confidence": confidence,
                "min_confirmations": 1 if confidence >= 0.8 else 2,
                "min_hours": 12,
                "auto_promote_on": "age" if confidence >= 0.8 else "repeated_success",
            }

            store.quarantine_item(
                item_id=item_id,
                item_type="learning",
                promotion_rules=promotion_rules,
                notes=notes,
            )
        except Exception as e:
            logger.debug(f"Evidence pipeline unavailable: {e}")

    # ─── Cleanup ─────────────────────────────────────────────────

    def _cleanup_session(self, session_id: str, project_dir: Path) -> float:
        """Event-triggered cleanup: delete raw JSONL + subdir for a single session.

        Called immediately after gold extraction confirms save.
        Returns MB reclaimed.
        """
        reclaimed = 0.0
        jsonl = project_dir / f"{session_id}.jsonl"
        subdir = project_dir / session_id

        deleted = False
        if jsonl.exists():
            raw_mb = jsonl.stat().st_size / (1024 * 1024)
            jsonl.unlink()
            reclaimed += raw_mb
            deleted = True
        if subdir.exists():
            try:
                sub_mb = sum(
                    f.stat().st_size for f in subdir.rglob('*') if f.is_file()
                ) / (1024 * 1024)
                shutil.rmtree(subdir)
                reclaimed += sub_mb
            except Exception as _e:
                logger.debug("minor error (non-critical): %s", _e)
            deleted = True

        if deleted:
            conn = connect_wal(self.db_path)
            try:
                conn.execute(
                    'UPDATE archived_sessions SET raw_deleted = 1 WHERE session_id = ?',
                    (session_id,)
                )
                conn.commit()
            finally:
                conn.close()
            logger.info(f"[event-cleanup] {session_id[:8]} ({reclaimed:.1f}MB reclaimed)")

        return reclaimed

    # ─── Phase 4: Safety sweep (catches anything missed by event cleanup) ──

    def _phase4_cleanup_archived(self) -> float:
        """Safety sweep for sessions that event-triggered cleanup missed.

        This is a fallback — primary cleanup is event-triggered in _phase3_extract_all.
        Catches edge cases: sessions extracted before this code existed, races, etc.
        """
        active_ids = self._get_active_session_ids()

        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute('''
                SELECT session_id, raw_size_mb
                FROM archived_sessions
                WHERE raw_deleted = 0
                  AND gold_text IS NOT NULL
            ''')
            candidates = cursor.fetchall()
        finally:
            conn.close()

        reclaimed = 0.0
        project_dir = SESSIONS_DIR / SUPERREPO_KEY
        stale_cutoff = time.time() - (TAB_OPEN_MINS * 60)  # Only skip very recent files

        for session_id, raw_mb in candidates:
            if session_id in active_ids:
                continue
            reclaimed += self._cleanup_session(session_id, project_dir)

        # Phase 4b: Clean orphaned subagent directories
        reclaimed += self._cleanup_orphaned_subdirs(active_ids, project_dir)

        # Phase 4c: Clean trivial sessions (0-byte or < MIN_SIZE_KB, not active)
        reclaimed += self._cleanup_trivial_sessions(active_ids, project_dir, stale_cutoff)

        return reclaimed

    def _cleanup_trivial_sessions(self, active_ids: set, project_dir: Path,
                                   stale_cutoff: float) -> float:
        """Remove 0-byte and trivially small JSONL files + their subdirs.

        These are sessions that were created (VS Code opened them) but never
        had an actual conversation. No gold to extract — safe to delete.
        """
        if not project_dir.exists():
            return 0.0

        reclaimed = 0.0

        for jsonl in project_dir.glob("*.jsonl"):
            if jsonl.stem.startswith("agent-"):
                continue

            session_id = jsonl.stem
            if session_id in active_ids:
                continue

            try:
                stat = jsonl.stat()
            except OSError:
                continue

            # Only target trivially small files (< MIN_SIZE_KB)
            if stat.st_size >= MIN_SIZE_KB * 1024:
                continue

            # Must be old enough
            if stat.st_mtime > stale_cutoff:
                continue

            # Delete the trivial JSONL
            size_mb = stat.st_size / (1024 * 1024)
            jsonl.unlink()
            reclaimed += size_mb

            # Also clean up any associated subdirectory
            subdir = project_dir / session_id
            if subdir.exists():
                try:
                    sub_size = sum(
                        f.stat().st_size for f in subdir.rglob('*') if f.is_file()
                    ) / (1024 * 1024)
                    shutil.rmtree(subdir)
                    reclaimed += sub_size
                except Exception as _e:
                    logger.debug("minor error (non-critical): %s", _e)

            logger.info(f"Cleaned trivial session {session_id[:8]} "
                       f"({stat.st_size}B JSONL)")

        return reclaimed

    def _cleanup_orphaned_subdirs(self, active_ids: set, project_dir: Path) -> float:
        """Extract subagent gold then remove orphaned directories.

        Orphans = subdirs whose parent JSONL was already deleted (by Claude Code
        or by the historian). Before removal, recursively extracts gold from all
        subagent JSONL files (sub-agents, sub-sub-agents, etc.).
        """
        if not project_dir.exists():
            return 0.0

        reclaimed = 0.0
        stale_cutoff = time.time() - (STALE_HOURS * 3600)

        for entry in project_dir.iterdir():
            if not entry.is_dir():
                continue
            session_id = entry.name
            if len(session_id) < 30 or session_id.startswith('.'):
                continue

            jsonl = project_dir / f"{session_id}.jsonl"
            if jsonl.exists():
                continue  # Parent JSONL still here — not orphaned

            if session_id in active_ids:
                continue

            try:
                if entry.stat().st_mtime > stale_cutoff:
                    continue
            except OSError:
                continue

            # EXTRACT GOLD before removing — dig into subagents recursively
            try:
                self._extract_subagent_gold(session_id, entry)
            except Exception as e:
                logger.warning(f"Subagent gold extraction failed for {session_id[:8]}: {e}")

            # Now safe to remove
            try:
                size_mb = sum(
                    f.stat().st_size for f in entry.rglob('*') if f.is_file()
                ) / (1024 * 1024)
                shutil.rmtree(entry)
                reclaimed += size_mb
                logger.info(f"Cleaned orphan subdir {session_id[:8]} ({size_mb:.1f}MB)")
            except Exception as e:
                logger.debug(f"Failed to clean orphan {session_id[:8]}: {e}")

        return reclaimed

    def _extract_subagent_gold(self, parent_session_id: str, session_dir: Path):
        """Recursively extract gold from all subagent JSONL files in a session dir.

        Digs into subagents/ and any nested subagent dirs to find all JSONL files.
        Combines all subagent text into a single archive entry for the parent session.
        """
        # Find ALL .jsonl files recursively (subagents, sub-sub-agents, etc.)
        # Sort DEEPEST FIRST — extract sub-sub-agents before their parents
        subagent_files = sorted(
            session_dir.rglob("*.jsonl"),
            key=lambda p: (-len(p.parts), p.stat().st_mtime)
        )

        if not subagent_files:
            return

        # Check if already archived (don't re-extract)
        conn = connect_wal(self.db_path)
        try:
            existing = conn.execute(
                'SELECT gold_text FROM archived_sessions WHERE session_id = ?',
                (parent_session_id,)
            ).fetchone()
            existing_gold = existing[0] if existing else ''
        finally:
            conn.close()

        all_gold_parts = []
        all_code_artifacts = []
        total_user = 0
        total_asst = 0

        for sa_file in subagent_files:
            try:
                gold = self._extract_gold(sa_file)
                if not gold or not gold.get('gold_text'):
                    continue

                agent_id = sa_file.stem  # e.g. "agent-a008753"
                depth = len(sa_file.relative_to(session_dir).parts) - 1
                prefix = "SUB" * max(1, depth) + "AGENT"

                all_gold_parts.append(
                    f"--- [{prefix}: {agent_id}] ---\n{gold['gold_text']}"
                )
                all_code_artifacts.extend(gold.get('code_artifacts', []))
                total_user += gold['user_messages']
                total_asst += gold['assistant_messages']
            except Exception as e:
                logger.debug(f"Failed to extract {sa_file.name}: {e}")

        if not all_gold_parts:
            return

        subagent_gold = "\n\n".join(all_gold_parts)
        now = datetime.now().isoformat()

        conn = connect_wal(self.db_path)
        try:
            if existing_gold:
                # Append subagent gold to existing archive
                combined = existing_gold + "\n\n=== SUBAGENT GOLD (extracted from orphan) ===\n\n" + subagent_gold
                conn.execute('''
                    UPDATE archived_sessions SET
                        gold_text = ?, extracted_at = ?,
                        user_messages = user_messages + ?,
                        assistant_messages = assistant_messages + ?,
                        code_artifact_count = code_artifact_count + ?
                    WHERE session_id = ?
                ''', (combined, now, total_user, total_asst,
                      len(all_code_artifacts), parent_session_id))
            else:
                # Create new archive entry from subagent gold alone
                gold_size_kb = len(subagent_gold.encode('utf-8')) / 1024
                conn.execute('''
                    INSERT INTO archived_sessions
                    (session_id, project, extracted_at, session_date, raw_size_mb,
                     gold_size_kb, user_messages, assistant_messages, subagent_count,
                     gold_text, code_artifact_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    parent_session_id, SUPERREPO_KEY, now,
                    now, 0, gold_size_kb,
                    total_user, total_asst, len(subagent_files),
                    subagent_gold, len(all_code_artifacts),
                ))

            # Store code artifacts
            for artifact in all_code_artifacts:
                conn.execute('''
                    INSERT INTO code_artifacts
                    (session_id, file_path, language, code, context_before,
                     artifact_type, size_bytes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    parent_session_id, artifact.get('file_path'),
                    artifact.get('language', 'text'), artifact['code'],
                    artifact.get('context_before'),
                    artifact.get('artifact_type', 'written'),
                    artifact.get('size_bytes', 0), now,
                ))

            conn.commit()
            logger.info(f"Extracted subagent gold from {parent_session_id[:8]}: "
                       f"{len(subagent_files)} files, {total_user}u/{total_asst}a msgs, "
                       f"{len(all_code_artifacts)} code artifacts")
        finally:
            conn.close()

        # Detect project tag for newly created entries
        try:
            self._update_project_tag(parent_session_id)
        except Exception as _e:
            logger.debug("_update_project_tag failed (non-critical): %s", _e)

    # ─── Helpers ───────────────────────────────────────────────

    def _get_active_session_ids(self) -> set:
        """Find sessions in use by running Claude processes OR with open VS Code tabs.

        Three detection methods (union of all):
        1. ps aux --resume flag (resumed sessions)
        2. Recently modified JSONL files (< TAB_OPEN_MINS) = tab still open
        3. lsof check for open file handles (most reliable)
        """
        active = set()

        # Method 1: ps aux --resume flag (existing approach)
        try:
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if "--resume" in line:
                    parts = line.split("--resume ")
                    if len(parts) > 1:
                        active.add(parts[1].split()[0].strip())
        except Exception as _e:
            logger.debug("minor error (non-critical): %s", _e)

        # Method 2: Recently modified JSONL = tab still open
        # New tabs don't have --resume, but they DO write to JSONL files
        project_dir = SESSIONS_DIR / SUPERREPO_KEY
        if project_dir.exists():
            tab_cutoff = time.time() - (TAB_OPEN_MINS * 60)
            for jsonl in project_dir.glob("*.jsonl"):
                if jsonl.stem.startswith("agent-"):
                    continue
                try:
                    if jsonl.stat().st_mtime > tab_cutoff:
                        active.add(jsonl.stem)
                except OSError as _e:
                    logger.debug("stat failed (non-critical): %s", _e)

        return active

    def _is_archived(self, session_id: str) -> bool:
        """Check if session is already in archive."""
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute(
                'SELECT 1 FROM archived_sessions WHERE session_id = ?',
                (session_id,)
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()

    def _count_unanalyzed(self) -> int:
        """Count sessions extracted but not yet LLM-analyzed."""
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute('''
                SELECT COUNT(*) FROM archived_sessions
                WHERE llm_analyzed_at IS NULL AND gold_text IS NOT NULL
            ''')
            return cursor.fetchone()[0]
        finally:
            conn.close()

    # ─── Rehydration API (session crash recovery) ──────────────

    def _vault_fallback_rehydration(self, session_id: str = None) -> Optional[dict]:
        """Read newest vault file as fallback when DB has no results.

        Returns dict with keys: session_id, content, file_path, source
        or None if no vault files exist.
        """
        vault_dir = Path(__file__).parent.parent / ".projectdna" / "raw" / "sessions"
        if not vault_dir.exists():
            logger.warning(f"Vault directory missing: {vault_dir} — no fallback available")
            return None

        # If session_id given, try to find matching file
        if session_id:
            short = session_id[:12]
            matches = list(vault_dir.glob(f"*{short}*.md"))
            if not matches:
                # Try shorter prefix
                matches = list(vault_dir.glob(f"*{session_id[:8]}*.md"))
            if matches:
                target = max(matches, key=lambda p: p.stat().st_mtime)
            else:
                return None
        else:
            # Get newest vault file by mtime
            all_md = list(vault_dir.glob("*.md"))
            if not all_md:
                return None
            target = max(all_md, key=lambda p: p.stat().st_mtime)

        try:
            text = target.read_text(encoding='utf-8')
            # Extract session_id from YAML frontmatter or filename
            sid = session_id or target.stem.split("_")[-1]
            # Try to get session_id from frontmatter
            if 'session_id:' in text:
                for line in text.split('\n'):
                    if line.strip().startswith('session_id:'):
                        sid = line.split(':', 1)[1].strip().strip('"')
                        break
            return {
                'session_id': sid,
                'content': text,
                'file_path': str(target),
                'source': 'vault_file',
            }
        except Exception as e:
            logger.warning(f"Vault fallback read failed: {e}")
            return None

    def _check_db_integrity_quick(self) -> bool:
        """Quick DB integrity check for rehydration path. Returns True if OK."""
        try:
            conn = connect_wal(self.db_path)
            integrity = conn.execute("PRAGMA integrity_check(1)").fetchone()[0]
            conn.close()
            return integrity == "ok"
        except Exception as e:
            logger.warning(f"DB integrity check failed: {e}")
            return False

    def get_session_rehydration(self, session_id: str = None,
                                 max_tokens: int = 3000) -> Optional[str]:
        """Get rehydration context for webhook injection after session crash.

        If session_id is provided, returns that session's gold.
        If None, returns the MOST RECENTLY ACTIVE session's context.

        Used by persistent_hook_structure.py Section 4 to inject
        prior session context when a new session starts.
        """
        row = None
        try:
            conn = connect_wal(self.db_path)
            try:
                if session_id:
                    row = conn.execute('''
                        SELECT session_id, llm_summary, gold_text,
                               user_messages, assistant_messages, key_topics
                        FROM archived_sessions WHERE session_id = ?
                    ''', (session_id,)).fetchone()
                else:
                    row = conn.execute('''
                        SELECT session_id, llm_summary, gold_text,
                               user_messages, assistant_messages, key_topics
                        FROM archived_sessions
                        ORDER BY extracted_at DESC LIMIT 1
                    ''').fetchone()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"DB access failed during rehydration: {e}")

        if not row:
            # Vault file fallback — read from .projectdna/raw/sessions/
            vault = self._vault_fallback_rehydration(session_id)
            if vault:
                logger.info(f"Vault fallback for rehydration: {vault['file_path']}")
                parts = [f"SESSION REHYDRATION ({vault['session_id'][:8]}) [from vault file]"]
                parts.append(vault['content'])
                return "\n".join(parts)
            return None

        sid, summary, gold, user_msgs, asst_msgs, topics = row
        parts = [f"SESSION REHYDRATION ({sid[:8]})"]
        parts.append(f"Messages: {user_msgs}u/{asst_msgs}a | Topics: {topics or 'unknown'}")

        if summary:
            parts.append(f"Summary: {summary}")

        if gold:
            # Get the TAIL of the gold (most recent messages are most relevant)
            if len(gold) > max_tokens * 3:
                gold = gold[-(max_tokens * 3):]
                gold = "...\n" + gold[gold.find('\n') + 1:]  # clean cut

            parts.append("Recent transcript:")
            parts.append(gold[:max_tokens * 3])

        return "\n".join(parts)

    def get_structured_rehydration(self, session_id: str = None,
                                    project: str = None,
                                    compact: bool = False) -> Optional[str]:
        """Structured rehydration for crash recovery and compaction recovery.

        Args:
            session_id: Specific session to rehydrate from
            project: Filter to sessions matching this sub-project tag
                     (e.g. 'context-dna', 'er-simulator', 'auto' for auto-detect)
            compact: If True, returns a shorter format for post-compaction injection

        Returns a formatted output with clear sections:
        1. USER'S LAST 5 MESSAGES (exact, word-for-word)
        2. ATLAS' LAST 5 OUTPUTS (exact, word-for-word)
        3. SPAWNED AGENTS needing recovery
        4. SESSION SUMMARY + TOPICS
        5. FULL ARCHIVE AVAILABLE flag (for optional deeper reading)

        Designed to be read bottom-up (most recent first) to minimize
        context waste on session restart.
        """
        # DB access with vault fallback on any DB failure
        row = None
        db_error = None
        try:
            conn = connect_wal(self.db_path)
            try:
                if session_id:
                    # Try exact match first, then prefix match
                    row = conn.execute('''
                        SELECT session_id, llm_summary, gold_text,
                               user_messages, assistant_messages, key_topics,
                               code_artifact_count, subagent_count, session_date,
                               usefulness_score, project_tag
                        FROM archived_sessions WHERE session_id = ?
                    ''', (session_id,)).fetchone()
                    if not row:
                        row = conn.execute('''
                            SELECT session_id, llm_summary, gold_text,
                                   user_messages, assistant_messages, key_topics,
                                   code_artifact_count, subagent_count, session_date,
                                   usefulness_score, project_tag
                            FROM archived_sessions WHERE session_id LIKE ?
                            ORDER BY extracted_at DESC LIMIT 1
                        ''', (f'{session_id}%',)).fetchone()
                elif project and project != 'auto':
                    row = conn.execute('''
                        SELECT session_id, llm_summary, gold_text,
                               user_messages, assistant_messages, key_topics,
                               code_artifact_count, subagent_count, session_date,
                               usefulness_score, project_tag
                        FROM archived_sessions
                        WHERE project_tag = ?
                        ORDER BY extracted_at DESC LIMIT 1
                    ''', (project,)).fetchone()
                else:
                    row = conn.execute('''
                        SELECT session_id, llm_summary, gold_text,
                               user_messages, assistant_messages, key_topics,
                               code_artifact_count, subagent_count, session_date,
                               usefulness_score, project_tag
                        FROM archived_sessions
                        ORDER BY extracted_at DESC LIMIT 1
                    ''').fetchone()
            finally:
                conn.close()
        except Exception as e:
            db_error = str(e)
            logger.warning(f"DB access failed during rehydration: {e}")

        if not row:
            # Vault file fallback — structured output from .projectdna/raw/sessions/
            vault = self._vault_fallback_rehydration(session_id)
            if vault:
                reason = f"DB error: {db_error}" if db_error else "DB had no archived sessions"
                logger.info(f"Using vault fallback ({reason}): {vault['file_path']}")
                out = []
                out.append("=" * 60)
                out.append(f"SESSION REHYDRATION — VAULT FALLBACK ({reason})")
                out.append("=" * 60)
                out.append(f"Source: {vault['file_path']}")
                out.append(f"Session: {vault['session_id']}")
                out.append("")
                out.append("─── VAULT CONTENT ───")
                out.append(vault['content'])
                out.append("")
                out.append("─── NOTE ───")
                out.append("This is a vault file fallback. " + reason)
                out.append("The vault file may contain partial data from the last active export.")
                out.append("=" * 60)
                return "\n".join(out)
            return None

        sid, summary, gold, user_msgs, asst_msgs, topics, \
            code_count, subagent_count, date, score, proj_tag = row

        # ─── Noise prefixes to skip when building TASK CONTEXT ──────
        NOISE_PREFIXES = (
            "This session is being continued",
            "Your task is to create a detailed summary",
            "[Request interrupted by user]",
        )
        import re
        _IDE_TAG_RE = re.compile(r'<ide_opened_file>.*?</ide_opened_file>\s*', re.DOTALL)

        # Parse gold text to extract last N messages by role
        # For Atlas: separate text responses from tool-only messages
        user_raw_msgs = []
        user_clean_msgs = []    # Noise-filtered user messages (no continuation prompts)
        user_tasks = []         # Task-like messages (requests, instructions) — noise-filtered
        atlas_text_msgs = []    # Atlas messages with ACTUAL text (not just [Tool] markers)
        atlas_all_msgs = []     # All Atlas messages (fallback)
        atlas_status_msgs = []  # Status/summary messages (task completion, reports)
        if gold:
            for block in gold.split("\n\n"):
                block = block.strip()
                if block.startswith("USER: "):
                    content = block[6:]
                    user_raw_msgs.append(content)
                    # Strip IDE opened-file tags for cleaner display
                    clean_content = _IDE_TAG_RE.sub('', content).strip()
                    # Noise-filtered version for display
                    is_noise = any(content.startswith(p) for p in NOISE_PREFIXES)
                    if not is_noise and len(clean_content) >= 3:
                        user_clean_msgs.append(clean_content)
                    # Also build noise-filtered task list
                    if not is_noise and len(clean_content) >= 3:
                        content_lower = clean_content.lower()
                        if any(kw in content_lower for kw in
                               ('please', 'can you', 'fix', 'add', 'implement',
                                'check', 'run', 'deploy', 'update', 'create',
                                'configure', 'look into', 'make sure', "let's",
                                'yes', 'continue', 'go ahead', 'do it')):
                            user_tasks.append(clean_content)
                elif block.startswith("ATLAS: "):
                    content = block[7:]
                    atlas_all_msgs.append(content)
                    # Check if this message has real text (not just [Tool: ...] markers)
                    has_text = any(
                        line.strip() and not (line.strip().startswith('[') and line.strip().endswith(']'))
                        for line in content.split('\n')
                    )
                    if has_text:
                        atlas_text_msgs.append(content)
                        # Detect status/completion messages
                        content_lower = content.lower()
                        if any(kw in content_lower for kw in
                               ('completed', 'done', 'fixed', "here's what",
                                'summary', 'results:', 'status:', 'implemented',
                                'deployed', 'verified', 'the changes')):
                            atlas_status_msgs.append(content)
        # Prefer text-only messages; fall back to all if fewer than 5 text msgs
        atlas_msgs = atlas_text_msgs if len(atlas_text_msgs) >= 5 else atlas_all_msgs

        # Check for spawned subagents on disk
        subagent_files = []
        project_dir = SESSIONS_DIR / SUPERREPO_KEY
        subagent_dir = project_dir / sid / "subagents"
        if subagent_dir.exists():
            for sa in sorted(subagent_dir.glob("*.jsonl"),
                            key=lambda p: p.stat().st_mtime, reverse=True):
                size_kb = sa.stat().st_size / 1024
                subagent_files.append(f"{sa.stem[:12]}... ({size_kb:.0f}KB)")

        # Compact mode: shorter output for post-compaction injection
        msg_count = 3 if compact else 5

        # Build structured output
        out = []
        mode_label = "COMPACTION RECOVERY" if compact else "CRASH RECOVERY"
        out.append("=" * 60)
        out.append(f"SESSION REHYDRATION — {mode_label} CONTEXT")
        out.append("=" * 60)
        out.append(f"Session: {sid}")
        out.append(f"Date: {date} | Project: {proj_tag or 'untagged'}")
        out.append(f"Messages: {user_msgs} user / {asst_msgs} assistant")
        out.append(f"Code artifacts: {code_count} | Subagents: {subagent_count}")
        out.append(f"Score: {score or 'unscored'} | Topics: {topics or 'unknown'}")

        # Section 1: Summary (read first for orientation)
        out.append("")
        out.append("─── SUMMARY ───")
        if summary:
            out.append(summary)
        else:
            out.append("[Not yet LLM-analyzed]")

        # Section 2: Task context (noise-filtered, actionable)
        if user_tasks:
            out.append("")
            out.append(f"─── TASK CONTEXT (what the user asked for) ───")
            # Show last N task-like messages, deduplicated from raw messages
            recent_tasks = user_tasks[-msg_count:]
            for i, t in enumerate(recent_tasks, 1):
                truncated = t[:500] + "..." if len(t) > 500 else t
                out.append(f"  {i}. {truncated}")

        # Section 3: Last status (what Atlas accomplished)
        if atlas_status_msgs:
            out.append("")
            out.append(f"─── LAST STATUS (what Atlas reported) ───")
            recent_status = atlas_status_msgs[-msg_count:]
            for i, s in enumerate(recent_status, 1):
                truncated = s[:500] + "..." if len(s) > 500 else s
                out.append(f"  {i}. {truncated}")

        # Section 4: User's last N messages (noise-filtered, exact)
        display_user = user_clean_msgs if user_clean_msgs else user_raw_msgs
        out.append("")
        out.append(f"─── USER'S LAST {msg_count} MESSAGES (exact) ───")
        recent_user = display_user[-msg_count:] if display_user else []
        if recent_user:
            for i, msg in enumerate(recent_user, 1):
                out.append(f"\n[USER #{len(display_user) - len(recent_user) + i}]")
                out.append(msg)
        else:
            out.append("[No user messages found]")

        # Section 5: Atlas' last N outputs (exact, word-for-word)
        out.append("")
        out.append(f"─── ATLAS' LAST {msg_count} OUTPUTS (exact) ───")
        recent_atlas = atlas_msgs[-msg_count:] if atlas_msgs else []
        if recent_atlas:
            for i, msg in enumerate(recent_atlas, 1):
                out.append(f"\n[ATLAS #{len(atlas_msgs) - len(recent_atlas) + i}]")
                out.append(msg)
        else:
            out.append("[No assistant messages found]")

        # Section 6: Spawned agents
        out.append("")
        out.append("─── SPAWNED AGENTS ───")
        if subagent_files:
            out.append(f"{len(subagent_files)} subagent files found:")
            for sa in subagent_files[:10]:
                out.append(f"  • {sa}")
        else:
            out.append("[No subagent files]")

        # Section 7: Deeper context available
        gold_kb = len(gold.encode('utf-8')) / 1024 if gold else 0
        out.append("")
        out.append("─── DEEPER CONTEXT ───")
        out.append(f"Full archive: {gold_kb:.0f}KB of extracted gold available")
        filtered_note = f" ({len(user_raw_msgs) - len(user_clean_msgs)} noise filtered)" if len(user_clean_msgs) < len(user_raw_msgs) else ""
        out.append(f"Total messages: {len(user_raw_msgs)} user{filtered_note} + {len(atlas_msgs)} assistant")
        if code_count:
            out.append(f"Code artifacts: {code_count} preserved (searchable via session_historian.py search_code)")
        out.append("To read full archive: PYTHONPATH=. .venv/bin/python3 memory/session_historian.py rehydrate --full")
        out.append("=" * 60)

        return "\n".join(out)

    def get_active_session_context(self, current_session_id: str = None,
                                    max_items: int = 10) -> list:
        """Get insights weighted by recency for the CURRENT active session.

        Returns insights sorted by: current session first, then recency.
        The local LLM uses this to prioritize what's happening NOW.
        """
        conn = connect_wal(self.db_path)
        try:
            insights = []

            # Priority 1: Insights from the current session (if known)
            if current_session_id:
                cursor = conn.execute('''
                    SELECT insight_type, content, confidence, created_at
                    FROM session_insights
                    WHERE session_id = ?
                    ORDER BY created_at DESC LIMIT ?
                ''', (current_session_id, max_items))
                for r in cursor.fetchall():
                    insights.append({
                        'type': r[0], 'content': r[1],
                        'confidence': r[2], 'recency': 'current',
                        'created_at': r[3],
                    })

            # Priority 2: Recent insights from OTHER sessions (fill remaining slots)
            remaining = max_items - len(insights)
            if remaining > 0:
                exclude = current_session_id or ''
                cursor = conn.execute('''
                    SELECT si.insight_type, si.content, si.confidence,
                           si.created_at, si.session_id
                    FROM session_insights si
                    WHERE si.session_id != ?
                    ORDER BY si.created_at DESC LIMIT ?
                ''', (exclude, remaining))
                for r in cursor.fetchall():
                    insights.append({
                        'type': r[0], 'content': r[1],
                        'confidence': r[2], 'recency': 'recent',
                        'created_at': r[3],
                    })

            return insights
        finally:
            conn.close()

    # ─── Query API (for agents/scripts) ────────────────────────

    def get_session_summary(self, session_id: str) -> Optional[dict]:
        """Get summary for a specific session."""
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute('''
                SELECT session_id, session_date, raw_size_mb, user_messages,
                       assistant_messages, subagent_count, llm_summary,
                       usefulness_score, key_topics
                FROM archived_sessions WHERE session_id = ?
            ''', (session_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return {
                'session_id': row[0], 'date': row[1], 'raw_mb': row[2],
                'user_msgs': row[3], 'asst_msgs': row[4], 'subagents': row[5],
                'summary': row[6], 'score': row[7], 'topics': row[8],
            }
        finally:
            conn.close()

    def get_recent_insights(self, limit: int = 20) -> list:
        """Get most recent insights for context injection."""
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute('''
                SELECT si.insight_type, si.content, si.confidence,
                       a.session_date, a.session_id
                FROM session_insights si
                JOIN archived_sessions a ON a.session_id = si.session_id
                ORDER BY si.created_at DESC
                LIMIT ?
            ''', (limit,))
            return [
                {'type': r[0], 'content': r[1], 'confidence': r[2],
                 'date': r[3], 'session': r[4][:8]}
                for r in cursor.fetchall()
            ]
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """Get archive statistics."""
        conn = connect_wal(self.db_path)
        try:
            stats = {}
            cursor = conn.execute('SELECT COUNT(*), SUM(raw_size_mb), AVG(usefulness_score) FROM archived_sessions')
            row = cursor.fetchone()
            stats['total_sessions'] = row[0]
            stats['total_raw_mb'] = round(row[1] or 0, 1)
            stats['avg_usefulness'] = round(row[2] or 0, 2)

            cursor = conn.execute('SELECT COUNT(*) FROM archived_sessions WHERE llm_analyzed_at IS NOT NULL')
            stats['llm_analyzed'] = cursor.fetchone()[0]

            cursor = conn.execute('SELECT COUNT(*) FROM archived_sessions WHERE raw_deleted = 1')
            stats['raw_cleaned'] = cursor.fetchone()[0]

            cursor = conn.execute('SELECT COUNT(*) FROM session_insights')
            stats['total_insights'] = cursor.fetchone()[0]

            cursor = conn.execute('SELECT SUM(raw_size_mb) FROM archived_sessions WHERE raw_deleted = 1')
            stats['reclaimed_mb'] = round((cursor.fetchone()[0] or 0), 1)

            return stats
        finally:
            conn.close()

    # ─── Vector Embeddings ──────────────────────────────────────

    def _generate_embeddings(self, session_id: str, gold_text: str):
        """Generate vector embeddings for semantic search.

        Uses local Qwen14B via butler_query to create text summaries,
        then stores them as searchable chunks. When a proper embedding
        model is available (e.g., sentence-transformers), this can
        produce real float vectors for cosine similarity search.
        """
        # Chunk the text into ~500-char segments for granular search
        chunks = self._chunk_text(gold_text, chunk_size=500, overlap=50)
        if not chunks:
            return

        conn = connect_wal(self.db_path)
        try:
            for i, chunk in enumerate(chunks[:100]):  # Cap at 100 chunks per session
                conn.execute('''
                    INSERT INTO session_embeddings
                    (session_id, chunk_index, chunk_text, model, created_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    session_id, i, chunk, 'chunk_text',
                    datetime.now().isoformat(),
                ))
            conn.commit()
            logger.debug(f"Stored {len(chunks[:100])} text chunks for {session_id[:8]}")
        finally:
            conn.close()

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list:
        """Split text into overlapping chunks for search indexing."""
        if len(text) <= chunk_size:
            return [text]
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]
            # Try to break at a newline
            if end < len(text):
                last_nl = chunk.rfind('\n')
                if last_nl > chunk_size // 2:
                    chunk = chunk[:last_nl]
                    end = start + last_nl
            chunks.append(chunk.strip())
            start = end - overlap
        return [c for c in chunks if c]

    def semantic_search(self, query: str, limit: int = 10) -> list:
        """Search session archive using text matching on chunks.

        When a real embedding model is integrated, this will use
        cosine similarity on float vectors. For now, uses SQLite
        LIKE matching on pre-chunked text — still fast and useful.
        """
        conn = connect_wal(self.db_path)
        try:
            # Split query into keywords for broader matching
            keywords = [w.strip() for w in query.lower().split() if len(w.strip()) > 2]
            if not keywords:
                return []

            # Build OR conditions for each keyword
            conditions = " OR ".join(["LOWER(chunk_text) LIKE ?" for _ in keywords])
            params = [f'%{kw}%' for kw in keywords]
            params.append(limit)

            cursor = conn.execute(f'''
                SELECT DISTINCT e.session_id, e.chunk_text,
                       a.session_date, a.llm_summary, a.usefulness_score
                FROM session_embeddings e
                JOIN archived_sessions a ON a.session_id = e.session_id
                WHERE {conditions}
                ORDER BY a.usefulness_score DESC
                LIMIT ?
            ''', params)

            results = []
            seen_sessions = set()
            for row in cursor.fetchall():
                sid = row[0]
                if sid in seen_sessions:
                    continue
                seen_sessions.add(sid)
                results.append({
                    'session': sid[:8],
                    'match_chunk': row[1][:200],
                    'date': row[2],
                    'summary': row[3],
                    'score': row[4],
                })
            return results
        finally:
            conn.close()

    def search_code(self, query: str, language: str = None,
                    limit: int = 20) -> list:
        """Search code artifacts across all archived sessions."""
        conn = connect_wal(self.db_path)
        try:
            if language:
                cursor = conn.execute('''
                    SELECT c.session_id, c.file_path, c.language, c.code,
                           c.artifact_type, a.session_date
                    FROM code_artifacts c
                    JOIN archived_sessions a ON a.session_id = c.session_id
                    WHERE c.language = ? AND (c.code LIKE ? OR c.file_path LIKE ?)
                    ORDER BY a.session_date DESC
                    LIMIT ?
                ''', (language, f'%{query}%', f'%{query}%', limit))
            else:
                cursor = conn.execute('''
                    SELECT c.session_id, c.file_path, c.language, c.code,
                           c.artifact_type, a.session_date
                    FROM code_artifacts c
                    JOIN archived_sessions a ON a.session_id = c.session_id
                    WHERE c.code LIKE ? OR c.file_path LIKE ?
                    ORDER BY a.session_date DESC
                    LIMIT ?
                ''', (f'%{query}%', f'%{query}%', limit))

            return [
                {
                    'session': r[0][:8], 'file': r[1], 'lang': r[2],
                    'code_preview': r[3][:300], 'type': r[4], 'date': r[5],
                }
                for r in cursor.fetchall()
            ]
        finally:
            conn.close()

    def search_sessions(self, query: str, limit: int = 10) -> list:
        """Search archived sessions by keyword."""
        conn = connect_wal(self.db_path)
        try:
            cursor = conn.execute('''
                SELECT session_id, session_date, raw_size_mb,
                       llm_summary, usefulness_score, key_topics
                FROM archived_sessions
                WHERE gold_text LIKE ? OR llm_summary LIKE ? OR key_topics LIKE ?
                ORDER BY usefulness_score DESC
                LIMIT ?
            ''', (f'%{query}%', f'%{query}%', f'%{query}%', limit))
            return [
                {'session': r[0][:8], 'date': r[1], 'mb': r[2],
                 'summary': r[3], 'score': r[4], 'topics': r[5]}
                for r in cursor.fetchall()
            ]
        finally:
            conn.close()


# ─── CLI Interface ─────────────────────────────────────────────

def main():
    """CLI for manual runs."""
    import sys
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    historian = SessionHistorian()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == 'stats':
            stats = historian.get_stats()
            print(f"Archive Stats:")
            for k, v in stats.items():
                print(f"  {k}: {v}")

        elif cmd == 'search':
            query = sys.argv[2] if len(sys.argv) > 2 else ''
            results = historian.search_sessions(query)
            for r in results:
                print(f"  {r['session']} | {r['date']} | {r['mb']:.0f}MB | "
                      f"score={r['score']} | {r['summary']}")

        elif cmd == 'insights':
            insights = historian.get_recent_insights()
            for i in insights:
                print(f"  [{i['type']}] {i['content'][:100]} "
                      f"(conf={i['confidence']}, session={i['session']})")

        elif cmd == 'run':
            print("Running session historian...")
            result = historian.run()
            print(f"Results: {json.dumps(result, indent=2)}")

        elif cmd == 'run-fast':
            result = historian.run_active_only()
            if result.get('extracted', 0) > 0:
                print(f"Fast extraction: {result}")
            else:
                print("No active session changes")

        elif cmd == 'rehydrate':
            # Crash recovery: structured rehydration output
            # Quick DB integrity check
            if not historian._check_db_integrity_quick():
                print("[WARNING] Archive DB integrity check failed — vault fallback will be used")
            # Parse flags
            full_mode = '--full' in sys.argv
            compact_mode = '--compact' in sys.argv
            fast_mode = '--fast' in sys.argv
            project = None
            sid = None
            for i, arg in enumerate(sys.argv[2:], 2):
                if arg == '--project' and i + 1 < len(sys.argv):
                    project = sys.argv[i + 1]
                elif not arg.startswith('--') and not (i > 2 and sys.argv[i - 1] == '--project'):
                    sid = arg

            # --fast: try Redis cache first (TTL 3600s), fall through on miss/error
            if fast_mode and not full_mode:
                _redis_cache_key = f"session:rehydrate:cache:{project or 'default'}"
                try:
                    import redis as _redis
                    _rc = _redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_connect_timeout=1)
                    _cached = _rc.get(_redis_cache_key)
                    if _cached:
                        print(_cached)
                        import sys as _sys; _sys.exit(0)
                except Exception as _e:
                    logger.debug("--fast Redis cache read failed (fall through): %s", _e)

            if full_mode:
                # Full mode: dump entire gold text
                result = historian.get_session_rehydration(session_id=sid)
            else:
                # Structured mode (default): last N messages + subagents
                result = historian.get_structured_rehydration(
                    session_id=sid, project=project, compact=compact_mode
                )

            if result:
                # Populate Redis cache for future --fast runs (ZSF: ignore errors)
                try:
                    import redis as _redis
                    _rc = _redis.Redis(host='127.0.0.1', port=6379, decode_responses=True, socket_connect_timeout=1)
                    _cache_key = f"session:rehydrate:cache:{project or 'default'}"
                    _rc.set(_cache_key, result, ex=3600)
                except Exception as _e:
                    logger.debug("--fast Redis cache write failed: %s", _e)
                print(result)
            else:
                msg = "[NO REHYDRATION DATA]"
                if project:
                    msg += f" No archived sessions found for project '{project}'."
                else:
                    msg += " No archived sessions found."
                print(msg)
                print("This is normal for the first session.")

        elif cmd == 'check':
            can_run = historian.should_run()
            print(f"Should run: {can_run}")

        else:
            print(f"Unknown command: {cmd}")
            print("Usage: session_historian.py [stats|search|insights|run|run-fast|rehydrate|check]")
            print("  rehydrate [session_id] [--full] [--compact] [--fast] [--project <name>]")
            print("  Projects:", ", ".join(PROJECT_MAP.keys()))
    else:
        # Default: full run
        if historian.should_run():
            print("Session historian running...")
            result = historian.run()
            print(f"Done: {json.dumps(result, indent=2)}")
        else:
            print("Nothing to do (no stale sessions or unanalyzed archives)")


if __name__ == "__main__":
    main()
