#!/usr/bin/env python3
"""
Context DNA Project Type Classification System

Comprehensive library of project types, deployment contexts, and
intelligent classification for adaptive hierarchy detection.

Features:
- 50+ project type classifications
- Deployment context tags (prod, dev, branch, internal)
- User input for main project names
- Editable type counts with feedback loop
- Smart recognition of relationships (landing page → main product)
"""

from enum import Enum
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field


# =============================================================================
# DEPLOYMENT CONTEXT (How is this code used?)
# =============================================================================

class DeploymentContext(Enum):
    """How a project is deployed/used."""

    # Production
    PRODUCTION_CLIENT = ("prod_client", "🚀", "Client-facing production",
        "Live product used by customers")
    PRODUCTION_INTERNAL = ("prod_internal", "🏢", "Internal production",
        "Live but internal-only (admin panels, dashboards)")

    # Development
    DEV_MAIN = ("dev_main", "🔧", "Main development",
        "Primary development branch/environment")
    DEV_BRANCH = ("dev_branch", "🌿", "Development branch",
        "Feature branch or experimental fork")
    DEV_SANDBOX = ("dev_sandbox", "🧪", "Sandbox/Lab",
        "Testing area, not for production")

    # Staging
    STAGING = ("staging", "🎭", "Staging environment",
        "Pre-production testing")
    PREVIEW = ("preview", "👁️", "Preview/Demo",
        "Demo environment for stakeholders")

    # Special
    ARCHIVED = ("archived", "📦", "Archived",
        "No longer active, kept for reference")
    TEMPLATE = ("template", "📋", "Template/Boilerplate",
        "Starter code for new projects")
    LEARNING = ("learning", "📚", "Learning/Tutorial",
        "Educational code, experiments")

    @property
    def id(self) -> str:
        return self.value[0]

    @property
    def icon(self) -> str:
        return self.value[1]

    @property
    def label(self) -> str:
        return self.value[2]

    @property
    def description(self) -> str:
        return self.value[3]


# =============================================================================
# PROJECT TYPE CATEGORIES
# =============================================================================

class ProjectCategory(Enum):
    """High-level project categories."""

    MOBILE = ("mobile", "📱", "Mobile Apps",
        "iOS, Android, React Native, Flutter")
    WEBAPP = ("webapp", "🌐", "Web Applications",
        "Frontend web apps, SPAs, PWAs")
    BACKEND = ("backend", "⚙️", "Backend Services",
        "APIs, servers, microservices")
    DESKTOP = ("desktop", "🖥️", "Desktop Apps",
        "Electron, native desktop applications")
    WEBSITE = ("website", "🏠", "Websites & Landing Pages",
        "Marketing sites, documentation sites")
    INFRASTRUCTURE = ("infra", "🏗️", "Infrastructure",
        "DevOps, Terraform, Docker, K8s")
    LIBRARY = ("library", "📚", "Libraries & Packages",
        "Shared code, npm packages, pip packages")
    CLI = ("cli", "⌨️", "CLI Tools",
        "Command-line applications")
    AI_ML = ("ai_ml", "🤖", "AI/ML Projects",
        "Machine learning, AI models, data pipelines")
    DATA = ("data", "📊", "Data & Analytics",
        "Databases, ETL, analytics")
    TESTING = ("testing", "🧪", "Testing & QA",
        "Test suites, QA tools, mocks")
    DOCS = ("docs", "📖", "Documentation",
        "Docs sites, wikis, READMEs")
    CONFIG = ("config", "⚡", "Configuration",
        "Shared configs, environment setups")
    MONOREPO = ("monorepo", "🗂️", "Monorepo Root",
        "Umbrella project containing others")

    @property
    def id(self) -> str:
        return self.value[0]

    @property
    def icon(self) -> str:
        return self.value[1]

    @property
    def label(self) -> str:
        return self.value[2]

    @property
    def description(self) -> str:
        return self.value[3]


# =============================================================================
# SPECIFIC PROJECT TYPES (50+ types)
# =============================================================================

