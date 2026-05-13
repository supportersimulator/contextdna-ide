#!/usr/bin/env python3
"""
Context DNA Adaptive Hierarchy Analyzer

Deep codebase scanner that learns your project structure:
- Detects repo types (monorepo, submodules, polyrepo)
- Finds services (backend, frontend, infra)
- Learns naming conventions and config patterns
- Supports incremental scanning for large codebases

Usage:
    # CLI test mode
    python -m context_dna.setup.hierarchy_analyzer --test /path/to/repo

    # Python
    from context_dna.setup.hierarchy_analyzer import HierarchyAnalyzer
    profile = HierarchyAnalyzer('/path/to/repo').analyze()
"""

import os
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import Counter
from datetime import datetime
from dataclasses import dataclass, field

from context_dna.setup.models import (
    HierarchyProfile,
    RepoType,
    SubmoduleInfo,
    ServiceLocation,
    NamingConvention,
    ConfigPattern,
    PlatformInfo,
)


# =============================================================================
# PROJECT MARKERS - Used to detect standalone projects
# =============================================================================

# These indicate a directory is likely a standalone project
PROJECT_MARKERS = {
    # Package managers / build systems
    'package.json': {'lang': 'javascript', 'confidence': 0.9},
    'pyproject.toml': {'lang': 'python', 'confidence': 0.95},
    'requirements.txt': {'lang': 'python', 'confidence': 0.6},
    'setup.py': {'lang': 'python', 'confidence': 0.8},
    'Gemfile': {'lang': 'ruby', 'confidence': 0.9},
    'Cargo.toml': {'lang': 'rust', 'confidence': 0.95},
    'go.mod': {'lang': 'go', 'confidence': 0.95},
    'pom.xml': {'lang': 'java', 'confidence': 0.9},
    'build.gradle': {'lang': 'java', 'confidence': 0.9},
    'CMakeLists.txt': {'lang': 'cpp', 'confidence': 0.85},
    'Makefile': {'lang': 'make', 'confidence': 0.4},

    # Framework-specific
    'manage.py': {'lang': 'python', 'framework': 'django', 'confidence': 0.95},
    'next.config.js': {'lang': 'javascript', 'framework': 'nextjs', 'confidence': 0.95},
    'next.config.mjs': {'lang': 'javascript', 'framework': 'nextjs', 'confidence': 0.95},
    'nuxt.config.js': {'lang': 'javascript', 'framework': 'nuxt', 'confidence': 0.95},
    'angular.json': {'lang': 'javascript', 'framework': 'angular', 'confidence': 0.95},
    'vue.config.js': {'lang': 'javascript', 'framework': 'vue', 'confidence': 0.9},
    'svelte.config.js': {'lang': 'javascript', 'framework': 'svelte', 'confidence': 0.95},

    # Infrastructure
    'docker-compose.yml': {'lang': 'yaml', 'type': 'infra', 'confidence': 0.7},
    'docker-compose.yaml': {'lang': 'yaml', 'type': 'infra', 'confidence': 0.7},
    'Dockerfile': {'lang': 'docker', 'type': 'infra', 'confidence': 0.5},
    'main.tf': {'lang': 'terraform', 'type': 'infra', 'confidence': 0.9},
}


@dataclass
class DetectedProject:
    """A potentially standalone project detected in the codebase."""
    path: str
    name: str
    language: Optional[str] = None
    framework: Optional[str] = None
    markers_found: List[str] = field(default_factory=list)
    confidence: float = 0.0
    is_submodule: bool = False
    is_root: bool = False
    needs_clarification: bool = False
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            'path': self.path,
            'name': self.name,
            'language': self.language,
            'framework': self.framework,
            'markers_found': self.markers_found,
            'confidence': self.confidence,
            'is_submodule': self.is_submodule,
            'is_root': self.is_root,
            'needs_clarification': self.needs_clarification,
            'description': self.description,
        }


# =============================================================================
# CONSTANTS
# =============================================================================

# Directories to skip during scanning
SKIP_DIRS = {
    '.git', '.svn', '.hg',
    'node_modules', '__pycache__', '.pytest_cache',
    'venv', '.venv', 'env', '.env',
    'dist', 'build', 'target',
    '.next', '.nuxt', '.output',
    'coverage', '.coverage',
    '.idea', '.vscode',
}

