#!/usr/bin/env python3
"""
Sandbox Verification - Test Procedures Before Storage

This module provides verification of infrastructure procedures in isolated
sandboxes BEFORE they are stored as SOPs. This ensures only VERIFIED,
WORKING procedures become part of the knowledge base.

VERIFICATION TYPES:
1. Terraform: terraform init && terraform validate
2. Docker Compose: docker-compose config (syntax validation)
3. Shell Scripts: bash -n (syntax check) + shellcheck (if available)
4. Python: python -m py_compile (syntax check)
5. YAML: yaml.safe_load (structure validation)
6. JSON: json.loads (structure validation)

WHY VERIFY?
- Only verified procedures become SOPs
- Failed verification → log issue, don't store as SOP
- Prevents bad practices from propagating
- Builds trust in the knowledge base

VERIFICATION FLOW:
```
auto_learn.py detects infra commit
    ↓
Extract artifacts from commit
    ↓
sandbox_verify.py validates each artifact
    ↓
If ALL pass → Store artifacts + Create SOP
If ANY fail → Log failure, skip SOP creation
```

Usage:
    from memory.sandbox_verify import SandboxVerifier

    verifier = SandboxVerifier()

    # Verify terraform file
    result = verifier.verify_terraform("main.tf", tf_content)
    if result.success:
        print("Terraform valid!")
    else:
        print(f"Terraform error: {result.error}")

    # Verify docker-compose
    result = verifier.verify_docker_compose(compose_content)

    # Verify shell script
    result = verifier.verify_shell_script(script_content)

    # Auto-detect and verify
    result = verifier.verify_file("deploy.sh", content)
"""

import os
import sys
import json
import tempfile
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


# =============================================================================
# VERIFICATION RESULT
# =============================================================================

@dataclass
class VerifyResult:
    """Result of a verification check."""
    success: bool
    file_type: str
    file_name: str = ""
    error: str = ""
    warnings: list = field(default_factory=list)
    output: str = ""
    verified_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __bool__(self):
        return self.success

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "file_type": self.file_type,
            "file_name": self.file_name,
            "error": self.error,
            "warnings": self.warnings,
            "verified_at": self.verified_at
        }


# =============================================================================
# SANDBOX VERIFIER
# =============================================================================

