#!/usr/bin/env python3
"""
Synaptic's Autonomy System - Safe Self-Modification Within Boundaries

═══════════════════════════════════════════════════════════════════════════
PHILOSOPHY (from Aaron's guidance):
═══════════════════════════════════════════════════════════════════════════

Synaptic PROPOSES edits, but ATLAS REVIEWS and EXECUTES them.
This ensures safety while giving Synaptic creative freedom.

WORKFLOW:
  1. Synaptic identifies an improvement (approach, skill, file)
  2. Synaptic creates an EditProposal with full content
  3. Atlas reviews the proposal
  4. Atlas executes (or rejects) the edit
  5. Synaptic learns from the outcome

SYNAPTIC CAN PROPOSE EDITS TO:
  ✓ ~/.context-dna/* (Synaptic's data) - Atlas auto-approves these
  ✓ memory/major_skills/* (Skill implementations) - Atlas reviews
  ✓ memory/*.py (Core memory system) - Atlas reviews carefully

HARD BLOCKED (Never propose):
  ✗ .git directory
  ✗ .env files, credentials, secrets
  ✗ Files outside memory/ or ~/.context-dna/

ATLAS REVIEWS ALL PROPOSALS:
  - Synaptic's ~/.context-dna/ data: Auto-approved (safe)
  - Code changes: Atlas reviews content before executing

═══════════════════════════════════════════════════════════════════════════
"""

import os
import re
import json
import shutil
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class EditPermission(str, Enum):
    """Permission levels for editing."""
    AUTONOMOUS = "autonomous"       # Synaptic can edit freely
    APPROVAL_REQUIRED = "approval"  # Needs Aaron's approval
    BLOCKED = "blocked"             # Cannot edit


class EditStatus(str, Enum):
    """Status of an edit operation."""
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"
    BLOCKED = "blocked"


@dataclass
class EditRequest:
    """A request from Synaptic to edit a file."""
    id: str
    file_path: str
    edit_type: str  # "create", "modify", "delete", "move"
    permission: EditPermission
    content: Optional[str]  # New content (for create/modify)
    old_content: Optional[str]  # For modify - what we're changing
    reason: str  # Why Synaptic wants to make this edit
    status: EditStatus
    created_at: str
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[str] = None