# Service detection patterns
# 'strong_markers' = ANY ONE of these is definitive (100% confidence)
# 'weak_markers' = multiple needed, or presence adds confidence
SERVICE_PATTERNS = {
    'backend': {
        'dirs': ['backend', 'server', 'api', 'src/api', 'core'],
        'strong_markers': ['manage.py', 'wsgi.py', 'asgi.py'],  # Any = Django
        'weak_markers': ['app.py', 'main.py', 'server.py', 'requirements.txt'],
        'frameworks': {
            'django': ['manage.py', 'settings.py', 'wsgi.py', 'asgi.py'],
            'flask': ['app.py', 'flask'],
            'fastapi': ['main.py', 'fastapi'],
            'express': ['app.js', 'server.js', 'express'],
            'nestjs': ['nest-cli.json', 'main.ts'],
            'rails': ['Gemfile', 'config.ru', 'Rakefile'],
            'spring': ['pom.xml', 'build.gradle', 'Application.java'],
        }
    },
    'frontend': {
        'dirs': ['frontend', 'client', 'web', 'ui', 'sim-frontend'],
        'strong_markers': ['next.config.js', 'next.config.mjs', 'angular.json', 'vue.config.js'],
        'weak_markers': ['package.json', 'index.html', 'tsconfig.json'],
        'frameworks': {
            'react': ['react', 'jsx', 'tsx'],
            'nextjs': ['next.config.js', 'next.config.mjs', 'pages/', 'app/'],
            'vue': ['vue.config.js', 'nuxt.config.js', '.vue'],
            'angular': ['angular.json', 'ng'],
            'svelte': ['svelte.config.js', '.svelte'],
        }
    },
    'infra': {
        'dirs': ['infra', 'infrastructure', 'terraform', 'k8s', 'deploy', 'ops'],
        'strong_markers': ['main.tf', 'terraform.tfvars', 'docker-compose.yml', 'docker-compose.yaml'],
        'weak_markers': ['Dockerfile', 'variables.tf', 'outputs.tf'],
        'frameworks': {
            'terraform': ['main.tf', 'variables.tf', '.tf'],
            'kubernetes': ['deployment.yaml', 'service.yaml', 'kustomization.yaml'],
            'docker': ['Dockerfile', 'docker-compose.yml', 'docker-compose.yaml'],
            'pulumi': ['Pulumi.yaml', '__main__.py'],
            'cdk': ['cdk.json', 'cdk.out'],
        }
    },
    'memory': {
        'dirs': ['memory', '.memory', 'brain'],
        'strong_markers': ['brain.py', 'query.py', 'context.py'],
        'weak_markers': ['learning', '.db', 'memory.py'],
    },
    'context_dna': {
        'dirs': ['context-dna', '.context-dna'],
        'strong_markers': ['brain.py', 'pyproject.toml'],
        'weak_markers': ['src/', 'docs/', 'local_llm/'],
    },
    'scripts': {
        'dirs': ['scripts', 'bin', 'tools'],
        'strong_markers': [],
        'weak_markers': ['.sh', '.py', '.js'],
    },
    'docs': {
        'dirs': ['docs', 'documentation', 'wiki'],
        'strong_markers': [],
        'weak_markers': ['README.md', 'CONTRIBUTING.md', '.md'],
    },
    'voice': {
        'dirs': ['ersim-voice-stack', 'voice', 'audio', 'speech'],
        'strong_markers': ['docker-compose.yml'],
        'weak_markers': ['services/', 'agents/', 'stt/', 'tts/'],
    },
}

# Config file patterns
CONFIG_PATTERNS = {
    'env': ['.env', '.env.local', '.env.development', '.env.production'],
    'yaml': ['config.yaml', 'config.yml', 'settings.yaml', 'settings.yml'],
    'json': ['config.json', 'settings.json', 'tsconfig.json', 'package.json'],
    'toml': ['pyproject.toml', 'Cargo.toml', 'config.toml'],
    'python': ['settings.py', 'config.py', 'conf.py'],
}


# =============================================================================
# HIERARCHY ANALYZER
# =============================================================================

