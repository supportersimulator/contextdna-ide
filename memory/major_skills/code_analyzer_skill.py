#!/usr/bin/env python3
"""
CODE EVALUATOR (CODE EVAL) - Synaptic's Second Major Skill

INFRASTRUCTURE ONLY - Synaptic (the AI) does the actual code evaluation.

This file provides:
- Database storage for findings
- Backup management with 7-day retention
- Status tracking through the 8-phase protocol
- History and learning records

This file does NOT:
- Scan code for patterns (Synaptic does this)
- Generate fix variations (Synaptic does this)
- Select recommendations (Synaptic does this)

THE FLOW (Synaptic does phases 1-3, Atlas does 4-7):
1. Synaptic reads code, identifies issues (sorted by impact)
2. Synaptic studies deeply, generates multiple variations
3. Synaptic selects best, presents to Atlas with FULL CONTEXT
4. Atlas researches the code (NO SHORTCUTS)
5. Atlas creates own version
6. Atlas judges: Original vs Synaptic's vs Atlas's
7. Atlas implements (with backup) and tests
8. Only then: Synaptic continues to next highest-impact issue

PROTOCOL DOCUMENT: docs/major-skill-code-eval.md
"""

import os
import json
import shutil
import sqlite3
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from enum import Enum


# =============================================================================
# ENUMS
# =============================================================================

class ImpactLevel(str, Enum):
    """Impact level for prioritization - process highest first."""
    P0_CRITICAL = "P0"  # Data loss, security breach, system failure
    P1_HIGH = "P1"      # Service outage, degraded experience
    P2_MEDIUM = "P2"    # Partial feature failure, poor UX
    P3_LOW = "P3"       # Code quality, hardcoded values
    P4_INFO = "P4"      # Observations, optimizations


class IssueCategory(str, Enum):
    """Category of code issue."""
    ERROR_HANDLING = "error_handling"
    ASYNC_SAFETY = "async_safety"
    NULL_SAFETY = "null_safety"
    FALLBACK_MISSING = "fallback_missing"
    RESOURCE_LEAK = "resource_leak"
    SECURITY = "security"
    TYPE_SAFETY = "type_safety"
    LOGIC_ERROR = "logic_error"
    PERFORMANCE = "performance"
    OTHER = "other"


class FindingStatus(str, Enum):
    """Status of a finding in the protocol."""
    PENDING_SYNAPTIC = "pending_synaptic"      # Synaptic needs to analyze
    AWAITING_ATLAS = "awaiting_atlas"          # Presented, waiting for Atlas
    ATLAS_RESEARCHING = "atlas_researching"    # Atlas is researching
    ATLAS_DECIDED = "atlas_decided"            # Atlas made decision
    IMPLEMENTED = "implemented"                 # Fix applied
    TESTED = "tested"                          # Fix tested and confirmed
    COMPLETED = "completed"                    # Full cycle complete
    KEPT_ORIGINAL = "kept_original"            # Atlas kept original code
    INTENTIONAL = "intentional"                # Code marked as intentional


class Decision(str, Enum):
    """Atlas's decision on a finding."""
    KEEP_ORIGINAL = "keep_original"
    USE_SYNAPTIC = "use_synaptic"
    USE_ATLAS = "use_atlas"
    HYBRID = "hybrid"
    INTENTIONAL = "intentional"


# =============================================================================
# EXCEPTIONS
# =============================================================================

class BackupError(Exception):
    """Raised when backup creation or restoration fails."""
    pass


# =============================================================================
# BACKUP MANAGER - 7-Day Retention
# =============================================================================