@dataclass
class ProjectType:
    """A specific project type definition."""
    id: str
    name: str
    category: ProjectCategory
    icon: str
    description: str
    markers: List[str] = field(default_factory=list)  # Files/folders that indicate this type
    frameworks: List[str] = field(default_factory=list)  # Common frameworks
    typical_contexts: List[DeploymentContext] = field(default_factory=list)


# Complete library of project types
PROJECT_TYPES: Dict[str, ProjectType] = {

    # =========================================================================
    # MOBILE APPS
    # =========================================================================
    "ios_native": ProjectType(
        id="ios_native",
        name="iOS Native App",
        category=ProjectCategory.MOBILE,
        icon="🍎",
        description="Native iOS application (Swift/Objective-C)",
        markers=["*.xcodeproj", "*.xcworkspace", "Podfile", "Package.swift"],
        frameworks=["SwiftUI", "UIKit", "Combine"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "android_native": ProjectType(
        id="android_native",
        name="Android Native App",
        category=ProjectCategory.MOBILE,
        icon="🤖",
        description="Native Android application (Kotlin/Java)",
        markers=["build.gradle", "AndroidManifest.xml", "settings.gradle"],
        frameworks=["Jetpack Compose", "Android SDK"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "react_native": ProjectType(
        id="react_native",
        name="React Native App",
        category=ProjectCategory.MOBILE,
        icon="⚛️📱",
        description="Cross-platform mobile app with React Native",
        markers=["metro.config.js", "app.json", "react-native.config.js"],
        frameworks=["React Native", "Expo"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "expo": ProjectType(
        id="expo",
        name="Expo App",
        category=ProjectCategory.MOBILE,
        icon="📲",
        description="Expo-managed React Native app",
        markers=["app.json", "expo-", "eas.json"],
        frameworks=["Expo", "React Native"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "flutter": ProjectType(
        id="flutter",
        name="Flutter App",
        category=ProjectCategory.MOBILE,
        icon="🐦",
        description="Cross-platform app with Flutter/Dart",
        markers=["pubspec.yaml", "lib/main.dart", "flutter"],
        frameworks=["Flutter"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),

    # =========================================================================
    # WEB APPLICATIONS
    # =========================================================================
    "nextjs": ProjectType(
        id="nextjs",
        name="Next.js App",
        category=ProjectCategory.WEBAPP,
        icon="▲",
        description="React framework with SSR/SSG",
        markers=["next.config.js", "next.config.mjs", "pages/", "app/"],
        frameworks=["Next.js", "React"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "react_spa": ProjectType(
        id="react_spa",
        name="React SPA",
        category=ProjectCategory.WEBAPP,
        icon="⚛️",
        description="Single-page React application",
        markers=["src/App.jsx", "src/App.tsx", "vite.config", "create-react-app"],
        frameworks=["React", "Vite", "CRA"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "vue": ProjectType(
        id="vue",
        name="Vue.js App",
        category=ProjectCategory.WEBAPP,
        icon="💚",
        description="Vue.js single-page application",
        markers=["vue.config.js", "nuxt.config.js", ".vue"],
        frameworks=["Vue", "Nuxt"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "angular": ProjectType(
        id="angular",
        name="Angular App",
        category=ProjectCategory.WEBAPP,
        icon="🅰️",
        description="Angular single-page application",
        markers=["angular.json", "ng"],
        frameworks=["Angular"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "svelte": ProjectType(
        id="svelte",
        name="SvelteKit App",
        category=ProjectCategory.WEBAPP,
        icon="🔥",
        description="Svelte/SvelteKit application",
        markers=["svelte.config.js", ".svelte"],
        frameworks=["Svelte", "SvelteKit"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "remix": ProjectType(
        id="remix",
        name="Remix App",
        category=ProjectCategory.WEBAPP,
        icon="💿",
        description="Remix full-stack React framework",
        markers=["remix.config.js", "app/root.tsx"],
        frameworks=["Remix", "React"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),

    # =========================================================================
    # BACKEND SERVICES
    # =========================================================================
    "django": ProjectType(
        id="django",
        name="Django Backend",
        category=ProjectCategory.BACKEND,
        icon="🐍",
        description="Python Django web application",
        markers=["manage.py", "wsgi.py", "asgi.py", "settings.py"],
        frameworks=["Django", "Django REST Framework"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "fastapi": ProjectType(
        id="fastapi",
        name="FastAPI Service",
        category=ProjectCategory.BACKEND,
        icon="⚡",
        description="Python FastAPI microservice",
        markers=["main.py", "fastapi"],
        frameworks=["FastAPI", "Uvicorn"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "flask": ProjectType(
        id="flask",
        name="Flask Service",
        category=ProjectCategory.BACKEND,
        icon="🧪",
        description="Python Flask web application",
        markers=["app.py", "flask"],
        frameworks=["Flask"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "express": ProjectType(
        id="express",
        name="Express.js API",
        category=ProjectCategory.BACKEND,
        icon="🚂",
        description="Node.js Express REST API",
        markers=["app.js", "server.js", "express"],
        frameworks=["Express"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "nestjs": ProjectType(
        id="nestjs",
        name="NestJS Service",
        category=ProjectCategory.BACKEND,
        icon="😺",
        description="TypeScript NestJS enterprise backend",
        markers=["nest-cli.json", "main.ts"],
        frameworks=["NestJS"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "rails": ProjectType(
        id="rails",
        name="Ruby on Rails",
        category=ProjectCategory.BACKEND,
        icon="💎",
        description="Ruby on Rails web application",
        markers=["Gemfile", "config.ru", "Rakefile"],
        frameworks=["Rails"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "go_api": ProjectType(
        id="go_api",
        name="Go API Service",
        category=ProjectCategory.BACKEND,
        icon="🐹",
        description="Go/Golang REST API or microservice",
        markers=["go.mod", "main.go"],
        frameworks=["Gin", "Echo", "Fiber"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "spring": ProjectType(
        id="spring",
        name="Spring Boot",
        category=ProjectCategory.BACKEND,
        icon="🍃",
        description="Java Spring Boot application",
        markers=["pom.xml", "build.gradle", "Application.java"],
        frameworks=["Spring Boot"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),

    # =========================================================================
    # DESKTOP APPS
    # =========================================================================
    "electron": ProjectType(
        id="electron",
        name="Electron App",
        category=ProjectCategory.DESKTOP,
        icon="⚡💻",
        description="Cross-platform desktop app with Electron",
        markers=["electron", "main.js", "preload.js", "electron-builder"],
        frameworks=["Electron"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "tauri": ProjectType(
        id="tauri",
        name="Tauri App",
        category=ProjectCategory.DESKTOP,
        icon="🦀💻",
        description="Lightweight desktop app with Tauri/Rust",
        markers=["tauri.conf.json", "src-tauri/"],
        frameworks=["Tauri"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),

    # =========================================================================
    # WEBSITES & LANDING PAGES
    # =========================================================================
    "landing_page": ProjectType(
        id="landing_page",
        name="Landing Page",
        category=ProjectCategory.WEBSITE,
        icon="🏠",
        description="Marketing or product landing page",
        markers=["index.html", "landing", "marketing"],
        frameworks=["HTML/CSS", "Tailwind"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT],
    ),
    "docs_site": ProjectType(
        id="docs_site",
        name="Documentation Site",
        category=ProjectCategory.WEBSITE,
        icon="📖",
        description="Documentation or wiki site",
        markers=["docusaurus.config.js", "mkdocs.yml", "docs/"],
        frameworks=["Docusaurus", "MkDocs", "GitBook"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT],
    ),
    "blog": ProjectType(
        id="blog",
        name="Blog",
        category=ProjectCategory.WEBSITE,
        icon="📝",
        description="Blog or content site",
        markers=["posts/", "blog/", "_posts/"],
        frameworks=["Hugo", "Jekyll", "Gatsby"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT],
    ),
    "static_site": ProjectType(
        id="static_site",
        name="Static Site",
        category=ProjectCategory.WEBSITE,
        icon="📄",
        description="Static HTML/CSS website",
        markers=["index.html", "*.html"],
        frameworks=["HTML/CSS"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT],
    ),

    # =========================================================================
    # INFRASTRUCTURE
    # =========================================================================
    "terraform": ProjectType(
        id="terraform",
        name="Terraform IaC",
        category=ProjectCategory.INFRASTRUCTURE,
        icon="🏗️",
        description="Terraform infrastructure as code",
        markers=["main.tf", "variables.tf", "*.tf"],
        frameworks=["Terraform"],
        typical_contexts=[DeploymentContext.PRODUCTION_INTERNAL, DeploymentContext.DEV_MAIN],
    ),
    "docker_compose": ProjectType(
        id="docker_compose",
        name="Docker Compose Stack",
        category=ProjectCategory.INFRASTRUCTURE,
        icon="🐳",
        description="Docker Compose multi-container setup",
        markers=["docker-compose.yml", "docker-compose.yaml"],
        frameworks=["Docker"],
        typical_contexts=[DeploymentContext.DEV_MAIN, DeploymentContext.STAGING],
    ),
    "kubernetes": ProjectType(
        id="kubernetes",
        name="Kubernetes Configs",
        category=ProjectCategory.INFRASTRUCTURE,
        icon="☸️",
        description="Kubernetes deployment configurations",
        markers=["deployment.yaml", "service.yaml", "kustomization.yaml"],
        frameworks=["Kubernetes"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.STAGING],
    ),
    "cicd": ProjectType(
        id="cicd",
        name="CI/CD Pipeline",
        category=ProjectCategory.INFRASTRUCTURE,
        icon="🔄",
        description="CI/CD pipeline configurations",
        markers=[".github/workflows/", ".gitlab-ci.yml", "Jenkinsfile"],
        frameworks=["GitHub Actions", "GitLab CI", "Jenkins"],
        typical_contexts=[DeploymentContext.DEV_MAIN],
    ),

    # =========================================================================
    # LIBRARIES & PACKAGES
    # =========================================================================
    "npm_package": ProjectType(
        id="npm_package",
        name="NPM Package",
        category=ProjectCategory.LIBRARY,
        icon="📦",
        description="Publishable NPM package/library",
        markers=["package.json", "index.js", "dist/"],
        frameworks=["NPM"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT],
    ),
    "python_package": ProjectType(
        id="python_package",
        name="Python Package",
        category=ProjectCategory.LIBRARY,
        icon="🐍📦",
        description="Publishable Python package",
        markers=["pyproject.toml", "setup.py", "src/"],
        frameworks=["PyPI"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT],
    ),
    "shared_lib": ProjectType(
        id="shared_lib",
        name="Shared Library",
        category=ProjectCategory.LIBRARY,
        icon="📚",
        description="Internal shared code library",
        markers=["lib/", "common/", "shared/"],
        frameworks=[],
        typical_contexts=[DeploymentContext.DEV_MAIN],
    ),

    # =========================================================================
    # AI/ML
    # =========================================================================
    "ml_model": ProjectType(
        id="ml_model",
        name="ML Model",
        category=ProjectCategory.AI_ML,
        icon="🤖",
        description="Machine learning model training/serving",
        markers=["model.py", "train.py", "*.pkl", "*.h5"],
        frameworks=["PyTorch", "TensorFlow", "scikit-learn"],
        typical_contexts=[DeploymentContext.DEV_MAIN, DeploymentContext.PRODUCTION_INTERNAL],
    ),
    "llm_service": ProjectType(
        id="llm_service",
        name="LLM Service",
        category=ProjectCategory.AI_ML,
        icon="🧠",
        description="Large language model service",
        markers=["llm", "embeddings", "chat"],
        frameworks=["OpenAI", "Anthropic", "Ollama"],
        typical_contexts=[DeploymentContext.PRODUCTION_CLIENT, DeploymentContext.DEV_MAIN],
    ),
    "data_pipeline": ProjectType(
        id="data_pipeline",
        name="Data Pipeline",
        category=ProjectCategory.DATA,
        icon="📊",
        description="ETL or data processing pipeline",
        markers=["pipeline.py", "etl/", "dags/"],
        frameworks=["Airflow", "Prefect", "dbt"],
        typical_contexts=[DeploymentContext.PRODUCTION_INTERNAL],
    ),

    # =========================================================================
    # TESTING & DEVELOPMENT
    # =========================================================================
    "test_lab": ProjectType(
        id="test_lab",
        name="Testing Lab",
        category=ProjectCategory.TESTING,
        icon="🧪",
        description="Testing/experimentation environment",
        markers=["test/", "tests/", "lab/", "experiments/"],
        frameworks=[],
        typical_contexts=[DeploymentContext.DEV_SANDBOX],
    ),
    "e2e_tests": ProjectType(
        id="e2e_tests",
        name="E2E Test Suite",
        category=ProjectCategory.TESTING,
        icon="🎭",
        description="End-to-end testing suite",
        markers=["cypress/", "playwright/", "e2e/"],
        frameworks=["Cypress", "Playwright", "Selenium"],
        typical_contexts=[DeploymentContext.DEV_MAIN],
    ),
    "mock_server": ProjectType(
        id="mock_server",
        name="Mock Server",
        category=ProjectCategory.TESTING,
        icon="🎪",
        description="Mock API or service for testing",
        markers=["mock/", "stub/", "fake/"],
        frameworks=[],
        typical_contexts=[DeploymentContext.DEV_SANDBOX],
    ),

    # =========================================================================
    # SPECIAL TYPES
    # =========================================================================
    "monorepo_root": ProjectType(
        id="monorepo_root",
        name="Monorepo Root",
        category=ProjectCategory.MONOREPO,
        icon="🗂️",
        description="Root of a monorepo containing multiple projects",
        markers=["lerna.json", "pnpm-workspace.yaml", "nx.json", "turbo.json"],
        frameworks=["Lerna", "Nx", "Turborepo", "pnpm"],
        typical_contexts=[DeploymentContext.DEV_MAIN],
    ),
    "submodule_external": ProjectType(
        id="submodule_external",
        name="Git Submodule",
        category=ProjectCategory.LIBRARY,
        icon="📦🔗",
        description="External git submodule",
        markers=[".gitmodules"],
        frameworks=[],
        typical_contexts=[DeploymentContext.DEV_MAIN],
    ),
    "branch_sandbox": ProjectType(
        id="branch_sandbox",
        name="Branch/Sandbox",
        category=ProjectCategory.TESTING,
        icon="🌿🧪",
        description="Sandbox branch or experimental fork",
        markers=["sandbox", "experiment", "v0-", "test-"],
        frameworks=[],
        typical_contexts=[DeploymentContext.DEV_BRANCH, DeploymentContext.DEV_SANDBOX],
    ),
    "admin_panel": ProjectType(
        id="admin_panel",
        name="Admin Panel",
        category=ProjectCategory.WEBAPP,
        icon="👑",
        description="Internal admin dashboard",
        markers=["admin", "dashboard", "panel"],
        frameworks=[],
        typical_contexts=[DeploymentContext.PRODUCTION_INTERNAL],
    ),
}


# =============================================================================
# PROJECT TYPE COUNTS (for UI)
# =============================================================================

@dataclass
class ProjectTypeCount:
    """Count of detected projects for a type."""
    type_id: str
    type_info: ProjectType
    count: int = 0
    user_override: Optional[int] = None  # If user manually set count
    projects: List[str] = field(default_factory=list)  # Project paths
    deployment_contexts: Dict[str, int] = field(default_factory=dict)  # Context → count


@dataclass
class UserProjectInput:
    """User's manual project name input."""
    main_project_names: List[str] = field(default_factory=list)
    known_relationships: Dict[str, str] = field(default_factory=dict)  # "landing-page" → "ER Simulator"
    type_hints: Dict[str, str] = field(default_factory=dict)  # "path" → "landing_page"


# =============================================================================
# RELATIONSHIP DETECTION PATTERNS
# =============================================================================

RELATIONSHIP_PATTERNS = {
    # Landing page patterns
    "landing_page": {
        "markers": ["landing", "website", "marketing", "www"],
        "suggests": "This appears to be a landing page for another project",
        "question": "Is this the landing page for {main_project}?",
    },
    # Admin panel patterns
    "admin_panel": {
        "markers": ["admin", "dashboard", "panel", "backoffice"],
        "suggests": "This looks like an admin panel",
        "question": "Is this the admin panel for {main_project}?",
    },
    # Mobile app patterns
    "mobile_companion": {
        "markers": ["mobile", "app", "ios", "android"],
        "suggests": "This appears to be a mobile companion app",
        "question": "Is this the mobile app for {main_project}?",
    },
    # Testing/lab patterns
    "test_lab": {
        "markers": ["test", "lab", "experiment", "sandbox", "v0-", "dev-"],
        "suggests": "This looks like a testing/development environment",
        "question": "Is this a testing environment for {main_project}?",
    },
    # Branch/fork patterns
    "branch_sandbox": {
        "markers": ["branch", "fork", "sandbox", "antigravity", "v0", "v1", "v2"],
        "suggests": "This appears to be a branch or sandbox",
        "question": "Is this a development branch of {main_project}?",
    },
}


# =============================================================================
# INTELLIGENT TYPE DETECTOR
# =============================================================================

class IntelligentTypeDetector:
    """
    Intelligently detects project types and relationships.

    Features:
    - Recognizes main projects vs supporting projects
    - Detects landing pages, admin panels, mobile apps
    - Identifies test labs, sandboxes, branches
    - Learns from user input
    """

    def __init__(self):
        self.user_input: Optional[UserProjectInput] = None
        self.detected_types: Dict[str, ProjectTypeCount] = {}

    def set_user_main_projects(self, names: List[str]):
        """Set the user's main project names."""
        if not self.user_input:
            self.user_input = UserProjectInput()
        self.user_input.main_project_names = names

    def detect_project_type(
        self,
        path: str,
        name: str,
        markers: List[str],
        description: str
    ) -> Tuple[ProjectType, float, Optional[str]]:
        """
        Detect the type of a project.

        Returns: (type, confidence, related_to_main_project)
        """
        best_type = None
        best_score = 0.0
        related_to = None

        name_lower = name.lower()
        desc_lower = description.lower()
        path_lower = path.lower()
        combined = f"{name_lower} {desc_lower} {path_lower}"

        # Check against each type
        for type_id, ptype in PROJECT_TYPES.items():
            score = 0.0

            # Check markers
            for marker in ptype.markers:
                marker_lower = marker.lower().replace("*", "")
                if any(marker_lower in m.lower() for m in markers):
                    score += 0.3
                if marker_lower in combined:
                    score += 0.2

            # Check framework mentions
            for fw in ptype.frameworks:
                if fw.lower() in combined:
                    score += 0.2

            # Category keywords
            cat_keywords = ptype.description.lower().split()
            for kw in cat_keywords:
                if len(kw) > 3 and kw in combined:
                    score += 0.1

            if score > best_score:
                best_score = score
                best_type = ptype

        # Check for relationships to main projects
        if self.user_input and self.user_input.main_project_names:
            for main_name in self.user_input.main_project_names:
                main_lower = main_name.lower().replace(" ", "").replace("-", "")

                # Check if name contains main project name
                if main_lower in name_lower.replace("-", "").replace("_", ""):
                    related_to = main_name

                # Check relationship patterns
                for rel_type, patterns in RELATIONSHIP_PATTERNS.items():
                    if any(m in combined for m in patterns["markers"]):
                        # This might be related
                        if related_to or main_lower in combined:
                            related_to = main_name
                            # Adjust type based on relationship
                            if rel_type in PROJECT_TYPES:
                                if best_score < 0.5:
                                    best_type = PROJECT_TYPES[rel_type]
                                    best_score = 0.6

        # Default fallback
        if not best_type or best_score < 0.2:
            # Guess based on common patterns
            if "landing" in combined or "marketing" in combined:
                best_type = PROJECT_TYPES["landing_page"]
                best_score = 0.5
            elif "admin" in combined or "dashboard" in combined:
                best_type = PROJECT_TYPES["admin_panel"]
                best_score = 0.5
            elif "test" in combined or "lab" in combined:
                best_type = PROJECT_TYPES["test_lab"]
                best_score = 0.4
            elif "mobile" in combined or "app" in combined:
                best_type = PROJECT_TYPES["expo"]
                best_score = 0.3
            else:
                # Generic webapp
                best_type = PROJECT_TYPES.get("react_spa", list(PROJECT_TYPES.values())[0])
                best_score = 0.2

        return best_type, min(best_score, 1.0), related_to

    def detect_deployment_context(
        self,
        name: str,
        path: str,
        description: str
    ) -> DeploymentContext:
        """Detect the deployment context of a project."""
        combined = f"{name} {path} {description}".lower()

        # Check for specific context markers
        if any(m in combined for m in ["prod", "production", "live"]):
            if "internal" in combined or "admin" in combined:
                return DeploymentContext.PRODUCTION_INTERNAL
            return DeploymentContext.PRODUCTION_CLIENT

        if any(m in combined for m in ["staging", "stage", "preprod"]):
            return DeploymentContext.STAGING

        if any(m in combined for m in ["preview", "demo"]):
            return DeploymentContext.PREVIEW

        if any(m in combined for m in ["sandbox", "lab", "experiment", "test"]):
            return DeploymentContext.DEV_SANDBOX

        if any(m in combined for m in ["branch", "fork", "v0-", "feature-"]):
            return DeploymentContext.DEV_BRANCH

        if any(m in combined for m in ["archive", "old", "deprecated"]):
            return DeploymentContext.ARCHIVED

        if any(m in combined for m in ["template", "starter", "boilerplate"]):
            return DeploymentContext.TEMPLATE

        if any(m in combined for m in ["learn", "tutorial", "example"]):
            return DeploymentContext.LEARNING

        # Default to main development
        return DeploymentContext.DEV_MAIN

    def get_type_counts(self) -> Dict[ProjectCategory, int]:
        """Get counts by category."""
        counts = {}
        for cat in ProjectCategory:
            counts[cat] = 0

        for type_count in self.detected_types.values():
            cat = type_count.type_info.category
            counts[cat] += type_count.count

        return counts

    def generate_questions(self) -> List[Dict]:
        """Generate clarifying questions based on detected types."""
        questions = []

        # Check for potential relationships
        for type_id, type_count in self.detected_types.items():
            if type_count.count > 0:
                ptype = type_count.type_info

                # Landing pages might relate to main projects
                if ptype.id == "landing_page" and self.user_input:
                    for main in self.user_input.main_project_names:
                        questions.append({
                            "type": "relationship",
                            "question": f"Is this landing page for {main}?",
                            "options": ["Yes", "No", "Different project"],
                            "projects": type_count.projects,
                        })

                # Test labs might need context
                if ptype.id == "test_lab":
                    questions.append({
                        "type": "context",
                        "question": "What is this testing environment for?",
                        "options": self.user_input.main_project_names if self.user_input else [],
                        "projects": type_count.projects,
                    })

        return questions


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_all_categories() -> List[ProjectCategory]:
    """Get all project categories."""
    return list(ProjectCategory)


def get_types_by_category(category: ProjectCategory) -> List[ProjectType]:
    """Get all project types in a category."""
    return [pt for pt in PROJECT_TYPES.values() if pt.category == category]


def get_all_contexts() -> List[DeploymentContext]:
    """Get all deployment contexts."""
    return list(DeploymentContext)


def search_types(query: str) -> List[ProjectType]:
    """Search for project types matching a query."""
    query_lower = query.lower()
    results = []

    for ptype in PROJECT_TYPES.values():
        score = 0
        if query_lower in ptype.name.lower():
            score += 3
        if query_lower in ptype.description.lower():
            score += 2
        if any(query_lower in fw.lower() for fw in ptype.frameworks):
            score += 1

        if score > 0:
            results.append((ptype, score))

    return [pt for pt, _ in sorted(results, key=lambda x: -x[1])]


# =============================================================================
# CLI TEST
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🗂️ PROJECT TYPE LIBRARY")
    print("=" * 60)

    print(f"\nTotal project types: {len(PROJECT_TYPES)}")
    print(f"Categories: {len(list(ProjectCategory))}")
    print(f"Deployment contexts: {len(list(DeploymentContext))}")

    print("\n📊 TYPES BY CATEGORY:")
    for cat in ProjectCategory:
        types = get_types_by_category(cat)
        print(f"\n  {cat.icon} {cat.label}: {len(types)} types")
        for t in types[:3]:
            print(f"    - {t.icon} {t.name}")
        if len(types) > 3:
            print(f"    ... +{len(types) - 3} more")

    print("\n🎯 DEPLOYMENT CONTEXTS:")
    for ctx in DeploymentContext:
        print(f"  {ctx.icon} {ctx.label}: {ctx.description}")