class HierarchyAnalyzer:
    """
    Scans codebase to learn structure and patterns.

    Design principles:
    - Non-destructive: Only reads, never modifies
    - Progressive: Can do incremental scans
    - Graceful: Handles edge cases without crashing
    - Transparent: Logs uncertainties for user review
    """

    def __init__(self, root_path: str | Path, max_depth: int = 10):
        """
        Initialize analyzer.

        Args:
            root_path: Root directory to analyze
            max_depth: Maximum directory depth to scan
        """
        self.root = Path(root_path).resolve()
        self.max_depth = max_depth
        self._file_cache: Dict[str, str] = {}
        self._scan_stats = {
            'dirs_scanned': 0,
            'files_checked': 0,
            'time_started': None,
            'time_completed': None,
        }

    def analyze(self, incremental: bool = False) -> HierarchyProfile:
        """
        Full codebase analysis.

        Args:
            incremental: If True, only scan changed directories

        Returns:
            Complete HierarchyProfile
        """
        self._scan_stats['time_started'] = datetime.utcnow()

        profile = HierarchyProfile(root_path=str(self.root))

        # Platform detection
        profile.platform = PlatformInfo.detect()

        # Repository type detection
        profile.repo_type = self._detect_repo_type()

        # Submodule detection
        if profile.repo_type == RepoType.SUBMODULE_MONOREPO:
            profile.submodules = self._parse_gitmodules()

        # Service location detection
        profile.locations = self._find_services()

        # Naming convention detection
        profile.naming_conventions = self._detect_naming_conventions()

        # Config pattern detection
        profile.config_patterns = self._detect_config_patterns()

        self._scan_stats['time_completed'] = datetime.utcnow()

        return profile

    def detect_all_projects(self, max_depth: int = 2) -> List[DetectedProject]:
        """
        Detect ALL potential standalone projects in the directory.

        This is the CORE VALUE of the adaptive system - being extra sensitive
        to detecting multiple different projects/programs.

        Args:
            max_depth: How deep to scan for projects

        Returns:
            List of all detected projects (may need clarification)
        """
        projects = []
        submodule_paths = set()

        # First, identify submodules (they're definitely separate projects)
        gitmodules = self.root / '.gitmodules'
        if gitmodules.exists():
            for sm in self._parse_gitmodules():
                submodule_paths.add(sm.path)
                projects.append(DetectedProject(
                    path=sm.path,
                    name=sm.path.split('/')[-1],
                    confidence=1.0,
                    is_submodule=True,
                    markers_found=['.gitmodules entry'],
                    description=f"Git submodule → {sm.url}",
                ))

        # Check the root directory
        root_project = self._check_directory_for_project(self.root, is_root=True)
        if root_project:
            projects.append(root_project)

        # Scan top-level directories
        for item in self.root.iterdir():
            if not item.is_dir():
                continue

            # Skip hidden, skip known non-project dirs
            if item.name.startswith('.'):
                continue
            if item.name in SKIP_DIRS:
                continue

            # Skip submodules (already handled)
            if item.name in submodule_paths:
                continue

            # Check this directory for project markers
            project = self._check_directory_for_project(item)
            if project:
                projects.append(project)
            elif max_depth > 1:
                # Check one level deeper (e.g., packages/app1, services/api)
                self._scan_subdirectories_for_projects(item, projects, max_depth - 1)

        # Sort by confidence and name
        projects.sort(key=lambda p: (-p.confidence, p.name))

        # Mark projects that need clarification
        self._mark_clarification_needed(projects)

        return projects

    def _check_directory_for_project(
        self,
        dir_path: Path,
        is_root: bool = False
    ) -> Optional[DetectedProject]:
        """Check if a directory looks like a standalone project."""
        markers_found = []
        best_confidence = 0.0
        language = None
        framework = None

        for marker, info in PROJECT_MARKERS.items():
            marker_path = dir_path / marker
            if marker_path.exists():
                markers_found.append(marker)
                conf = info.get('confidence', 0.5)
                if conf > best_confidence:
                    best_confidence = conf
                    language = info.get('lang')
                    framework = info.get('framework')

        # If we found markers, this is likely a project
        if markers_found and best_confidence >= 0.4:
            name = dir_path.name if not is_root else dir_path.name + " (root)"
            path = '.' if is_root else dir_path.name

            return DetectedProject(
                path=path,
                name=name,
                language=language,
                framework=framework,
                markers_found=markers_found,
                confidence=best_confidence,
                is_root=is_root,
                description=self._generate_project_description(markers_found, framework),
            )

        return None

    def _scan_subdirectories_for_projects(
        self,
        parent: Path,
        projects: List[DetectedProject],
        depth: int
    ):
        """Recursively scan subdirectories for projects."""
        try:
            for item in parent.iterdir():
                if not item.is_dir():
                    continue
                if item.name.startswith('.') or item.name in SKIP_DIRS:
                    continue

                project = self._check_directory_for_project(item)
                if project:
                    # Adjust path to be relative to root
                    project.path = str(item.relative_to(self.root))
                    projects.append(project)
                elif depth > 1:
                    self._scan_subdirectories_for_projects(item, projects, depth - 1)
        except PermissionError as e:
            print(f"[WARN] Permission denied scanning directory: {e}")

    def _generate_project_description(
        self,
        markers: List[str],
        framework: Optional[str]
    ) -> str:
        """Generate a human-readable description of the project."""
        if framework:
            framework_names = {
                'django': 'Django web application',
                'nextjs': 'Next.js React app',
                'nuxt': 'Nuxt.js Vue app',
                'angular': 'Angular application',
                'vue': 'Vue.js application',
                'svelte': 'SvelteKit application',
            }
            return framework_names.get(framework, f'{framework} project')

        if 'package.json' in markers:
            if 'next.config.js' in markers or 'next.config.mjs' in markers:
                return 'Next.js React app'
            return 'Node.js/JavaScript project'
        if 'pyproject.toml' in markers or 'setup.py' in markers:
            return 'Python project'
        if 'requirements.txt' in markers:
            return 'Python project (requirements)'
        if 'Cargo.toml' in markers:
            return 'Rust project'
        if 'go.mod' in markers:
            return 'Go project'
        if 'Gemfile' in markers:
            return 'Ruby project'
        if 'docker-compose.yml' in markers or 'docker-compose.yaml' in markers:
            return 'Docker infrastructure'
        if 'main.tf' in markers:
            return 'Terraform infrastructure'

        return 'Project (type unclear)'

    def _mark_clarification_needed(self, projects: List[DetectedProject]):
        """Mark projects that need user clarification."""
        # If we have many projects, they probably need clarification
        if len(projects) > 3:
            for p in projects:
                if not p.is_submodule and p.confidence < 0.9:
                    p.needs_clarification = True

        # If there are overlapping or unclear boundaries
        paths = [p.path for p in projects]
        for p in projects:
            # Check for nested projects (needs clarification)
            for other_path in paths:
                if other_path != p.path and other_path.startswith(p.path + '/'):
                    p.needs_clarification = True
                    break

            # Low confidence needs clarification
            if p.confidence < 0.7 and not p.is_submodule:
                p.needs_clarification = True

    # -------------------------------------------------------------------------
    # Repository Type Detection
    # -------------------------------------------------------------------------

    def _detect_repo_type(self) -> RepoType:
        """Detect the type of repository structure."""
        # Check for git submodules
        gitmodules = self.root / '.gitmodules'
        if gitmodules.exists():
            return RepoType.SUBMODULE_MONOREPO

        # Check for monorepo tools
        if (self.root / 'nx.json').exists():
            return RepoType.NX_MONOREPO

        if (self.root / 'turbo.json').exists():
            return RepoType.TURBO_MONOREPO

        if (self.root / 'lerna.json').exists():
            return RepoType.LERNA_MONOREPO

        # Check if multiple independent projects (polyrepo pattern)
        if self._is_polyrepo():
            return RepoType.POLYREPO

        # Check for standard repo (single project)
        if (self.root / '.git').exists():
            return RepoType.STANDARD

        return RepoType.UNKNOWN

    def _is_polyrepo(self) -> bool:
        """Check if this looks like a polyrepo (multiple independent repos)."""
        # Look for multiple package.json or pyproject.toml at top level
        top_level_projects = []

        for item in self.root.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                has_package = (item / 'package.json').exists()
                has_pyproject = (item / 'pyproject.toml').exists()
                has_git = (item / '.git').exists()

                if has_git or (has_package and has_pyproject):
                    top_level_projects.append(item.name)

        return len(top_level_projects) >= 2

    def _parse_gitmodules(self) -> List[SubmoduleInfo]:
        """Parse .gitmodules file for submodule information."""
        gitmodules = self.root / '.gitmodules'
        if not gitmodules.exists():
            return []

        submodules = []
        content = gitmodules.read_text()

        # Parse gitmodules format
        current_path = None
        current_url = None
        current_branch = None

        for line in content.split('\n'):
            line = line.strip()

            if line.startswith('[submodule'):
                # Save previous submodule if exists
                if current_path and current_url:
                    submodules.append(SubmoduleInfo(
                        path=current_path,
                        url=current_url,
                        branch=current_branch,
                    ))
                current_path = None
                current_url = None
                current_branch = None

            elif '=' in line:
                key, value = [x.strip() for x in line.split('=', 1)]
                if key == 'path':
                    current_path = value
                elif key == 'url':
                    current_url = value
                elif key == 'branch':
                    current_branch = value

        # Don't forget the last one
        if current_path and current_url:
            submodules.append(SubmoduleInfo(
                path=current_path,
                url=current_url,
                branch=current_branch,
            ))

        return submodules

    # -------------------------------------------------------------------------
    # Service Detection
    # -------------------------------------------------------------------------

    def _find_services(self) -> Dict[str, ServiceLocation]:
        """Locate all services in the codebase."""
        services = {}

        for category, patterns in SERVICE_PATTERNS.items():
            location = self._find_service_location(category, patterns)
            if location:
                services[category] = location

        return services

    def _find_service_location(
        self,
        category: str,
        patterns: dict
    ) -> Optional[ServiceLocation]:
        """Find a specific service type using strong/weak marker system."""
        candidate_dirs = patterns.get('dirs', [])
        strong_markers = patterns.get('strong_markers', [])
        weak_markers = patterns.get('weak_markers', [])
        frameworks = patterns.get('frameworks', {})

        # Check each candidate directory
        for dir_name in candidate_dirs:
            dir_path = self.root / dir_name
            if dir_path.is_dir():
                # Check for strong markers first (any = 100% confidence)
                for marker in strong_markers:
                    if self._marker_exists(dir_path, marker):
                        framework = self._detect_framework(dir_path, frameworks)
                        return ServiceLocation(
                            category=category,
                            path=dir_name,
                            framework=framework,
                            confidence=1.0,
                        )

                # Check weak markers (need multiple, or dir name match adds confidence)
                weak_count = sum(1 for m in weak_markers if self._marker_exists(dir_path, m))

                # Directory name matching expected pattern gives base confidence
                base_confidence = 0.6 if dir_name in candidate_dirs[:3] else 0.3
                marker_confidence = (weak_count / max(len(weak_markers), 1)) * 0.4

                confidence = base_confidence + marker_confidence

                if confidence >= 0.5:
                    framework = self._detect_framework(dir_path, frameworks)
                    return ServiceLocation(
                        category=category,
                        path=dir_name,
                        framework=framework,
                        confidence=min(confidence, 0.95),
                    )

        # Also check root directory for markers (single-service repos)
        if category in ('backend', 'frontend'):
            for marker in strong_markers:
                if self._marker_exists(self.root, marker):
                    framework = self._detect_framework(self.root, frameworks)
                    return ServiceLocation(
                        category=category,
                        path='.',
                        framework=framework,
                        confidence=0.9,
                    )

        return None

    def _marker_exists(self, path: Path, marker: str) -> bool:
        """Check if a marker exists in a directory."""
        if not path.is_dir():
            return False

        # File extension pattern (e.g., '.sh')
        if marker.startswith('.') and len(marker) > 1 and '/' not in marker:
            try:
                return any(f.suffix == marker for f in path.iterdir() if f.is_file())
            except PermissionError:
                return False

        # Directory pattern (e.g., 'services/')
        if marker.endswith('/'):
            return (path / marker.rstrip('/')).is_dir()

        # Specific file or directory
        target = path / marker
        return target.exists()

    def _verify_service(self, path: Path, markers: List[str]) -> float:
        """Verify a directory contains a service by checking markers (legacy compat)."""
        if not path.is_dir():
            return 0.0

        found = sum(1 for m in markers if self._marker_exists(path, m))
        return found / len(markers) if markers else 0.0

    def _detect_framework(
        self,
        path: Path,
        frameworks: Dict[str, List[str]]
    ) -> Optional[str]:
        """Detect which framework is used in a service directory."""
        best_match = None
        best_score = 0

        for framework, indicators in frameworks.items():
            score = 0
            for indicator in indicators:
                if indicator.startswith('.'):
                    # File extension
                    if any(f.suffix == indicator for f in path.rglob('*') if f.is_file()):
                        score += 1
                elif indicator.endswith('/'):
                    # Directory
                    if (path / indicator.rstrip('/')).is_dir():
                        score += 2  # Directories are stronger indicators
                else:
                    # File or content
                    if (path / indicator).exists():
                        score += 2
                    elif self._content_contains(path, indicator):
                        score += 1

            if score > best_score:
                best_score = score
                best_match = framework

        return best_match if best_score >= 2 else None

    def _content_contains(self, path: Path, pattern: str) -> bool:
        """Check if any relevant file contains a pattern (cached)."""
        cache_key = f"{path}:{pattern}"
        if cache_key in self._file_cache:
            return self._file_cache[cache_key] == 'found'

        # Check common files
        for filename in ['package.json', 'requirements.txt', 'pyproject.toml']:
            filepath = path / filename
            if filepath.exists():
                try:
                    content = filepath.read_text(errors='ignore')
                    if pattern.lower() in content.lower():
                        self._file_cache[cache_key] = 'found'
                        return True
                except Exception as e:
                    print(f"[WARN] Failed to read file {filepath} for pattern check: {e}")

        self._file_cache[cache_key] = 'not_found'
        return False

    # -------------------------------------------------------------------------
    # Naming Convention Detection
    # -------------------------------------------------------------------------

    def _detect_naming_conventions(self) -> List[NamingConvention]:
        """Detect naming conventions used in the codebase."""
        conventions = []

        # Analyze directory names
        dir_convention = self._analyze_name_patterns(self._get_directory_names())
        if dir_convention:
            conventions.append(NamingConvention(
                pattern=dir_convention['pattern'],
                scope='directories',
                examples=dir_convention['examples'][:5],
                confidence=dir_convention['confidence'],
            ))

        # Analyze file names (excluding extensions)
        file_convention = self._analyze_name_patterns(self._get_file_basenames())
        if file_convention:
            conventions.append(NamingConvention(
                pattern=file_convention['pattern'],
                scope='files',
                examples=file_convention['examples'][:5],
                confidence=file_convention['confidence'],
            ))

        return conventions

    def _get_directory_names(self, max_dirs: int = 200) -> List[str]:
        """Get directory names for analysis."""
        dirs = []

        for item in self.root.rglob('*'):
            if len(dirs) >= max_dirs:
                break

            if item.is_dir() and item.name not in SKIP_DIRS:
                # Skip hidden directories
                if not any(p.startswith('.') for p in item.relative_to(self.root).parts):
                    dirs.append(item.name)

        return dirs

    def _get_file_basenames(self, max_files: int = 500) -> List[str]:
        """Get file basenames (without extension) for analysis."""
        basenames = []

        # Focus on code files
        code_extensions = {'.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.rs'}

        for item in self.root.rglob('*'):
            if len(basenames) >= max_files:
                break

            if item.is_file() and item.suffix in code_extensions:
                # Skip hidden and node_modules
                parts = item.relative_to(self.root).parts
                if not any(p.startswith('.') for p in parts):
                    if 'node_modules' not in parts:
                        basenames.append(item.stem)

        return basenames

    def _analyze_name_patterns(self, names: List[str]) -> Optional[dict]:
        """Analyze a list of names to detect naming convention."""
        if len(names) < 5:
            return None

        # Pattern detection
        patterns = Counter()
        examples = {
            'snake_case': [],
            'camelCase': [],
            'PascalCase': [],
            'kebab-case': [],
            'SCREAMING_SNAKE': [],
        }

        for name in names:
            if '_' in name and name.islower():
                patterns['snake_case'] += 1
                examples['snake_case'].append(name)
            elif '-' in name and name.islower():
                patterns['kebab-case'] += 1
                examples['kebab-case'].append(name)
            elif name[0].isupper() and '_' not in name and '-' not in name:
                patterns['PascalCase'] += 1
                examples['PascalCase'].append(name)
            elif name[0].islower() and any(c.isupper() for c in name):
                patterns['camelCase'] += 1
                examples['camelCase'].append(name)
            elif name.isupper() and '_' in name:
                patterns['SCREAMING_SNAKE'] += 1
                examples['SCREAMING_SNAKE'].append(name)

        if not patterns:
            return None

        dominant_pattern = patterns.most_common(1)[0]
        total = sum(patterns.values())
        confidence = dominant_pattern[1] / total

        return {
            'pattern': dominant_pattern[0],
            'examples': examples[dominant_pattern[0]],
            'confidence': confidence,
        }

    # -------------------------------------------------------------------------
    # Config Pattern Detection
    # -------------------------------------------------------------------------

    def _detect_config_patterns(self) -> List[ConfigPattern]:
        """Detect configuration patterns in use."""
        patterns = []

        for config_type, filenames in CONFIG_PATTERNS.items():
            locations = []

            for filename in filenames:
                # Check root
                if (self.root / filename).exists():
                    locations.append(filename)

                # Check common subdirectories
                for subdir in ['config', 'configs', 'settings']:
                    subpath = self.root / subdir / filename
                    if subpath.exists():
                        locations.append(f"{subdir}/{filename}")

            if locations:
                # Determine if this is the preferred config type
                is_preferred = config_type in ('env', 'yaml')  # Common preferences

                patterns.append(ConfigPattern(
                    type=config_type,
                    locations=locations,
                    is_preferred=is_preferred and len(locations) > 1,
                ))

        return patterns

    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------

    def get_structure_hash(self) -> str:
        """Get a hash of the current structure for change detection."""
        # Hash based on top-level structure
        structure = []

        for item in sorted(self.root.iterdir()):
            if item.name.startswith('.'):
                continue
            structure.append(f"{item.name}:{item.is_dir()}")

        return hashlib.sha256('\n'.join(structure).encode()).hexdigest()[:16]

    def get_scan_stats(self) -> dict:
        """Get statistics from the last scan."""
        return self._scan_stats.copy()