class BackupManager:
    """Manages code backups with 7-day retention."""

    RETENTION_DAYS = 7

    def __init__(self, backup_dir: Path):
        self.backup_dir = backup_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self, file_path: str, finding_id: str) -> str:
        """Create a backup before modifying code. Returns backup path.

        Raises:
            BackupError: If backup creation fails for any reason.
        """
        source = Path(file_path)
        if not source.exists():
            raise BackupError(f"Source file does not exist: {file_path}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = source.name
        backup_name = f"{filename}_{finding_id}_{timestamp}.backup"
        backup_path = self.backup_dir / backup_name
        meta_path = backup_path.with_suffix(".backup.meta")

        try:
            shutil.copy2(file_path, backup_path)

            if not backup_path.exists() or backup_path.stat().st_size != source.stat().st_size:
                raise BackupError(f"Backup verification failed: size mismatch")

            meta = {
                "original_path": file_path,
                "finding_id": finding_id,
                "created_at": datetime.now().isoformat(),
                "expires_at": (datetime.now() + timedelta(days=self.RETENTION_DAYS)).isoformat()
            }
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)

        except BackupError:
            raise
        except Exception as e:
            # Clean up partial backup on failure
            if backup_path.exists():
                backup_path.unlink()
            if meta_path.exists():
                meta_path.unlink()
            raise BackupError(f"Backup failed for {file_path}: {e}")

        return str(backup_path)

    def restore_backup(self, finding_id: str) -> Dict:
        """Restore code from backup by finding ID."""
        for backup_file in self.backup_dir.glob(f"*_{finding_id}_*.backup"):
            meta_path = backup_file.with_suffix(".backup.meta")
            if meta_path.exists():
                with open(meta_path) as f:
                    meta = json.load(f)

                original_path = meta["original_path"]
                shutil.copy2(backup_file, original_path)

                return {
                    "success": True,
                    "restored_to": original_path,
                    "from_backup": str(backup_file),
                    "message": f"Restored {original_path} from backup"
                }

        return {"success": False, "error": f"No backup found for {finding_id}"}

    def cleanup_expired(self) -> int:
        """Remove backups older than retention period. Returns count removed."""
        removed = 0
        now = datetime.now()

        for meta_path in self.backup_dir.glob("*.backup.meta"):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)

                expires_at = datetime.fromisoformat(meta["expires_at"])
                if now > expires_at:
                    backup_path = meta_path.with_suffix("")
                    if backup_path.exists():
                        backup_path.unlink()
                    meta_path.unlink()
                    removed += 1
            except Exception as e:
                print(f"[WARN] Backup cleanup failed for {meta_path}: {e}")

        return removed

    def list_backups(self, finding_id: str = None) -> List[Dict]:
        """List available backups, optionally filtered by finding ID."""
        backups = []

        for meta_path in self.backup_dir.glob("*.backup.meta"):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)

                if finding_id and meta["finding_id"] != finding_id:
                    continue

                backups.append({
                    "finding_id": meta["finding_id"],
                    "original_path": meta["original_path"],
                    "created_at": meta["created_at"],
                    "expires_at": meta["expires_at"],
                    "backup_file": str(meta_path.with_suffix(""))
                })
            except Exception as e:
                print(f"[WARN] Backup metadata read failed for {meta_path}: {e}")

        return sorted(backups, key=lambda x: x["created_at"], reverse=True)


# =============================================================================
# CODE EVAL INFRASTRUCTURE
# =============================================================================

