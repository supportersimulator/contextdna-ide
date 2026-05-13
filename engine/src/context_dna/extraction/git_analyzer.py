"""Git Analyzer for Context DNA.

Analyzes git commits to extract learnings automatically.
Detects patterns, fixes, and significant changes.
"""

import subprocess
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any


@dataclass
class GitCommit:
    """Represents a git commit."""

    hash: str
    short_hash: str
    author: str
    date: datetime
    subject: str
    body: str
    files_changed: List[str]
    insertions: int
    deletions: int


@dataclass
class CommitAnalysis:
    """Analysis result for a commit."""

    commit: GitCommit
    is_fix: bool
    is_feature: bool
    is_refactor: bool
    is_docs: bool
    is_test: bool
    is_infrastructure: bool
    affected_areas: List[str]
    learning_type: str  # 'win', 'fix', 'pattern'
    suggested_tags: List[str]
    significance: float  # 0-1


class GitAnalyzer:
    """Analyzes git history for automatic learning extraction.

    Features:
    - Detects commit types (fix, feature, refactor, etc.)
    - Identifies affected code areas
    - Suggests tags based on content
    - Filters significant commits worth recording
    """

    # Conventional commit patterns
    COMMIT_PATTERNS = {
        "fix": r"^fix(\(.+\))?:",
        "feat": r"^feat(\(.+\))?:",
        "refactor": r"^refactor(\(.+\))?:",
        "docs": r"^docs(\(.+\))?:",
        "test": r"^test(\(.+\))?:",
        "chore": r"^chore(\(.+\))?:",
        "perf": r"^perf(\(.+\))?:",
        "ci": r"^ci(\(.+\))?:",
    }

    # Infrastructure file patterns
    INFRA_PATTERNS = [
        r"docker",
        r"terraform",
        r"\.tf$",
        r"kubernetes",
        r"k8s",
        r"helm",
        r"ansible",
        r"aws",
        r"gcp",
        r"azure",
        r"nginx",
        r"Dockerfile",
        r"docker-compose",
    ]

    # Keywords that indicate significant learnings
    LEARNING_KEYWORDS = [
        "fixed",
        "resolved",
        "solved",
        "workaround",
        "bug",
        "issue",
        "problem",
        "error",
        "critical",
        "important",
        "gotcha",
        "note",
        "remember",
        "lesson",
        "learned",
        "discovery",
        "optimization",
        "performance",
        "security",
    ]

    def __init__(self, repo_path: Optional[str] = None):
        """Initialize git analyzer.

        Args:
            repo_path: Path to git repository (defaults to current directory)
        """
        self._repo_path = Path(repo_path or ".").resolve()

    def get_recent_commits(
        self,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[GitCommit]:
        """Get recent commits.

        Args:
            limit: Maximum number of commits
            since: Only commits since this date (e.g., "1 week ago")

        Returns:
            List of GitCommit objects
        """
        cmd = [
            "git",
            "log",
            f"-{limit}",
            "--pretty=format:%H|%h|%an|%ai|%s|%b<<<END>>>",
            "--numstat",
        ]

        if since:
            cmd.append(f"--since={since}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self._repo_path,
                timeout=30,
            )

            if result.returncode != 0:
                return []

            return self._parse_git_log(result.stdout)

        except subprocess.TimeoutExpired:
            return []
        except Exception:
            return []

    def analyze_commit(self, commit: GitCommit) -> CommitAnalysis:
        """Analyze a single commit for learning potential.

        Args:
            commit: GitCommit to analyze

        Returns:
            CommitAnalysis with extracted information
        """
        subject_lower = commit.subject.lower()
        full_message = f"{commit.subject}\n{commit.body}".lower()

        # Detect commit type
        is_fix = bool(re.match(self.COMMIT_PATTERNS["fix"], subject_lower, re.I)) or \
                 "fix" in subject_lower or "bug" in subject_lower
        is_feature = bool(re.match(self.COMMIT_PATTERNS["feat"], subject_lower, re.I)) or \
                     "add" in subject_lower or "implement" in subject_lower
        is_refactor = bool(re.match(self.COMMIT_PATTERNS["refactor"], subject_lower, re.I)) or \
                      "refactor" in subject_lower
        is_docs = bool(re.match(self.COMMIT_PATTERNS["docs"], subject_lower, re.I)) or \
                  "docs" in subject_lower or "readme" in subject_lower
        is_test = bool(re.match(self.COMMIT_PATTERNS["test"], subject_lower, re.I)) or \
                  "test" in subject_lower
        is_infrastructure = any(
            re.search(pattern, f, re.I)
            for pattern in self.INFRA_PATTERNS
            for f in commit.files_changed
        )

        # Determine affected areas
        affected_areas = self._detect_affected_areas(commit.files_changed)

        # Determine learning type
        if is_fix:
            learning_type = "fix"
        elif is_feature and (commit.insertions > 100 or is_infrastructure):
            learning_type = "win"
        elif is_refactor and commit.insertions > 50:
            learning_type = "pattern"
        else:
            learning_type = "win"

        # Generate suggested tags
        suggested_tags = self._generate_tags(commit, affected_areas)

        # Calculate significance
        significance = self._calculate_significance(
            commit, is_fix, is_feature, is_infrastructure
        )

        return CommitAnalysis(
            commit=commit,
            is_fix=is_fix,
            is_feature=is_feature,
            is_refactor=is_refactor,
            is_docs=is_docs,
            is_test=is_test,
            is_infrastructure=is_infrastructure,
            affected_areas=affected_areas,
            learning_type=learning_type,
            suggested_tags=suggested_tags,
            significance=significance,
        )

    def find_significant_commits(
        self,
        limit: int = 50,
        since: Optional[str] = None,
        min_significance: float = 0.5,
    ) -> List[CommitAnalysis]:
        """Find commits worth recording as learnings.

        Args:
            limit: Maximum commits to analyze
            since: Only commits since this date
            min_significance: Minimum significance threshold

        Returns:
            List of significant CommitAnalysis objects
        """
        commits = self.get_recent_commits(limit, since)
        analyses = [self.analyze_commit(c) for c in commits]

        return [
            a for a in analyses
            if a.significance >= min_significance
        ]

    def _parse_git_log(self, output: str) -> List[GitCommit]:
        """Parse git log output."""
        commits = []
        entries = output.split("<<<END>>>")

        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue

            lines = entry.split("\n")
            if not lines:
                continue

            # Parse header line
            header = lines[0]
            parts = header.split("|")
            if len(parts) < 5:
                continue

            hash_full = parts[0]
            hash_short = parts[1]
            author = parts[2]
            date_str = parts[3]
            subject = parts[4]
            body = parts[5] if len(parts) > 5 else ""

            # Parse date
            try:
                date = datetime.fromisoformat(date_str.replace(" ", "T").split("+")[0])
            except ValueError:
                date = datetime.now()

            # Parse file stats
            files_changed = []
            insertions = 0
            deletions = 0

            for line in lines[1:]:
                line = line.strip()
                if not line or line.startswith("<<<"):
                    continue

                stat_parts = line.split("\t")
                if len(stat_parts) >= 3:
                    try:
                        ins = int(stat_parts[0]) if stat_parts[0] != "-" else 0
                        dels = int(stat_parts[1]) if stat_parts[1] != "-" else 0
                        filename = stat_parts[2]
                        insertions += ins
                        deletions += dels
                        files_changed.append(filename)
                    except ValueError:
                        continue

            commits.append(GitCommit(
                hash=hash_full,
                short_hash=hash_short,
                author=author,
                date=date,
                subject=subject,
                body=body,
                files_changed=files_changed,
                insertions=insertions,
                deletions=deletions,
            ))

        return commits

    def _detect_affected_areas(self, files: List[str]) -> List[str]:
        """Detect affected code areas from file paths."""
        areas = set()

        for f in files:
            f_lower = f.lower()

            # Detect by directory/path patterns
            if "api" in f_lower or "endpoint" in f_lower:
                areas.add("api")
            if "docker" in f_lower or "compose" in f_lower:
                areas.add("docker")
            if "terraform" in f_lower or ".tf" in f_lower:
                areas.add("terraform")
            if "test" in f_lower or "spec" in f_lower:
                areas.add("testing")
            if "aws" in f_lower or "lambda" in f_lower or "s3" in f_lower:
                areas.add("aws")
            if "database" in f_lower or "migration" in f_lower or "model" in f_lower:
                areas.add("database")
            if "auth" in f_lower or "login" in f_lower:
                areas.add("authentication")
            if "config" in f_lower or "settings" in f_lower:
                areas.add("configuration")
            if "ci" in f_lower or "github/workflows" in f_lower:
                areas.add("ci-cd")

            # Detect by file extension
            if f.endswith(".py"):
                areas.add("python")
            elif f.endswith((".js", ".ts", ".tsx", ".jsx")):
                areas.add("javascript")
            elif f.endswith((".tf", ".tfvars")):
                areas.add("terraform")
            elif f.endswith((".yml", ".yaml")):
                areas.add("yaml")
            elif f.endswith(".sh"):
                areas.add("shell")

        return list(areas)

    def _generate_tags(
        self,
        commit: GitCommit,
        affected_areas: List[str],
    ) -> List[str]:
        """Generate suggested tags for a commit."""
        tags = list(affected_areas)  # Start with affected areas

        message = f"{commit.subject} {commit.body}".lower()

        # Add keyword-based tags
        keyword_tags = {
            "async": ["async", "asyncio", "await"],
            "performance": ["perf", "performance", "optimization", "speed", "fast"],
            "security": ["security", "auth", "permission", "vulnerability"],
            "bug": ["bug", "fix", "issue", "error"],
            "deployment": ["deploy", "release", "production"],
        }

        for tag, keywords in keyword_tags.items():
            if any(kw in message for kw in keywords):
                tags.append(tag)

        # Limit to 5 most relevant tags
        return list(set(tags))[:5]

    def _calculate_significance(
        self,
        commit: GitCommit,
        is_fix: bool,
        is_feature: bool,
        is_infrastructure: bool,
    ) -> float:
        """Calculate how significant a commit is for learning."""
        score = 0.3  # Base score

        # Fixes are always significant
        if is_fix:
            score += 0.3

        # Infrastructure changes are significant
        if is_infrastructure:
            score += 0.2

        # Larger changes are more significant (up to a point)
        total_changes = commit.insertions + commit.deletions
        if total_changes > 200:
            score += 0.2
        elif total_changes > 50:
            score += 0.1

        # Features with substantial body text are significant
        if is_feature and len(commit.body) > 100:
            score += 0.15

        # Check for learning keywords in message
        message = f"{commit.subject} {commit.body}".lower()
        keyword_matches = sum(1 for kw in self.LEARNING_KEYWORDS if kw in message)
        score += min(0.2, keyword_matches * 0.05)

        return min(1.0, score)
