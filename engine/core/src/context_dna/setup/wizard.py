#!/usr/bin/env python3
"""
AI-Assisted Setup Wizard - Interactive LLM-Powered Configuration

This module provides an intelligent setup assistant that:
1. Detects system environment and capabilities
2. Uses LLM to generate specific commands for user's system
3. Guides through secure API key configuration
4. Validates each step before proceeding

INTERACTIVE LLM SETUP:
The wizard uses a real LLM to help with:
- Generating system-specific commands
- Troubleshooting errors
- Explaining configuration options
- Suggesting optimal settings

SECURITY:
- Never exposes or logs API keys
- Uses system keychain for secure storage
- Validates keys without storing in plain text
- Warns against insecure practices

Usage:
    # CLI
    context-dna setup --wizard

    # Python
    from context_dna.setup.wizard import SetupWizard
    wizard = SetupWizard()
    wizard.run()
"""

import os
import sys
import json
import platform
import subprocess
from pathlib import Path
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass, field


# =============================================================================
# SETUP STEP DEFINITION
# =============================================================================

@dataclass
class SetupStep:
    """A single step in the setup process."""
    id: str
    name: str
    description: str
    check_fn: Callable[[], bool]  # Returns True if step is complete
    action_fn: Callable[[], bool]  # Performs the step, returns success
    required: bool = True
    depends_on: List[str] = field(default_factory=list)
    ai_prompt: Optional[str] = None  # Prompt for AI assistance


# =============================================================================
# SETUP WIZARD
# =============================================================================