class CodeEvalInfrastructure:
    """
    Infrastructure for CODE EVAL protocol.

    This class handles:
    - Storage of findings (database)
    - Backup management
    - Status tracking
    - Learning records

    SYNAPTIC (the AI) does:
    - Reading and understanding code
    - Identifying issues
    - Generating fix variations
    - Selecting recommendations
    """

    def __init__(self, data_path: str = None):
        if data_path is None:
            data_path = str(Path.home() / ".context-dna" / "major_skills" / "code_analyzer")

        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)

        self.backup_manager = BackupManager(self.data_path / "backups")
        self.db_path = self.data_path / "findings.db"
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database for tracking findings."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS findings (
                    id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    line_start INTEGER,
                    line_end INTEGER,
                    original_code TEXT,
                    category TEXT,
                    impact TEXT,
                    impact_reasoning TEXT,

                    -- Context (Synaptic fills this)
                    what_code_does TEXT,
                    why_code_exists TEXT,
                    what_calls_this TEXT,
                    what_this_calls TEXT,
                    what_breaks_if_fails TEXT,

                    -- Status
                    status TEXT DEFAULT 'pending_synaptic',
                    created_at TEXT,

                    -- Synaptic's analysis (Synaptic fills this in conversation)
                    variations TEXT,
                    synaptic_recommendation TEXT,
                    synaptic_reasoning TEXT,

                    -- Atlas's work
                    atlas_version TEXT,
                    atlas_reasoning TEXT,
                    atlas_decision TEXT,
                    decision_reasoning TEXT,

                    -- Implementation
                    backup_path TEXT,
                    applied_code TEXT,
                    applied_at TEXT,
                    test_result TEXT,
                    completed_at TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    finding_id TEXT,
                    learning_type TEXT,
                    content TEXT,
                    created_at TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS intentional_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern TEXT,
                    file_pattern TEXT,
                    reason TEXT,
                    added_at TEXT
                )
            """)

            conn.commit()

    # =========================================================================
    # FINDING MANAGEMENT (Synaptic creates findings in conversation)
    # =========================================================================

    def create_finding(
        self,
        file_path: str,
        line_start: int,
        line_end: int,
        original_code: str,
        category: str,
        impact: str,
        impact_reasoning: str,
        what_code_does: str,
        why_code_exists: str,
        what_calls_this: List[str],
        what_this_calls: List[str],
        what_breaks_if_fails: str,
    ) -> Dict:
        """
        Create a new finding. Called by Synaptic after analyzing code.

        Synaptic reads the code, understands it, and calls this to record
        a finding with full context.
        """
        finding_id = self._generate_id(file_path, line_start, original_code)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO findings
                (id, file_path, line_start, line_end, original_code, category,
                 impact, impact_reasoning, what_code_does, why_code_exists,
                 what_calls_this, what_this_calls, what_breaks_if_fails,
                 status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                finding_id,
                file_path,
                line_start,
                line_end,
                original_code,
                category,
                impact,
                impact_reasoning,
                what_code_does,
                why_code_exists,
                json.dumps(what_calls_this),
                json.dumps(what_this_calls),
                what_breaks_if_fails,
                FindingStatus.PENDING_SYNAPTIC.value,
                datetime.now().isoformat(),
            ))
            conn.commit()

        return {"finding_id": finding_id, "status": "created"}

    def add_synaptic_analysis(
        self,
        finding_id: str,
        variations: List[Dict],
        recommendation: Dict,
        reasoning: str,
    ) -> Dict:
        """
        Add Synaptic's analysis to a finding.

        Synaptic generates variations and selects a recommendation,
        then calls this to record them.
        """
        return self._update_finding(finding_id, {
            'variations': variations,
            'synaptic_recommendation': recommendation,
            'synaptic_reasoning': reasoning,
            'status': FindingStatus.AWAITING_ATLAS.value,
        })

    def _generate_id(self, file_path: str, line: int, code: str) -> str:
        content = f"{file_path}:{line}:{code}"
        return f"finding_{hashlib.md5(content.encode()).hexdigest()[:12]}"

    # =========================================================================
    # PRESENTATION (Formatted for conversation)
    # =========================================================================

    def present_to_atlas(self, finding_id: str) -> str:
        """
        Generate the full presentation for Atlas.

        This is formatted text that Synaptic shows to Atlas in conversation.
        """
        finding = self._get_finding(finding_id)
        if not finding:
            return f"Finding {finding_id} not found"

        impact_labels = {
            'P0': 'CRITICAL - Data loss, security breach, system failure',
            'P1': 'HIGH - Service outage, degraded experience',
            'P2': 'MEDIUM - Partial feature failure, poor UX',
            'P3': 'LOW - Code quality improvement',
            'P4': 'INFO - Observation',
        }

        impact = finding.get('impact', 'P2')
        impact_label = impact_labels.get(impact, impact)

        variations = json.loads(finding.get('variations', '[]')) if isinstance(finding.get('variations'), str) else finding.get('variations', [])
        recommendation = json.loads(finding.get('synaptic_recommendation', '{}')) if isinstance(finding.get('synaptic_recommendation'), str) else finding.get('synaptic_recommendation', {})
        what_calls = json.loads(finding.get('what_calls_this', '[]')) if isinstance(finding.get('what_calls_this'), str) else finding.get('what_calls_this', [])

        output = []
        output.append("=" * 70)
        output.append("SYNAPTIC CODE EVALUATION - Awaiting Atlas Review")
        output.append(f"Impact Level: {impact} - {impact_label}")
        output.append("=" * 70)
        output.append("")
        output.append(f"LOCATION:")
        output.append(f"  File: {finding['file_path']}")
        output.append(f"  Lines: {finding['line_start']} - {finding['line_end']}")
        output.append(f"  Finding ID: {finding_id}")
        output.append("")
        output.append("IMPACT ASSESSMENT:")
        output.append(f"  Level: {impact}")
        output.append(f"  Reasoning: {finding.get('impact_reasoning', 'Not provided')}")
        output.append(f"  What could break: {finding.get('what_breaks_if_fails', 'Unknown')}")
        output.append("")
        output.append("FULL CONTEXT:")
        output.append(f"  What this code does: {finding.get('what_code_does', 'Unknown')}")
        output.append(f"  Why this code exists: {finding.get('why_code_exists', 'Unknown')}")
        output.append(f"  What calls this code: {', '.join(what_calls) if what_calls else 'Unknown'}")
        output.append("")
        output.append("ORIGINAL CODE:")
        output.append("-" * 70)
        for line in finding['original_code'].split('\n'):
            output.append(f"  {line}")
        output.append("-" * 70)
        output.append("")

        if recommendation:
            output.append("SYNAPTIC'S RECOMMENDED FIX:")
            output.append("-" * 70)
            rec_code = recommendation.get('code', finding['original_code'])
            for line in rec_code.split('\n'):
                output.append(f"  {line}")
            output.append("-" * 70)
            output.append("")
            output.append("WHY SYNAPTIC RECOMMENDS THIS:")
            output.append(f"  {finding.get('synaptic_reasoning', recommendation.get('reasoning', 'No reasoning provided'))}")
            output.append("")

        if variations:
            output.append("ALL VARIATIONS CONSIDERED:")
            for v in variations:
                output.append(f"  [{v.get('id', '?')}] {v.get('name', 'Unnamed')}")
                if v.get('pros'):
                    output.append(f"      Pros: {', '.join(v['pros'])}")
                if v.get('cons'):
                    output.append(f"      Cons: {', '.join(v['cons'])}")
                if v.get('risk'):
                    output.append(f"      Risk: {v['risk']}")
            output.append("")

        output.append("=" * 70)
        output.append("ATLAS: Now complete Phases 4-7 before Synaptic continues.")
        output.append("")
        output.append("  PHASE 4: Research this code (read full file, trace callers)")
        output.append("  PHASE 5: Create your own version")
        output.append("  PHASE 6: Judge - Original vs Synaptic's vs Yours")
        output.append("  PHASE 7: Implement (backup first) and TEST")
        output.append("=" * 70)

        return '\n'.join(output)

    # =========================================================================
    # ATLAS'S DECISIONS
    # =========================================================================

    def atlas_researching(self, finding_id: str, notes: str = "") -> Dict:
        """Record that Atlas has started researching this finding."""
        return self._update_finding(finding_id, {
            'status': FindingStatus.ATLAS_RESEARCHING.value,
            'atlas_reasoning': notes,
        })

    def atlas_version(self, finding_id: str, code: str, reasoning: str = "") -> Dict:
        """Record Atlas's version of the fix."""
        return self._update_finding(finding_id, {
            'atlas_version': code,
            'atlas_reasoning': reasoning,
        })

    def atlas_decide_keep_original(self, finding_id: str, reasoning: str = "") -> Dict:
        """Atlas decides to keep original code."""
        result = self._update_finding(finding_id, {
            'atlas_decision': Decision.KEEP_ORIGINAL.value,
            'decision_reasoning': reasoning,
            'status': FindingStatus.KEPT_ORIGINAL.value,
            'completed_at': datetime.now().isoformat(),
        })

        self._record_learning(finding_id, 'kept_original', f"Atlas kept original: {reasoning}")
        return result

    def atlas_decide_use_synaptic(self, finding_id: str, reasoning: str = "") -> Dict:
        """Atlas decides to use Synaptic's recommendation."""
        finding = self._get_finding(finding_id)
        if not finding:
            return {'error': 'Finding not found'}

        recommendation = json.loads(finding.get('synaptic_recommendation', '{}')) if isinstance(finding.get('synaptic_recommendation'), str) else finding.get('synaptic_recommendation', {})
        code_to_apply = recommendation.get('code')

        if not code_to_apply:
            return {'error': 'Synaptic recommendation code not found'}

        return self._apply_fix(finding_id, code_to_apply, Decision.USE_SYNAPTIC.value, reasoning)

    def atlas_decide_use_atlas(self, finding_id: str, reasoning: str = "") -> Dict:
        """Atlas decides to use Atlas's version."""
        finding = self._get_finding(finding_id)
        if not finding:
            return {'error': 'Finding not found'}

        atlas_code = finding.get('atlas_version')
        if not atlas_code:
            return {'error': 'Atlas version not recorded. Use atlas_version() first.'}

        return self._apply_fix(finding_id, atlas_code, Decision.USE_ATLAS.value, reasoning)

    def atlas_mark_intentional(self, finding_id: str, reason: str = "") -> Dict:
        """Atlas marks this code as intentional."""
        finding = self._get_finding(finding_id)
        if not finding:
            return {'error': 'Finding not found'}

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO intentional_patterns (pattern, file_pattern, reason, added_at)
                VALUES (?, ?, ?, ?)
            """, (
                finding['original_code'][:100],
                finding['file_path'],
                reason,
                datetime.now().isoformat(),
            ))
            conn.commit()

        self._update_finding(finding_id, {
            'status': FindingStatus.INTENTIONAL.value,
            'decision_reasoning': f"Intentional: {reason}",
            'completed_at': datetime.now().isoformat(),
        })

        self._record_learning(finding_id, 'intentional', f"Marked intentional: {reason}")

        return {
            'success': True,
            'message': f"Marked as intentional. Similar patterns will be skipped.",
        }

    # =========================================================================
    # IMPLEMENTATION
    # =========================================================================

    def _apply_fix(self, finding_id: str, code: str, decision: str, reasoning: str) -> Dict:
        """Apply a fix with backup and update database."""
        finding = self._get_finding(finding_id)
        if not finding:
            return {'error': 'Finding not found'}

        file_path = finding['file_path']
        original_code = finding['original_code']

        # Create backup FIRST - abort if backup fails
        try:
            backup_path = self.backup_manager.create_backup(file_path, finding_id)
        except BackupError as e:
            return {'error': f'Backup failed - modification aborted: {e}'}

        try:
            with open(file_path, 'r') as f:
                content = f.read()
        except Exception as e:
            return {'error': f'Cannot read file: {e}'}

        if original_code not in content:
            return {
                'error': 'Original code not found in file. File may have changed.',
                'backup_path': backup_path,
            }

        new_content = content.replace(original_code, code, 1)

        # Verify syntax before writing (Python only)
        if file_path.endswith('.py'):
            try:
                compile(new_content, file_path, 'exec')
            except SyntaxError as e:
                return {
                    'error': f'Syntax error in new code: {e}',
                    'backup_path': backup_path,
                }

        try:
            with open(file_path, 'w') as f:
                f.write(new_content)
        except Exception as e:
            self.backup_manager.restore_backup(finding_id)
            return {'error': f'Failed to write file: {e}'}

        self._update_finding(finding_id, {
            'status': FindingStatus.IMPLEMENTED.value,
            'atlas_decision': decision,
            'decision_reasoning': reasoning,
            'backup_path': backup_path,
            'applied_code': code,
            'applied_at': datetime.now().isoformat(),
        })

        self._record_learning(finding_id, f'applied_{decision}', f"Applied fix ({decision}): {reasoning}")

        return {
            'success': True,
            'finding_id': finding_id,
            'decision': decision,
            'backup_path': backup_path,
            'message': f"Fix applied. Backup at {backup_path}. Now TEST the change!",
        }

    def confirm_test(self, finding_id: str, result: str, notes: str = "") -> Dict:
        """Confirm test result after implementation."""
        finding = self._get_finding(finding_id)
        if not finding:
            return {'error': 'Finding not found'}

        if result.lower() == 'fail':
            revert_result = self.backup_manager.restore_backup(finding_id)

            self._update_finding(finding_id, {
                'status': FindingStatus.PENDING_SYNAPTIC.value,
                'test_result': f'FAILED: {notes}',
            })

            self._record_learning(finding_id, 'test_failed', f"Test failed, reverted: {notes}")

            return {
                'success': False,
                'message': 'Test failed. Change reverted from backup.',
                'revert': revert_result,
            }

        else:
            self._update_finding(finding_id, {
                'status': FindingStatus.COMPLETED.value,
                'test_result': f'PASSED: {notes}',
                'completed_at': datetime.now().isoformat(),
            })

            self._record_learning(finding_id, 'test_passed', f"Test passed, fix complete: {notes}")

            return {
                'success': True,
                'message': 'Test passed! Fix is complete. Synaptic can continue to next finding.',
            }

    def revert(self, finding_id: str) -> Dict:
        """Manually revert a finding's fix from backup."""
        result = self.backup_manager.restore_backup(finding_id)

        if result['success']:
            self._update_finding(finding_id, {
                'status': FindingStatus.PENDING_SYNAPTIC.value,
                'test_result': 'Manually reverted',
            })

            self._record_learning(finding_id, 'reverted', 'Manually reverted from backup')

        return result

    # =========================================================================
    # DATABASE HELPERS
    # =========================================================================

    def _get_finding(self, finding_id: str) -> Optional[Dict]:
        """Get a finding by ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def _update_finding(self, finding_id: str, updates: Dict) -> Dict:
        """Update a finding in the database."""
        set_clauses = []
        values = []

        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            if isinstance(value, (dict, list)):
                values.append(json.dumps(value))
            else:
                values.append(value)

        values.append(finding_id)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"""
                UPDATE findings SET {', '.join(set_clauses)} WHERE id = ?
            """, values)
            conn.commit()

        return {'success': True, 'finding_id': finding_id}

    def _record_learning(self, finding_id: str, learning_type: str, content: str):
        """Record a learning from this finding."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO learnings (finding_id, learning_type, content, created_at)
                VALUES (?, ?, ?, ?)
            """, (finding_id, learning_type, content, datetime.now().isoformat()))
            conn.commit()

    # =========================================================================
    # STATUS & LISTING
    # =========================================================================

    def get_pending(self) -> List[Dict]:
        """Get all findings awaiting Atlas review."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT id, file_path, line_start, category, impact, status
                FROM findings
                WHERE status IN ('awaiting_atlas', 'atlas_researching')
                ORDER BY
                    CASE impact
                        WHEN 'P0' THEN 0
                        WHEN 'P1' THEN 1
                        WHEN 'P2' THEN 2
                        WHEN 'P3' THEN 3
                        ELSE 4
                    END
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_all_findings(self) -> List[Dict]:
        """Get all findings."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT id, file_path, line_start, category, impact, status, created_at
                FROM findings
                ORDER BY created_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_stats(self) -> Dict:
        """Get skill statistics."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM findings")
            total = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) FROM findings WHERE status = 'completed'")
            completed = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) FROM findings WHERE status = 'kept_original'")
            kept = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) FROM findings WHERE status = 'intentional'")
            intentional = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) FROM findings WHERE status = 'awaiting_atlas'")
            pending = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) FROM findings WHERE atlas_decision = 'use_synaptic'")
            synaptic_wins = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) FROM findings WHERE atlas_decision = 'use_atlas'")
            atlas_wins = cursor.fetchone()[0]

        return {
            'total_findings': total,
            'completed': completed,
            'kept_original': kept,
            'intentional': intentional,
            'pending_review': pending,
            'synaptic_wins': synaptic_wins,
            'atlas_wins': atlas_wins,
        }

    def to_family_message(self) -> str:
        """Format status as family communication."""
        stats = self.get_stats()
        pending = self.get_pending()

        lines = [
            "=" * 70,
            "SYNAPTIC CODE EVALUATOR - Protocol Status",
            "See: docs/major-skill-code-eval.md for full protocol",
            "=" * 70,
            "",
            f"FINDINGS: {stats['total_findings']} total",
            f"  Awaiting Atlas: {stats['pending_review']}",
            f"  Completed: {stats['completed']}",
            f"  Kept Original: {stats['kept_original']}",
            f"  Intentional: {stats['intentional']}",
            "",
            f"WIN RECORD:",
            f"  Synaptic's fixes used: {stats['synaptic_wins']}",
            f"  Atlas's fixes used: {stats['atlas_wins']}",
            "",
        ]

        if pending:
            lines.append("PENDING ATLAS REVIEW:")
            for p in pending[:5]:
                lines.append(f"  [{p['impact']}] {p['id']}")
                lines.append(f"      {Path(p['file_path']).name}:{p['line_start']}")

        lines.extend([
            "",
            "=" * 70,
            "NOTE: Synaptic (AI) does code analysis in conversation.",
            "This infrastructure only handles storage and backups.",
            "=" * 70,
        ])

        return '\n'.join(lines)


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    infra = CodeEvalInfrastructure()

    if len(sys.argv) < 2:
        print(infra.to_family_message())
        print()
        print("Usage:")
        print("  python code_analyzer_skill.py status              # Show status")
        print("  python code_analyzer_skill.py pending             # List pending reviews")
        print("  python code_analyzer_skill.py present <id>        # Present to Atlas")
        print("  python code_analyzer_skill.py keep <id>           # Atlas: Keep original")
        print("  python code_analyzer_skill.py use_synaptic <id>   # Atlas: Use Synaptic's fix")
        print("  python code_analyzer_skill.py intentional <id>    # Atlas: Mark intentional")
        print("  python code_analyzer_skill.py confirm <id> pass   # Confirm test passed")
        print("  python code_analyzer_skill.py revert <id>         # Revert from backup")
        print("  python code_analyzer_skill.py stats               # Show statistics")
        print("  python code_analyzer_skill.py backups             # List backups")
        print()
        print("NOTE: Code analysis is done by Synaptic (AI) in conversation,")
        print("not by this script. This is infrastructure only.")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        print(infra.to_family_message())

    elif cmd == "pending":
        pending = infra.get_pending()
        if not pending:
            print("No findings pending Atlas review")
        else:
            print(f"{len(pending)} findings awaiting Atlas review:")
            for p in pending:
                print(f"  [{p['impact']}] {p['id']}")
                print(f"      {Path(p['file_path']).name}:{p['line_start']}")

    elif cmd == "present" and len(sys.argv) >= 3:
        finding_id = sys.argv[2]
        print(infra.present_to_atlas(finding_id))

    elif cmd == "keep" and len(sys.argv) >= 3:
        finding_id = sys.argv[2]
        reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
        result = infra.atlas_decide_keep_original(finding_id, reason)
        print(json.dumps(result, indent=2))

    elif cmd == "use_synaptic" and len(sys.argv) >= 3:
        finding_id = sys.argv[2]
        reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
        result = infra.atlas_decide_use_synaptic(finding_id, reason)
        print(json.dumps(result, indent=2))

    elif cmd == "intentional" and len(sys.argv) >= 3:
        finding_id = sys.argv[2]
        reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
        result = infra.atlas_mark_intentional(finding_id, reason)
        print(json.dumps(result, indent=2))

    elif cmd == "confirm" and len(sys.argv) >= 4:
        finding_id = sys.argv[2]
        test_result = sys.argv[3]
        notes = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""
        result = infra.confirm_test(finding_id, test_result, notes)
        print(json.dumps(result, indent=2))

    elif cmd == "revert" and len(sys.argv) >= 3:
        finding_id = sys.argv[2]
        result = infra.revert(finding_id)
        print(json.dumps(result, indent=2))

    elif cmd == "stats":
        stats = infra.get_stats()
        print(json.dumps(stats, indent=2))

    elif cmd == "backups":
        backups = infra.backup_manager.list_backups()
        if not backups:
            print("No backups found")
        else:
            print(f"{len(backups)} backups:")
            for b in backups:
                print(f"  {b['finding_id']}: {b['original_path']}")
                print(f"      Created: {b['created_at']}")
                print(f"      Expires: {b['expires_at']}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