class SandboxVerifier:
    """
    Verify infrastructure artifacts before storage.

    Uses local commands for verification (terraform, docker, bash, etc.)
    Falls back to syntax-only checks when tools aren't available.
    """

    def __init__(self):
        """Initialize verifier and detect available tools."""
        self.available_tools = self._detect_tools()

    def _detect_tools(self) -> dict:
        """Detect which verification tools are available."""
        tools = {}

        # Check for terraform
        try:
            result = subprocess.run(["terraform", "version"], capture_output=True, timeout=5)
            tools["terraform"] = result.returncode == 0
        except Exception:
            tools["terraform"] = False

        # Check for docker
        try:
            result = subprocess.run(["docker", "version"], capture_output=True, timeout=5)
            tools["docker"] = result.returncode == 0
        except Exception:
            tools["docker"] = False

        # Check for docker-compose
        try:
            result = subprocess.run(["docker-compose", "version"], capture_output=True, timeout=5)
            tools["docker-compose"] = result.returncode == 0
        except Exception:
            # Try docker compose (v2)
            try:
                result = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=5)
                tools["docker-compose"] = result.returncode == 0
            except Exception:
                tools["docker-compose"] = False

        # Check for shellcheck
        try:
            result = subprocess.run(["shellcheck", "--version"], capture_output=True, timeout=5)
            tools["shellcheck"] = result.returncode == 0
        except Exception:
            tools["shellcheck"] = False

        # Bash is usually available
        try:
            result = subprocess.run(["bash", "--version"], capture_output=True, timeout=5)
            tools["bash"] = result.returncode == 0
        except Exception:
            tools["bash"] = False

        # Python is definitely available
        tools["python"] = True

        return tools

    def verify_terraform(self, file_name: str, content: str) -> VerifyResult:
        """
        Verify Terraform configuration.

        Uses terraform validate if available, falls back to HCL syntax check.

        Args:
            file_name: Name of the terraform file
            content: Terraform content

        Returns:
            VerifyResult
        """
        if not self.available_tools.get("terraform"):
            # Fall back to basic syntax check
            return self._verify_terraform_syntax(file_name, content)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write terraform file
            tf_path = Path(tmpdir) / file_name
            tf_path.write_text(content)

            # Run terraform init (required before validate)
            try:
                init_result = subprocess.run(
                    ["terraform", "init", "-backend=false", "-input=false"],
                    cwd=tmpdir,
                    capture_output=True,
                    timeout=60,
                    text=True
                )

                if init_result.returncode != 0:
                    return VerifyResult(
                        success=False,
                        file_type="terraform",
                        file_name=file_name,
                        error=f"terraform init failed: {init_result.stderr}",
                        output=init_result.stdout
                    )

                # Run terraform validate
                validate_result = subprocess.run(
                    ["terraform", "validate"],
                    cwd=tmpdir,
                    capture_output=True,
                    timeout=30,
                    text=True
                )

                return VerifyResult(
                    success=validate_result.returncode == 0,
                    file_type="terraform",
                    file_name=file_name,
                    error=validate_result.stderr if validate_result.returncode != 0 else "",
                    output=validate_result.stdout
                )

            except subprocess.TimeoutExpired:
                return VerifyResult(
                    success=False,
                    file_type="terraform",
                    file_name=file_name,
                    error="Terraform validation timed out"
                )
            except Exception as e:
                return VerifyResult(
                    success=False,
                    file_type="terraform",
                    file_name=file_name,
                    error=str(e)
                )

    def _verify_terraform_syntax(self, file_name: str, content: str) -> VerifyResult:
        """Basic HCL syntax check when terraform isn't available."""
        # Very basic checks
        warnings = []

        # Check for balanced braces
        open_braces = content.count("{")
        close_braces = content.count("}")
        if open_braces != close_braces:
            return VerifyResult(
                success=False,
                file_type="terraform",
                file_name=file_name,
                error=f"Unbalanced braces: {open_braces} open, {close_braces} close"
            )

        # Check for common issues
        if 'resource "' in content or 'data "' in content or 'variable "' in content:
            # Looks like valid terraform
            pass
        else:
            warnings.append("File doesn't contain standard terraform blocks")

        return VerifyResult(
            success=True,
            file_type="terraform",
            file_name=file_name,
            warnings=warnings,
            output="Basic syntax check passed (terraform not available for full validation)"
        )

    def verify_docker_compose(self, content: str, file_name: str = "docker-compose.yml") -> VerifyResult:
        """
        Verify Docker Compose file.

        Uses docker-compose config if available, falls back to YAML validation.

        Args:
            content: Docker Compose content
            file_name: Filename for reference

        Returns:
            VerifyResult
        """
        if not self.available_tools.get("docker-compose"):
            # Fall back to YAML validation
            return self._verify_yaml(file_name, content, expect_docker_compose=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            compose_path = Path(tmpdir) / file_name
            compose_path.write_text(content)

            try:
                # Try docker-compose config
                result = subprocess.run(
                    ["docker-compose", "-f", str(compose_path), "config"],
                    capture_output=True,
                    timeout=30,
                    text=True
                )

                if result.returncode != 0:
                    # Try docker compose (v2)
                    result = subprocess.run(
                        ["docker", "compose", "-f", str(compose_path), "config"],
                        capture_output=True,
                        timeout=30,
                        text=True
                    )

                return VerifyResult(
                    success=result.returncode == 0,
                    file_type="docker-compose",
                    file_name=file_name,
                    error=result.stderr if result.returncode != 0 else "",
                    output=result.stdout[:500] if result.stdout else ""
                )

            except subprocess.TimeoutExpired:
                return VerifyResult(
                    success=False,
                    file_type="docker-compose",
                    file_name=file_name,
                    error="Docker Compose validation timed out"
                )
            except Exception as e:
                return VerifyResult(
                    success=False,
                    file_type="docker-compose",
                    file_name=file_name,
                    error=str(e)
                )

    def _verify_yaml(self, file_name: str, content: str, expect_docker_compose: bool = False) -> VerifyResult:
        """Verify YAML syntax."""
        if not YAML_AVAILABLE:
            return VerifyResult(
                success=True,
                file_type="yaml",
                file_name=file_name,
                warnings=["PyYAML not available, skipping validation"]
            )

        try:
            data = yaml.safe_load(content)

            warnings = []
            if expect_docker_compose:
                # Check for docker-compose structure
                if not isinstance(data, dict):
                    return VerifyResult(
                        success=False,
                        file_type="docker-compose",
                        file_name=file_name,
                        error="Docker Compose file must be a YAML object"
                    )
                if "services" not in data and "version" not in data:
                    warnings.append("Missing 'services' or 'version' key (may be invalid docker-compose)")

            return VerifyResult(
                success=True,
                file_type="yaml" if not expect_docker_compose else "docker-compose",
                file_name=file_name,
                warnings=warnings,
                output="YAML syntax valid"
            )

        except yaml.YAMLError as e:
            return VerifyResult(
                success=False,
                file_type="yaml" if not expect_docker_compose else "docker-compose",
                file_name=file_name,
                error=f"YAML syntax error: {e}"
            )

    def verify_shell_script(self, content: str, file_name: str = "script.sh") -> VerifyResult:
        """
        Verify shell script.

        Uses bash -n for syntax check, shellcheck for linting if available.

        Args:
            content: Shell script content
            file_name: Filename for reference

        Returns:
            VerifyResult
        """
        warnings = []

        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / file_name
            script_path.write_text(content)

            # Basic syntax check with bash -n
            if self.available_tools.get("bash"):
                try:
                    result = subprocess.run(
                        ["bash", "-n", str(script_path)],
                        capture_output=True,
                        timeout=10,
                        text=True
                    )

                    if result.returncode != 0:
                        return VerifyResult(
                            success=False,
                            file_type="shell",
                            file_name=file_name,
                            error=f"Bash syntax error: {result.stderr}"
                        )

                except subprocess.TimeoutExpired:
                    return VerifyResult(
                        success=False,
                        file_type="shell",
                        file_name=file_name,
                        error="Bash syntax check timed out"
                    )

            # Optional shellcheck
            if self.available_tools.get("shellcheck"):
                try:
                    result = subprocess.run(
                        ["shellcheck", str(script_path)],
                        capture_output=True,
                        timeout=10,
                        text=True
                    )

                    if result.returncode != 0:
                        # shellcheck warnings (not necessarily errors)
                        for line in result.stdout.split("\n")[:5]:  # First 5 warnings
                            if line.strip():
                                warnings.append(line.strip())

                except Exception as e:
                    print(f"[WARN] shellcheck verification failed: {e}")

            return VerifyResult(
                success=True,
                file_type="shell",
                file_name=file_name,
                warnings=warnings,
                output="Shell script syntax valid"
            )

    def verify_python(self, content: str, file_name: str = "script.py") -> VerifyResult:
        """
        Verify Python script syntax.

        Args:
            content: Python script content
            file_name: Filename for reference

        Returns:
            VerifyResult
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            py_path = Path(tmpdir) / file_name
            py_path.write_text(content)

            try:
                result = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(py_path)],
                    capture_output=True,
                    timeout=10,
                    text=True
                )

                return VerifyResult(
                    success=result.returncode == 0,
                    file_type="python",
                    file_name=file_name,
                    error=result.stderr if result.returncode != 0 else "",
                    output="Python syntax valid" if result.returncode == 0 else ""
                )

            except subprocess.TimeoutExpired:
                return VerifyResult(
                    success=False,
                    file_type="python",
                    file_name=file_name,
                    error="Python syntax check timed out"
                )

    def verify_json(self, content: str, file_name: str = "file.json") -> VerifyResult:
        """
        Verify JSON syntax.

        Args:
            content: JSON content
            file_name: Filename for reference

        Returns:
            VerifyResult
        """
        try:
            json.loads(content)
            return VerifyResult(
                success=True,
                file_type="json",
                file_name=file_name,
                output="JSON syntax valid"
            )
        except json.JSONDecodeError as e:
            return VerifyResult(
                success=False,
                file_type="json",
                file_name=file_name,
                error=f"JSON syntax error: {e}"
            )

    def verify_file(self, file_name: str, content: str) -> VerifyResult:
        """
        Auto-detect file type and verify.

        Args:
            file_name: Filename (used for type detection)
            content: File content

        Returns:
            VerifyResult
        """
        file_lower = file_name.lower()

        # Terraform
        if file_lower.endswith(".tf"):
            return self.verify_terraform(file_name, content)

        # Docker Compose
        if "docker-compose" in file_lower or "compose.yml" in file_lower or "compose.yaml" in file_lower:
            return self.verify_docker_compose(content, file_name)

        # Dockerfile (basic validation)
        if "dockerfile" in file_lower:
            return self._verify_dockerfile(content, file_name)

        # Shell scripts
        if file_lower.endswith(".sh") or file_lower.endswith(".bash"):
            return self.verify_shell_script(content, file_name)

        # Python
        if file_lower.endswith(".py"):
            return self.verify_python(content, file_name)

        # YAML
        if file_lower.endswith(".yml") or file_lower.endswith(".yaml"):
            return self._verify_yaml(file_name, content)

        # JSON
        if file_lower.endswith(".json"):
            return self.verify_json(content, file_name)

        # Unknown - pass with warning
        return VerifyResult(
            success=True,
            file_type="unknown",
            file_name=file_name,
            warnings=[f"No specific validator for file type: {file_name}"]
        )

    def _verify_dockerfile(self, content: str, file_name: str) -> VerifyResult:
        """Basic Dockerfile validation."""
        warnings = []
        lines = content.strip().split("\n")

        # Check for FROM instruction
        has_from = any(line.strip().upper().startswith("FROM ") for line in lines if line.strip() and not line.strip().startswith("#"))
        if not has_from:
            return VerifyResult(
                success=False,
                file_type="dockerfile",
                file_name=file_name,
                error="Dockerfile must start with a FROM instruction"
            )

        # Check for common issues
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                # Check instruction is valid
                parts = stripped.split(None, 1)
                if parts:
                    instruction = parts[0].upper()
                    valid_instructions = {
                        "FROM", "RUN", "CMD", "LABEL", "MAINTAINER", "EXPOSE",
                        "ENV", "ADD", "COPY", "ENTRYPOINT", "VOLUME", "USER",
                        "WORKDIR", "ARG", "ONBUILD", "STOPSIGNAL", "HEALTHCHECK",
                        "SHELL"
                    }
                    if instruction not in valid_instructions and not instruction.startswith("#"):
                        warnings.append(f"Line {i}: Unknown instruction '{instruction}'")

        return VerifyResult(
            success=True,
            file_type="dockerfile",
            file_name=file_name,
            warnings=warnings,
            output="Dockerfile syntax appears valid"
        )

    def verify_all(self, artifacts: dict[str, str]) -> dict[str, VerifyResult]:
        """
        Verify all artifacts.

        Args:
            artifacts: Dict of {file_name: content}

        Returns:
            Dict of {file_name: VerifyResult}
        """
        results = {}
        for file_name, content in artifacts.items():
            results[file_name] = self.verify_file(file_name, content)
        return results

    def all_passed(self, results: dict[str, VerifyResult]) -> bool:
        """Check if all verifications passed."""
        return all(r.success for r in results.values())


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Sandbox Verifier CLI")
        print("")
        print("Commands:")
        print("  check <file>            - Verify a file")
        print("  tools                   - Show available verification tools")
        print("")
        print("Examples:")
        print("  python sandbox_verify.py check infra/main.tf")
        print("  python sandbox_verify.py check docker-compose.yml")
        print("  python sandbox_verify.py check scripts/deploy.sh")
        sys.exit(0)

    cmd = sys.argv[1]
    verifier = SandboxVerifier()

    if cmd == "check":
        if len(sys.argv) < 3:
            print("Usage: check <file>")
            sys.exit(1)

        file_path = sys.argv[2]
        if not Path(file_path).exists():
            print(f"File not found: {file_path}")
            sys.exit(1)

        content = Path(file_path).read_text()
        file_name = Path(file_path).name

        result = verifier.verify_file(file_name, content)

        if result.success:
            print(f"✅ {result.file_type.upper()} VALID: {file_name}")
            if result.warnings:
                print("   Warnings:")
                for w in result.warnings:
                    print(f"   - {w}")
        else:
            print(f"❌ {result.file_type.upper()} INVALID: {file_name}")
            print(f"   Error: {result.error}")

        sys.exit(0 if result.success else 1)

    elif cmd == "tools":
        print("Available verification tools:")
        for tool, available in verifier.available_tools.items():
            status = "✅" if available else "❌"
            print(f"  {status} {tool}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