class SetupWizard:
    """
    Interactive AI-powered setup wizard.

    Guides users through Context DNA configuration with:
    - Automatic system detection
    - LLM-generated commands
    - Secure credential handling
    - Step-by-step validation
    """

    def __init__(self, project_dir: str = None, quiet: bool = False):
        """
        Initialize setup wizard.

        Args:
            project_dir: Target project directory
            quiet: If True, minimize output
        """
        self.project_dir = Path(project_dir or os.getcwd())
        self.config_dir = self.project_dir / ".context-dna"
        self.quiet = quiet
        self.llm_available = False
        self._llm_provider = None
        self._detect_llm()

    def _detect_llm(self):
        """Detect available LLM for assistance."""
        # Check Ollama first (free, local)
        try:
            import urllib.request
            req = urllib.request.Request("http://localhost:11434/api/tags")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    self.llm_available = True
                    self._llm_provider = "ollama"
                    return
        except Exception:
            pass

        # Check for API keys — honor LLM_PROVIDER env flag (default: anthropic)
        _llm_flag = os.environ.get("LLM_PROVIDER", "anthropic")
        _has_deepseek = bool(
            os.environ.get("Context_DNA_Deep_Seek")
            or os.environ.get("Context_DNA_Deepseek")
            or os.environ.get("DEEPSEEK_API_KEY")
        )
        if _llm_flag == "deepseek" and _has_deepseek:
            self.llm_available = True
            self._llm_provider = "deepseek"
        elif os.environ.get("Context_DNA_OPENAI"):
            self.llm_available = True
            self._llm_provider = "openai"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            self.llm_available = True
            self._llm_provider = "anthropic"
        elif _has_deepseek:
            # Fallback: DeepSeek available even without explicit flag
            self.llm_available = True
            self._llm_provider = "deepseek"

    def _ask_llm(self, prompt: str) -> Optional[str]:
        """
        Ask the LLM for help with setup.

        Args:
            prompt: The question/request

        Returns:
            LLM response or None
        """
        if not self.llm_available:
            return None

        system_context = f"""You are a helpful setup assistant for Context DNA.
User's system:
- OS: {platform.system()} {platform.release()}
- Python: {platform.python_version()}
- Shell: {os.environ.get('SHELL', 'unknown')}
- Architecture: {platform.machine()}

Provide SPECIFIC, ACTIONABLE commands for their exact system.
Be concise - numbered steps with exact commands.

⚠️ CRITICAL SECURITY RULES:
- NEVER show example API keys that look real
- NEVER ask users to paste keys in chat
- Tell them to use IDE environment variables
- Direct them to official sites for keys"""

        try:
            if self._llm_provider == "ollama":
                return self._ask_ollama(system_context, prompt)
            elif self._llm_provider == "openai":
                return self._ask_openai(system_context, prompt)
            elif self._llm_provider == "deepseek":
                return self._ask_deepseek(system_context, prompt)
            elif self._llm_provider == "anthropic":
                return self._ask_anthropic(system_context, prompt)
        except Exception as e:
            return None

        return None

    def _ask_ollama(self, system: str, prompt: str) -> str:
        """Ask Ollama."""
        import urllib.request
        import json

        data = json.dumps({
            "model": "llama3.2:3b",
            "prompt": f"{system}\n\nUser question: {prompt}",
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result.get("response", "")

    def _ask_openai(self, system: str, prompt: str) -> str:
        """Ask OpenAI."""
        import urllib.request
        import json

        data = json.dumps({
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 500,
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ['Context_DNA_OPENAI']}",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result["choices"][0]["message"]["content"]

    def _ask_deepseek(self, system: str, prompt: str) -> str:
        """Ask DeepSeek (OpenAI-compatible endpoint)."""
        import urllib.request
        import json

        key = (
            os.environ.get("Context_DNA_Deep_Seek")
            or os.environ.get("Context_DNA_Deepseek")
            or os.environ.get("DEEPSEEK_API_KEY", "")
        )
        data = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 500,
        }).encode()

        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result["choices"][0]["message"]["content"]

    def _ask_anthropic(self, system: str, prompt: str) -> str:
        """Ask Anthropic."""
        import urllib.request
        import json

        data = json.dumps({
            "model": "claude-3-haiku-20240307",
            "max_tokens": 500,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result["content"][0]["text"]

    # -------------------------------------------------------------------------
    # Setup Steps
    # -------------------------------------------------------------------------

    def run(self) -> bool:
        """
        Run the interactive setup wizard.

        Returns:
            True if setup completed successfully
        """
        self._print_header()

        steps = [
            self._step_init(),
            self._step_storage(),
            self._step_llm_provider(),
            self._step_ide_hooks(),
            self._step_test(),
        ]

        completed = 0
        for step in steps:
            # Check if already complete
            if step.check_fn():
                self._print_step_ok(step)
                completed += 1
                continue

            # Show step
            self._print_step_start(step)

            # Offer AI assistance if available
            if self.llm_available and step.ai_prompt:
                if self._prompt_yes_no("Would you like AI assistance?", default=True):
                    guidance = self._ask_llm(step.ai_prompt)
                    if guidance:
                        print("\n📝 AI Guidance:")
                        print("-" * 40)
                        print(guidance)
                        print("-" * 40)
                        print()

            # Run the action
            success = step.action_fn()

            if success:
                self._print_step_ok(step)
                completed += 1
            elif step.required:
                self._print_step_fail(step)
                if not self._prompt_yes_no("Continue anyway?", default=False):
                    return False
            else:
                self._print_step_skip(step)

        # Final summary
        self._print_summary(completed, len(steps))
        return completed == len(steps)

    def _step_init(self) -> SetupStep:
        """Initialize Context DNA in project."""
        def check():
            return (self.config_dir / "config.json").exists()

        def action():
            self.config_dir.mkdir(parents=True, exist_ok=True)
            config = {
                "version": "1.0.0",
                "created_at": __import__("datetime").datetime.now().isoformat(),
                "project_dir": str(self.project_dir),
            }
            (self.config_dir / "config.json").write_text(json.dumps(config, indent=2))
            return True

        return SetupStep(
            id="init",
            name="Project Initialization",
            description="Create Context DNA configuration",
            check_fn=check,
            action_fn=action,
            ai_prompt="How do I initialize a new project with Context DNA?",
        )

    def _step_storage(self) -> SetupStep:
        """Configure storage backend."""
        def check():
            # SQLite will be created on first use
            return True

        def action():
            print("\n📦 Storage Options:")
            print("  1. SQLite (default, zero-config)")
            print("  2. PostgreSQL with pgvector (advanced)")
            print()

            choice = input("Choose [1]: ").strip() or "1"

            if choice == "1":
                print("✅ SQLite will be used (no configuration needed)")
                return True
            elif choice == "2":
                return self._configure_postgres()

            return True

        return SetupStep(
            id="storage",
            name="Storage Backend",
            description="Configure where learnings are stored",
            check_fn=check,
            action_fn=action,
            required=False,
            ai_prompt="Help me decide between SQLite and PostgreSQL for Context DNA storage.",
        )

    def _step_llm_provider(self) -> SetupStep:
        """Configure LLM provider."""
        def check():
            # Check Ollama
            try:
                import urllib.request
                req = urllib.request.Request("http://localhost:11434/api/tags")
                with urllib.request.urlopen(req, timeout=2):
                    return True
            except Exception:
                pass

            # Check API keys
            return bool(
                os.environ.get("Context_DNA_OPENAI") or
                os.environ.get("ANTHROPIC_API_KEY") or
                os.environ.get("Context_DNA_Deep_Seek") or
                os.environ.get("Context_DNA_Deepseek") or
                os.environ.get("DEEPSEEK_API_KEY")
            )

        def action():
            print("\n🤖 LLM Provider Options:")
            print("  1. Ollama (FREE, runs locally) ⭐ Recommended")
            print("  2. DeepSeek (cheap, OpenAI-compatible)")
            print("  3. OpenAI (requires API key)")
            print("  4. Anthropic (requires API key)")
            print()

            choice = input("Choose [1]: ").strip() or "1"

            if choice == "1":
                return self._setup_ollama()
            elif choice == "2":
                return self._setup_api_key("deepseek")
            elif choice == "3":
                return self._setup_api_key("openai")
            elif choice == "4":
                return self._setup_api_key("anthropic")

            return False

        return SetupStep(
            id="llm",
            name="LLM Provider",
            description="Configure AI for wisdom generation",
            check_fn=check,
            action_fn=action,
            ai_prompt="What are the pros and cons of Ollama vs OpenAI for local development?",
        )

    def _step_ide_hooks(self) -> SetupStep:
        """Install IDE hooks."""
        def check():
            # Check for any hook
            claude_md = self.project_dir / "CLAUDE.md"
            cursorrules = self.project_dir / ".cursorrules"
            git_hook = self.project_dir / ".git" / "hooks" / "post-commit"

            for f in [claude_md, cursorrules, git_hook]:
                if f.exists():
                    content = f.read_text()
                    if "context-dna" in content.lower() or "Context DNA" in content:
                        return True
            return False

        def action():
            print("\n🔗 IDE Hooks will inject Context DNA into your workflow:")
            print("  - Claude Code: Adds protocol to CLAUDE.md")
            print("  - Cursor: Adds rules to .cursorrules")
            print("  - Git: Auto-learns from commits")
            print()

            if self._prompt_yes_no("Install all hooks?", default=True):
                try:
                    from context_dna.hooks.installer import HookInstaller
                    installer = HookInstaller(str(self.project_dir))
                    results = installer.install_all()

                    for hook, result in results.items():
                        status = "✅" if result["success"] else "⚠️"
                        print(f"  {status} {hook}: {result['message']}")

                    return any(r["success"] for r in results.values())
                except ImportError:
                    print("⚠️ Hook installer not available")
                    return False

            return True  # User declined but that's OK

        return SetupStep(
            id="hooks",
            name="IDE Hooks",
            description="Install automatic learning hooks",
            check_fn=check,
            action_fn=action,
            required=False,
            ai_prompt="How do Context DNA IDE hooks work? What do they inject?",
        )

    def _step_test(self) -> SetupStep:
        """Test the setup."""
        def check():
            return False  # Always run test

        def action():
            print("\n🧪 Testing Context DNA...")
            print()

            try:
                from context_dna.brain import Brain
                brain = Brain(str(self.project_dir))

                # Test basic operations
                print("  Testing win recording...")
                brain.win("Setup test", "Context DNA installed successfully")
                print("  ✅ Win recorded")

                print("  Testing query...")
                results = brain.query("setup test")
                print(f"  ✅ Query returned {len(results)} results")

                print()
                print("🎉 Context DNA is working!")
                return True

            except Exception as e:
                print(f"  ❌ Error: {e}")

                if self.llm_available:
                    print()
                    if self._prompt_yes_no("Would you like AI help troubleshooting?"):
                        guidance = self._ask_llm(
                            f"Context DNA test failed with error: {e}. How do I fix this?"
                        )
                        if guidance:
                            print("\n📝 AI Troubleshooting:")
                            print(guidance)

                return False

        return SetupStep(
            id="test",
            name="Verification",
            description="Test Context DNA is working",
            check_fn=check,
            action_fn=action,
        )

    # -------------------------------------------------------------------------
    # Provider Setup Helpers
    # -------------------------------------------------------------------------

    def _setup_ollama(self) -> bool:
        """Guide user through Ollama setup."""
        print("\n🦙 Setting up Ollama (Free Local AI)...")
        print()

        # Check if installed
        ollama_path = subprocess.run(
            ["which", "ollama"],
            capture_output=True,
        )

        if ollama_path.returncode != 0:
            print("Ollama not found. Install it first:")
            print()

            if platform.system() == "Darwin":
                print("  brew install ollama")
            else:
                print("  curl -fsSL https://ollama.ai/install.sh | sh")

            print()
            print("Then run: ollama serve")
            print("And pull a model: ollama pull llama3.2:3b")
            print()

            return self._prompt_yes_no("Continue after installing Ollama?", default=False)

        # Check if running
        try:
            import urllib.request
            req = urllib.request.Request("http://localhost:11434/api/tags")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                models = data.get("models", [])

                if models:
                    print(f"✅ Ollama running with {len(models)} model(s)")
                    for m in models[:3]:
                        print(f"   - {m['name']}")
                    return True
                else:
                    print("⚠️ Ollama running but no models installed")
                    print("   Run: ollama pull llama3.2:3b")
                    return self._prompt_yes_no("Continue?", default=True)

        except Exception:
            print("⚠️ Ollama not running")
            print("   Run: ollama serve")
            return self._prompt_yes_no("Continue?", default=False)

    def _setup_api_key(self, provider: str) -> bool:
        """Guide user through API key setup."""
        from context_dna.setup.notifications import get_api_key_setup_instructions

        print(get_api_key_setup_instructions(provider))

        env_var = f"{provider.upper()}_API_KEY"

        # Check if already set
        if os.environ.get(env_var):
            print(f"✅ {env_var} is already set")
            return True

        print()
        print("⚠️  SECURITY REMINDER:")
        print("    - Never share your API key")
        print("    - Use IDE environment variables (recommended)")
        print("    - Or add to .env file (add .env to .gitignore!)")
        print()

        choice = input("How would you like to configure? [1=IDE vars, 2=.env file]: ").strip()

        if choice == "2":
            # Help with .env file
            env_file = self.project_dir / ".env"
            gitignore = self.project_dir / ".gitignore"

            # Add to gitignore
            if gitignore.exists():
                content = gitignore.read_text()
                if ".env" not in content:
                    with open(gitignore, "a") as f:
                        f.write("\n.env\n")
                    print("✅ Added .env to .gitignore")

            print()
            print("Now add your key to .env:")
            print(f"  echo '{env_var}=your-key-here' >> .env")
            print()
            print("Then restart your terminal or run:")
            print("  source .env")

        else:
            print()
            print("Configure in your IDE settings:")
            print("  VS Code: terminal.integrated.env.osx")
            print("  Cursor: Similar to VS Code")
            print()

        return self._prompt_yes_no("Key configured?", default=True)

    def _configure_postgres(self) -> bool:
        """Configure PostgreSQL with pgvector."""
        print("\n🐘 PostgreSQL + pgvector Setup")
        print()

        # Check Docker
        if not subprocess.run(["which", "docker"], capture_output=True).returncode == 0:
            print("⚠️ Docker required for PostgreSQL setup")
            print("   Install from: https://docker.com")
            return False

        print("This will start PostgreSQL with pgvector using Docker.")
        print()

        if self._prompt_yes_no("Start PostgreSQL container?", default=True):
            # Create docker-compose for postgres
            compose = """
version: '3.8'
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: context_dna
      POSTGRES_USER: context_dna
      POSTGRES_PASSWORD: context_dna_local
    volumes:
      - context_dna_pg:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "context_dna"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  context_dna_pg:
"""
            compose_file = self.config_dir / "docker-compose.yml"
            compose_file.write_text(compose)

            result = subprocess.run(
                ["docker-compose", "-f", str(compose_file), "up", "-d"],
                capture_output=True,
            )

            if result.returncode == 0:
                print("✅ PostgreSQL started")
                print()
                print("Add to your .env:")
                print('  CONTEXT_DNA_POSTGRES_URL="postgresql://context_dna:context_dna_local@localhost:5432/context_dna"')
                return True
            else:
                print(f"❌ Failed: {result.stderr.decode()}")
                return False

        return True

    # -------------------------------------------------------------------------
    # UI Helpers
    # -------------------------------------------------------------------------

    def _print_header(self):
        """Print wizard header."""
        print()
        print("╔" + "═" * 58 + "╗")
        print("║" + " " * 58 + "║")
        print("║" + "    🧬 CONTEXT DNA SETUP WIZARD".center(58) + "║")
        print("║" + " " * 58 + "║")
        if self.llm_available:
            print("║" + f"    AI Assistant: {self._llm_provider} ✨".center(58) + "║")
        print("║" + " " * 58 + "║")
        print("╚" + "═" * 58 + "╝")
        print()

    def _print_step_start(self, step: SetupStep):
        """Print step starting."""
        print()
        print(f"━━━ {step.name} ━━━")
        print(f"    {step.description}")
        print()

    def _print_step_ok(self, step: SetupStep):
        """Print step completed."""
        print(f"  ✅ {step.name}: Complete")

    def _print_step_fail(self, step: SetupStep):
        """Print step failed."""
        print(f"  ❌ {step.name}: Failed")

    def _print_step_skip(self, step: SetupStep):
        """Print step skipped."""
        print(f"  ⏭️ {step.name}: Skipped")

    def _print_summary(self, completed: int, total: int):
        """Print final summary."""
        print()
        print("═" * 60)
        print()
        if completed == total:
            print("  🎉 Setup Complete!")
            print()
            print("  Next steps:")
            print("    context-dna status    # Check status")
            print("    context-dna consult   # Get AI wisdom")
            print("    context-dna win       # Record a win")
        else:
            print(f"  ⚠️ Setup Incomplete ({completed}/{total})")
            print()
            print("  Run 'context-dna setup' again to finish")

        print()
        print("  Dashboard: http://localhost:3456")
        print("  Docs: https://context-dna.dev/docs")
        print()

    def _prompt_yes_no(self, question: str, default: bool = True) -> bool:
        """Prompt user for yes/no answer."""
        default_str = "Y/n" if default else "y/N"
        response = input(f"{question} [{default_str}]: ").strip().lower()

        if not response:
            return default

        return response in ("y", "yes", "1", "true")


# =============================================================================
# CLI INTERFACE
# =============================================================================

def run_wizard(project_dir: str = None, quiet: bool = False) -> bool:
    """Run the setup wizard."""
    wizard = SetupWizard(project_dir, quiet)
    return wizard.run()


if __name__ == "__main__":
    success = run_wizard()
    sys.exit(0 if success else 1)