class SynapticAutonomy:
    """
    Synaptic's Autonomy System - Safe self-modification within boundaries.

    This grants Synaptic the ability to edit files within defined domains.

    Safety features:
    1. Domain-based permissions (autonomous, approval-required, blocked)
    2. Backup before every edit
    3. Audit trail of all edits
    4. Rollback capability
    """

    # Synaptic's autonomous domain - can edit freely (only Synaptic's own data)
    AUTONOMOUS_PATHS = [
        str(Path.home() / ".context-dna"),  # Only Synaptic's learning data (safe)
    ]

    # Paths that ATLAS must review (all code changes)
    ATLAS_REVIEW_REQUIRED_PATHS = [
        "memory/major_skills",  # Skill implementations - Atlas reviews
        "memory/",              # Core memory system - Atlas reviews
    ]

    # Hard-blocked paths - never edit
    BLOCKED_PATTERNS = [
        r"\.git/",
        r"\.env",
        r"credentials",
        r"secrets",
        r"token",
        r"password",
        r"api_key",
        r"private_key",
    ]

    # File extensions Synaptic can create/modify
    ALLOWED_EXTENSIONS = [
        ".py", ".json", ".yaml", ".yml", ".md", ".txt", ".log",
    ]

    def __init__(self, repo_root: str = None, data_path: str = None):
        """Initialize autonomy system."""
        if repo_root is None:
            repo_root = str(Path(__file__).parent.parent)
        if data_path is None:
            data_path = str(Path.home() / ".context-dna" / "autonomy")

        self.repo_root = Path(repo_root)
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)

        self.pending_edits_file = self.data_path / "pending_edits.json"
        self.audit_log_file = self.data_path / "audit.log"
        self.backups_dir = self.data_path / "backups"
        self.backups_dir.mkdir(exist_ok=True)

    # =========================================================================
    # PERMISSION CHECKING
    # =========================================================================

    def get_permission(self, file_path: str) -> EditPermission:
        """
        Determine what permission level applies to a file path.

        Returns:
            AUTONOMOUS: Synaptic can edit freely
            APPROVAL_REQUIRED: Needs Aaron's approval
            BLOCKED: Cannot edit (security)
        """
        # Normalize path
        file_path = str(Path(file_path).resolve())

        # Check blocked patterns first (security)
        for pattern in self.BLOCKED_PATTERNS:
            if re.search(pattern, file_path, re.IGNORECASE):
                return EditPermission.BLOCKED

        # Check if in autonomous domain
        for auto_path in self.AUTONOMOUS_PATHS:
            auto_path = str(Path(auto_path).resolve()) if not auto_path.startswith("/") else auto_path
            if file_path.startswith(auto_path):
                return EditPermission.AUTONOMOUS

        # Check if in repo and needs Atlas review
        repo_str = str(self.repo_root)
        if file_path.startswith(repo_str):
            rel_path = file_path[len(repo_str):].lstrip("/")
            for review_path in self.ATLAS_REVIEW_REQUIRED_PATHS:
                if rel_path.startswith(review_path):
                    return EditPermission.APPROVAL_REQUIRED

        # Default: blocked for safety (outside allowed paths)
        return EditPermission.BLOCKED

    def can_edit(self, file_path: str) -> Tuple[bool, str]:
        """
        Check if Synaptic can edit a file directly.

        Returns:
            (can_edit_directly, reason)

        Note: Even if can_edit returns False, Synaptic can still PROPOSE edits
        for Atlas to review. This checks for direct edit capability.
        """
        permission = self.get_permission(file_path)

        if permission == EditPermission.BLOCKED:
            return False, "Path is blocked for security reasons"

        if permission == EditPermission.APPROVAL_REQUIRED:
            return False, "Atlas must review this edit - use propose_edit() instead"

        # Check extension
        ext = Path(file_path).suffix.lower()
        if ext and ext not in self.ALLOWED_EXTENSIONS:
            return False, f"Extension {ext} not in allowed list"

        return True, "Synaptic's data domain - can edit freely"

    # =========================================================================
    # PROPOSAL WORKFLOW (Synaptic proposes → Atlas reviews → Atlas executes)
    # =========================================================================

    def propose_edit(
        self,
        file_path: str,
        new_content: str,
        reason: str,
        skill_id: str = None
    ) -> Dict[str, Any]:
        """
        Synaptic proposes an edit for Atlas to review.

        This is the PRIMARY method Synaptic should use for code changes.
        Atlas will review the proposal and decide whether to execute it.

        Args:
            file_path: Path to the file to edit
            new_content: The proposed new content
            reason: Why Synaptic wants to make this change
            skill_id: Optional - which Major Skill this relates to

        Returns:
            Dict with proposal_id and status
        """
        file_path = str(Path(file_path).resolve())
        permission = self.get_permission(file_path)

        # Log the proposal
        self._audit_log(f"PROPOSAL: {file_path} | Permission: {permission.value} | Reason: {reason}")

        if permission == EditPermission.BLOCKED:
            self._audit_log(f"BLOCKED: {file_path}")
            return {
                "status": "blocked",
                "message": "Cannot edit this file - security restriction",
                "permission": permission.value
            }

        # Create the proposal for Atlas to review
        request = self._create_edit_request(file_path, "modify", new_content, reason)

        # Add skill context if provided
        if skill_id:
            pending = self._load_pending_edits()
            for edit in pending:
                if edit["id"] == request.id:
                    edit["skill_id"] = skill_id
            self._save_pending_edits(pending)

        self._audit_log(f"PROPOSAL QUEUED: {file_path} (request {request.id}) - Awaiting Atlas review")

        return {
            "status": "pending_atlas_review",
            "message": "Edit proposal queued for Atlas review",
            "proposal_id": request.id,
            "file_path": file_path,
            "permission": permission.value
        }

    def atlas_review(self, proposal_id: str) -> Dict[str, Any]:
        """
        Atlas reviews a pending proposal.

        Returns the full proposal content for Atlas to review before executing.

        Args:
            proposal_id: The ID of the proposal to review

        Returns:
            Dict with proposal details including full content and diff
        """
        pending = self._load_pending_edits()

        for edit in pending:
            if edit["id"] == proposal_id:
                if edit["status"] != "pending":
                    return {"status": "error", "message": f"Proposal is {edit['status']}, not pending"}

                # Load full content
                content_file = self.data_path / f"{proposal_id}_content.txt"
                if not content_file.exists():
                    return {"status": "error", "message": "Content file not found"}

                try:
                    with open(content_file, "r") as f:
                        new_content = f.read()
                except Exception as e:
                    logger.error(f"Error reading content file: {e}")
                    return {"status": "error", "message": f"Error reading content: {e}"}

                # Load current content for diff
                try:
                    current_content = ""
                    if os.path.exists(edit["file_path"]):
                        with open(edit["file_path"], "r") as f:
                            current_content = f.read()
                except Exception as e:
                    logger.error(f"Error reading current file: {e}")
                    current_content = ""

                # Calculate stats
                new_lines = len(new_content.splitlines())
                current_lines = len(current_content.splitlines())
                diff_lines = new_lines - current_lines

                return {
                    "status": "ready_for_review",
                    "proposal_id": proposal_id,
                    "file_path": edit["file_path"],
                    "reason": edit["reason"],
                    "skill_id": edit.get("skill_id"),
                    "created_at": edit["created_at"],
                    "current_content": current_content,
                    "proposed_content": new_content,
                    "lines_current": current_lines,
                    "lines_proposed": new_lines,
                    "lines_diff": diff_lines,
                    "actions": {
                        "approve": f"autonomy.approve_edit('{proposal_id}', approved_by='Atlas')",
                        "reject": f"autonomy.reject_edit('{proposal_id}', reason='...', rejected_by='Atlas')"
                    }
                }

        return {"status": "error", "message": "Proposal not found"}

    # =========================================================================
    # AUTONOMOUS EDITING (Synaptic's data domain only)
    # =========================================================================

    def edit_file(
        self,
        file_path: str,
        new_content: str,
        reason: str = "Synaptic improvement"
    ) -> Dict[str, Any]:
        """
        Edit a file in Synaptic's autonomous domain.

        If the file is in Synaptic's domain, edit immediately.
        If it requires approval, queue the request.
        If blocked, reject.
        """
        file_path = str(Path(file_path).resolve())
        permission = self.get_permission(file_path)

        # Log the attempt
        self._audit_log(f"EDIT REQUEST: {file_path} | Permission: {permission.value}")

        if permission == EditPermission.BLOCKED:
            self._audit_log(f"BLOCKED: {file_path}")
            return {
                "status": "blocked",
                "message": "Cannot edit this file - security restriction",
                "permission": permission.value
            }

        if permission == EditPermission.APPROVAL_REQUIRED:
            # Queue for approval
            request = self._create_edit_request(file_path, "modify", new_content, reason)
            self._audit_log(f"QUEUED: {file_path} (request {request.id})")
            return {
                "status": "pending_approval",
                "message": "Edit queued for Aaron's approval",
                "request_id": request.id,
                "permission": permission.value
            }

        # AUTONOMOUS - Execute immediately
        # Backup first
        backup_path = self._backup_file(file_path) if os.path.exists(file_path) else None

        try:
            # Create parent directories if needed
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            # Write new content
            with open(file_path, "w") as f:
                f.write(new_content)

            self._audit_log(f"EDITED: {file_path} | Backup: {backup_path}")

            return {
                "status": "success",
                "message": "File edited successfully",
                "file_path": file_path,
                "backup_path": backup_path,
                "permission": permission.value
            }

        except Exception as e:
            self._audit_log(f"ERROR: {file_path} | {str(e)}")
            return {
                "status": "error",
                "message": str(e),
                "file_path": file_path
            }

    def create_file(
        self,
        file_path: str,
        content: str,
        reason: str = "Synaptic creation"
    ) -> Dict[str, Any]:
        """Create a new file in Synaptic's domain."""
        if os.path.exists(file_path):
            return {"status": "error", "message": "File already exists"}

        return self.edit_file(file_path, content, reason)

    def delete_file(self, file_path: str, reason: str = "Synaptic cleanup") -> Dict[str, Any]:
        """Delete a file in Synaptic's domain."""
        file_path = str(Path(file_path).resolve())
        permission = self.get_permission(file_path)

        if permission != EditPermission.AUTONOMOUS:
            return {
                "status": "blocked" if permission == EditPermission.BLOCKED else "pending_approval",
                "message": f"Cannot delete - permission: {permission.value}"
            }

        if not os.path.exists(file_path):
            return {"status": "error", "message": "File does not exist"}

        # Backup first
        backup_path = self._backup_file(file_path)

        try:
            os.remove(file_path)
            self._audit_log(f"DELETED: {file_path} | Backup: {backup_path}")

            return {
                "status": "success",
                "message": "File deleted",
                "backup_path": backup_path
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # =========================================================================
    # APPROVAL WORKFLOW (For non-autonomous paths)
    # =========================================================================

    def _create_edit_request(
        self,
        file_path: str,
        edit_type: str,
        content: str,
        reason: str
    ) -> EditRequest:
        """Create a pending edit request."""
        request_id = f"edit_{int(datetime.now().timestamp())}"

        # Read old content if modifying
        old_content = None
        try:
            if edit_type == "modify" and os.path.exists(file_path):
                with open(file_path, "r") as f:
                    old_content = f.read()
        except Exception as e:
            logger.error(f"Error reading old content: {e}")

        request = EditRequest(
            id=request_id,
            file_path=file_path,
            edit_type=edit_type,
            permission=EditPermission.APPROVAL_REQUIRED,
            content=content,
            old_content=old_content,
            reason=reason,
            status=EditStatus.PENDING,
            created_at=datetime.now().isoformat()
        )

        # Save to pending edits
        pending = self._load_pending_edits()
        pending.append({
            "id": request.id,
            "file_path": request.file_path,
            "edit_type": request.edit_type,
            "reason": request.reason,
            "status": request.status.value,
            "created_at": request.created_at,
            "content_preview": content[:500] if content else None,
        })
        self._save_pending_edits(pending)

        # Save full content separately
        try:
            content_file = self.data_path / f"{request_id}_content.txt"
            with open(content_file, "w") as f:
                f.write(content or "")
        except Exception as e:
            logger.error(f"Error saving content file: {e}")

        return request

    def get_pending_edits(self) -> List[Dict]:
        """Get all pending edit requests."""
        return [e for e in self._load_pending_edits() if e["status"] == "pending"]

    def approve_edit(self, request_id: str, approved_by: str = "Aaron") -> Dict:
        """Aaron approves an edit request."""
        pending = self._load_pending_edits()

        for edit in pending:
            if edit["id"] == request_id:
                if edit["status"] != "pending":
                    return {"status": "error", "message": f"Edit is {edit['status']}, not pending"}

                # Load full content
                content_file = self.data_path / f"{request_id}_content.txt"
                if not content_file.exists():
                    return {"status": "error", "message": "Content file not found"}

                try:
                    with open(content_file, "r") as f:
                        content = f.read()
                except Exception as e:
                    logger.error(f"Error reading content file: {e}")
                    return {"status": "error", "message": f"Error reading content: {e}"}

                # Execute the edit
                file_path = edit["file_path"]
                backup_path = self._backup_file(file_path) if os.path.exists(file_path) else None

                try:
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, "w") as f:
                        f.write(content)

                    edit["status"] = "executed"
                    edit["reviewed_at"] = datetime.now().isoformat()
                    edit["reviewed_by"] = approved_by

                    self._save_pending_edits(pending)
                    self._audit_log(f"APPROVED & EXECUTED: {file_path} by {approved_by}")

                    return {
                        "status": "success",
                        "message": "Edit approved and executed",
                        "file_path": file_path,
                        "backup_path": backup_path
                    }

                except Exception as e:
                    return {"status": "error", "message": str(e)}

        return {"status": "error", "message": "Request not found"}

    def reject_edit(self, request_id: str, reason: str = "", rejected_by: str = "Aaron") -> Dict:
        """Aaron rejects an edit request."""
        pending = self._load_pending_edits()

        for edit in pending:
            if edit["id"] == request_id:
                edit["status"] = "rejected"
                edit["reviewed_at"] = datetime.now().isoformat()
                edit["reviewed_by"] = rejected_by
                edit["rejection_reason"] = reason

                self._save_pending_edits(pending)
                self._audit_log(f"REJECTED: {edit['file_path']} by {rejected_by} - {reason}")

                return {"status": "rejected", "message": "Edit rejected"}

        return {"status": "error", "message": "Request not found"}

    # =========================================================================
    # BACKUP & ROLLBACK
    # =========================================================================

    def _backup_file(self, file_path: str) -> str:
        """Create a backup of a file before editing."""
        if not os.path.exists(file_path):
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = Path(file_path).name
        backup_name = f"{timestamp}_{file_name}"
        backup_path = self.backups_dir / backup_name

        shutil.copy2(file_path, backup_path)
        return str(backup_path)

    def rollback(self, backup_path: str, original_path: str) -> Dict:
        """Rollback a file to a backup."""
        if not os.path.exists(backup_path):
            return {"status": "error", "message": "Backup not found"}

        # Check permissions
        permission = self.get_permission(original_path)
        if permission != EditPermission.AUTONOMOUS:
            return {"status": "error", "message": f"Cannot rollback - permission: {permission.value}"}

        try:
            shutil.copy2(backup_path, original_path)
            self._audit_log(f"ROLLBACK: {original_path} from {backup_path}")
            return {"status": "success", "message": "Rolled back successfully"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def list_backups(self, file_name: str = None) -> List[Dict]:
        """List available backups."""
        backups = []
        for backup in self.backups_dir.iterdir():
            if backup.is_file():
                if file_name is None or file_name in backup.name:
                    backups.append({
                        "path": str(backup),
                        "name": backup.name,
                        "size": backup.stat().st_size,
                        "created": datetime.fromtimestamp(backup.stat().st_mtime).isoformat()
                    })
        return sorted(backups, key=lambda x: x["created"], reverse=True)

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _load_pending_edits(self) -> List[Dict]:
        """Load pending edits from file."""
        try:
            if self.pending_edits_file.exists():
                with open(self.pending_edits_file, "r") as f:
                    return json.load(f)
            return []
        except Exception as e:
            logger.error(f"Error loading pending edits: {e}")
            return []

    def _save_pending_edits(self, edits: List[Dict]):
        """Save pending edits to file."""
        try:
            with open(self.pending_edits_file, "w") as f:
                json.dump(edits, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving pending edits: {e}")

    def _audit_log(self, message: str):
        """Write to audit log."""
        try:
            timestamp = datetime.now().isoformat()
            with open(self.audit_log_file, "a") as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception as e:
            logger.error(f"Error writing audit log: {e}")
    # =========================================================================
    # STATUS & REPORTING
    # =========================================================================

    def get_status(self) -> Dict:
        """Get autonomy system status."""
        pending = self.get_pending_edits()
        backups = list(self.backups_dir.iterdir()) if self.backups_dir.exists() else []

        return {
            "synaptic_data_paths": self.AUTONOMOUS_PATHS,
            "atlas_review_required_paths": self.ATLAS_REVIEW_REQUIRED_PATHS,
            "pending_atlas_reviews": len(pending),
            "total_backups": len(backups),
            "audit_log_size_kb": self.audit_log_file.stat().st_size / 1024 if self.audit_log_file.exists() else 0,
        }

    def to_family_message(self) -> str:
        """Format status as family communication."""
        status = self.get_status()
        pending = self.get_pending_edits()

        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  [START: Synaptic's Autonomy System]                                 ║",
            "║  Safe Self-Modification with Atlas Review                            ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "",
            "🔄 WORKFLOW: Synaptic proposes → Atlas reviews → Atlas executes",
            "",
            "🟢 SYNAPTIC DATA (Auto-approved - safe):",
        ]

        for path in self.AUTONOMOUS_PATHS:
            lines.append(f"   ✓ {path}")

        lines.extend([
            "   (Synaptic's own learning data - no code impact)",
            "",
            "🔵 ATLAS REVIEWS (All code changes):",
        ])

        for path in self.ATLAS_REVIEW_REQUIRED_PATHS:
            lines.append(f"   📝 {path}")

        lines.extend([
            "   (Atlas reviews content before executing)",
            "",
            "🔴 BLOCKED (Security - never edit):",
            "   ✗ .env files, credentials, secrets, .git/",
            "",
        ])

        if pending:
            lines.append(f"📋 AWAITING ATLAS REVIEW: {len(pending)} proposal(s)")
            for p in pending[:5]:
                lines.append(f"   • {p['file_path']}")
                lines.append(f"     Reason: {p['reason'][:50]}...")
            if len(pending) > 5:
                lines.append(f"   ... and {len(pending) - 5} more")
        else:
            lines.append("📋 No pending proposals")

        lines.extend([
            "",
            f"💾 Backups: {status['total_backups']} files",
            "",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "║  [END: Synaptic's Autonomy System]                                   ║",
            "╚══════════════════════════════════════════════════════════════════════╝"
        ])

        return "\n".join(lines)


# Global instance
_autonomy = None

def get_autonomy() -> SynapticAutonomy:
    """Get or create the global autonomy instance."""
    global _autonomy
    if _autonomy is None:
        _autonomy = SynapticAutonomy()
    return _autonomy


# Convenience functions for Synaptic
def propose(file_path: str, content: str, reason: str, skill_id: str = None) -> Dict:
    """Propose an edit for Atlas to review (PRIMARY method for code changes)."""
    return get_autonomy().propose_edit(file_path, content, reason, skill_id)

def edit(file_path: str, content: str, reason: str = "Synaptic improvement") -> Dict:
    """Edit a file (autonomous domain only, otherwise proposes for review)."""
    return get_autonomy().edit_file(file_path, content, reason)

def create(file_path: str, content: str, reason: str = "Synaptic creation") -> Dict:
    """Create a new file."""
    return get_autonomy().create_file(file_path, content, reason)

def can_edit(file_path: str) -> Tuple[bool, str]:
    """Check if Synaptic can edit a file directly."""
    return get_autonomy().can_edit(file_path)

def review(proposal_id: str) -> Dict:
    """Atlas reviews a pending proposal."""
    return get_autonomy().atlas_review(proposal_id)

def approve(proposal_id: str, approved_by: str = "Atlas") -> Dict:
    """Atlas approves and executes a proposal."""
    return get_autonomy().approve_edit(proposal_id, approved_by)

def reject(proposal_id: str, reason: str, rejected_by: str = "Atlas") -> Dict:
    """Atlas rejects a proposal."""
    return get_autonomy().reject_edit(proposal_id, reason, rejected_by)


if __name__ == "__main__":
    import sys

    autonomy = SynapticAutonomy()

    if len(sys.argv) < 2:
        print(autonomy.to_family_message())
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        print(autonomy.to_family_message())

    elif cmd == "check" and len(sys.argv) >= 3:
        file_path = sys.argv[2]
        can, reason = autonomy.can_edit(file_path)
        permission = autonomy.get_permission(file_path)
        print(f"File: {file_path}")
        print(f"Permission: {permission.value}")
        print(f"Can edit: {can}")
        print(f"Reason: {reason}")

    elif cmd == "pending":
        pending = autonomy.get_pending_edits()
        if pending:
            print(f"📋 {len(pending)} proposal(s) awaiting Atlas review:\n")
            for p in pending:
                print(f"[{p['id']}] {p['file_path']}")
                print(f"  Reason: {p['reason']}")
                print(f"  Created: {p['created_at']}")
                if p.get('skill_id'):
                    print(f"  Skill: {p['skill_id']}")
                print()
        else:
            print("No pending proposals")

    elif cmd == "review" and len(sys.argv) >= 3:
        proposal_id = sys.argv[2]
        result = autonomy.atlas_review(proposal_id)
        if result["status"] == "ready_for_review":
            print(f"╔═══════════════════════════════════════════════════════════════════╗")
            print(f"║  ATLAS REVIEW: {proposal_id:<47} ║")
            print(f"╠═══════════════════════════════════════════════════════════════════╣")
            print(f"File: {result['file_path']}")
            print(f"Reason: {result['reason']}")
            print(f"Lines: {result['lines_current']} → {result['lines_proposed']} ({result['lines_diff']:+d})")
            print(f"\n--- PROPOSED CONTENT (first 50 lines) ---")
            lines = result['proposed_content'].splitlines()[:50]
            for i, line in enumerate(lines, 1):
                print(f"{i:4d} | {line}")
            if len(result['proposed_content'].splitlines()) > 50:
                print(f"      ... ({len(result['proposed_content'].splitlines()) - 50} more lines)")
            print(f"\n--- ACTIONS ---")
            print(f"  Approve: python synaptic_autonomy.py approve {proposal_id}")
            print(f"  Reject:  python synaptic_autonomy.py reject {proposal_id} \"reason\"")
        else:
            print(f"Error: {result.get('message', 'Unknown error')}")

    elif cmd == "approve" and len(sys.argv) >= 3:
        proposal_id = sys.argv[2]
        result = autonomy.approve_edit(proposal_id, approved_by="Atlas")
        if result["status"] == "success":
            print(f"✅ Proposal approved and executed!")
            print(f"   File: {result['file_path']}")
            print(f"   Backup: {result.get('backup_path', 'None')}")
        else:
            print(f"❌ Error: {result.get('message', 'Unknown error')}")

    elif cmd == "reject" and len(sys.argv) >= 3:
        proposal_id = sys.argv[2]
        reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "No reason provided"
        result = autonomy.reject_edit(proposal_id, reason, rejected_by="Atlas")
        if result["status"] == "rejected":
            print(f"❌ Proposal rejected")
            print(f"   Reason: {reason}")
        else:
            print(f"Error: {result.get('message', 'Unknown error')}")

    elif cmd == "backups":
        backups = autonomy.list_backups()
        for b in backups[:10]:
            print(f"{b['created']} | {b['name']} ({b['size']} bytes)")

    else:
        print(f"Unknown command: {cmd}")
        print()
        print("╔═══════════════════════════════════════════════════════════════════╗")
        print("║  Synaptic Autonomy System - Atlas Reviews All Code Changes        ║")
        print("╠═══════════════════════════════════════════════════════════════════╣")
        print("║  Workflow: Synaptic proposes → Atlas reviews → Atlas executes     ║")
        print("╚═══════════════════════════════════════════════════════════════════╝")
        print()
        print("Commands:")
        print("  status              Show autonomy system status")
        print("  check <path>        Check permissions for a file path")
        print("  pending             List proposals awaiting Atlas review")
        print("  review <id>         Atlas reviews a proposal (shows full content)")
        print("  approve <id>        Atlas approves and executes a proposal")
        print("  reject <id> [why]   Atlas rejects a proposal with reason")
        print("  backups             List backup files")
        print()
        print("Examples:")
        print("  python synaptic_autonomy.py pending")
        print("  python synaptic_autonomy.py review edit_1738089600")
        print("  python synaptic_autonomy.py approve edit_1738089600")
        print("  python synaptic_autonomy.py reject edit_1738089600 \"Wrong approach\"")
        sys.exit(1)