# =============================================================================
# CLI INTERFACE
# =============================================================================

def test_analyzer(path: str, verbose: bool = True):
    """
    Test the analyzer on a given path.

    This is the verification entry point for testing.
    """
    from pprint import pprint

    path = Path(path).resolve()

    print()
    print("=" * 60)
    print("🔍 HIERARCHY ANALYZER TEST")
    print("=" * 60)
    print(f"Path: {path}")
    print()

    if not path.exists():
        print(f"❌ Error: Path does not exist: {path}")
        return False

    analyzer = HierarchyAnalyzer(path)

    print("Analyzing...")
    profile = analyzer.analyze()

    print()
    print("─" * 60)
    print("RESULTS")
    print("─" * 60)

    print(f"\n📁 Repository Type: {profile.repo_type.value}")

    if profile.submodules:
        print(f"\n📦 Submodules ({len(profile.submodules)}):")
        for sm in profile.submodules[:5]:
            print(f"   • {sm.path} → {sm.url}")
        if len(profile.submodules) > 5:
            print(f"   ... and {len(profile.submodules) - 5} more")

    if profile.locations:
        print(f"\n🏗️ Services Found:")
        for category, loc in profile.locations.items():
            framework = f" ({loc.framework})" if loc.framework else ""
            conf = f" [{loc.confidence:.0%}]"
            print(f"   • {category}: {loc.path}{framework}{conf}")

    if profile.naming_conventions:
        print(f"\n📝 Naming Conventions:")
        for conv in profile.naming_conventions:
            print(f"   • {conv.scope}: {conv.pattern} [{conv.confidence:.0%}]")
            if verbose:
                print(f"     Examples: {', '.join(conv.examples[:3])}")

    if profile.config_patterns:
        print(f"\n⚙️ Config Patterns:")
        for pat in profile.config_patterns:
            pref = " ⭐" if pat.is_preferred else ""
            print(f"   • {pat.type}: {', '.join(pat.locations[:3])}{pref}")

    if profile.platform:
        print(f"\n💻 Platform:")
        print(f"   • OS: {profile.platform.os} ({profile.platform.arch})")
        if profile.platform.is_apple_silicon:
            print(f"   • Apple Silicon: Yes ✓")
        print(f"   • Recommended LLM: {profile.platform.recommended_backend.value}")

    stats = analyzer.get_scan_stats()
    if stats['time_completed']:
        duration = (stats['time_completed'] - stats['time_started']).total_seconds()
        print(f"\n⏱️ Scan completed in {duration:.2f}s")

    print()
    print("=" * 60)
    print("✅ Analysis Complete")
    print("=" * 60)

    return True


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m context_dna.setup.hierarchy_analyzer <path>")
        print("       python -m context_dna.setup.hierarchy_analyzer --test <path>")
        sys.exit(1)

    args = sys.argv[1:]

    # Handle --test flag
    if args[0] == '--test':
        path = args[1] if len(args) > 1 else '.'
        success = test_analyzer(path)
        sys.exit(0 if success else 1)
    else:
        path = args[0]
        success = test_analyzer(path)
        sys.exit(0 if success else 1)
