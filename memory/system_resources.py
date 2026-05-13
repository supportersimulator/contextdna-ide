#!/usr/bin/env python3
"""
Context DNA System Resource Detection & Adaptive Configuration

This module INTEGRATES with the existing resource profiles at:
  context-dna/infra/resource-profiles.yaml

It provides Python-level detection and recommendation that works with
the existing `context-dna resources detect|apply|status` CLI commands.

EXISTING PROFILES (from resource-profiles.yaml):
================================================
1. LIGHT (≤12GB RAM, ≤6 cores)
   - Minimal footprint for development
   - 7GB total allocation
   - 4GB Ollama limit

2. STANDARD (12-24GB RAM, 6-12 cores)  [DEFAULT]
   - Balanced for typical development
   - 14GB total allocation
   - 8GB Ollama limit

3. HEAVY (24GB+ RAM, 12+ cores)
   - Maximum performance
   - 28GB total allocation
   - 16GB Ollama limit

This module adds:
- Python API for detection (used by ecosystem_health.py, agent_service.py)
- Dashboard status endpoint
- LLM capability checking
- Integration with xbar health display

Usage:
    from memory.system_resources import (
        detect_system_specs,
        get_recommended_profile,
        get_dashboard_status,
        can_run_local_llm,
    )

    # Get recommended profile based on system
    specs = detect_system_specs()
    profile = get_recommended_profile(specs)

    # Check LLM capabilities
    llm_status = can_run_local_llm(specs, "7b")
"""

import json
import os
import platform
import subprocess
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, Any, Optional, List

# =============================================================================
# SYSTEM SPECS DETECTION
# =============================================================================

class ChipArchitecture(Enum):
    """Chip architecture for LLM recommendations."""
    APPLE_SILICON = "apple_silicon"  # M1/M2/M3/M4/M5 - Use MLX
    INTEL_MAC = "intel_mac"          # Intel Mac - Use Ollama
    INTEL_LINUX = "intel_linux"      # Intel/AMD Linux - Use Ollama
    INTEL_WINDOWS = "intel_windows"  # Intel/AMD Windows - Use Ollama
    ARM_LINUX = "arm_linux"          # ARM Linux - Use Ollama
    UNKNOWN = "unknown"


@dataclass
class SystemSpecs:
    """Detected system specifications."""
    # Hardware
    total_ram_gb: float
    available_ram_gb: float
    cpu_cores: int
    cpu_model: str

    # Platform
    os_name: str
    os_version: str
    architecture: str

    # Chip-specific detection
    chip_architecture: ChipArchitecture
    is_apple_silicon: bool
    apple_chip_generation: Optional[str]  # M1, M2, M3, M4, M5

    # GPU (if detectable)
    has_gpu: bool
    gpu_model: Optional[str]
    gpu_memory_gb: Optional[float]

    # Docker
    docker_available: bool
    docker_memory_limit_gb: Optional[float]

    # LLM Runtimes
    ollama_available: bool
    ollama_models: List[str]
    mlx_available: bool
    mlx_models: List[str]

    # Recommended LLM runtime
    recommended_llm_runtime: str  # "mlx", "ollama", "none"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # Convert enums to strings
        data['chip_architecture'] = self.chip_architecture.value
        return data


def _detect_apple_chip_generation(cpu_model: str) -> Optional[str]:
    """Extract Apple chip generation from CPU model string."""
    import re
    # Match patterns like "Apple M1", "Apple M2 Pro", "Apple M3 Max", "Apple M5"
    match = re.search(r'Apple (M\d+)', cpu_model)
    if match:
        return match.group(1)
    return None


def _detect_chip_architecture(os_name: str, cpu_model: str) -> tuple[ChipArchitecture, bool, Optional[str]]:
    """
    Detect chip architecture for LLM runtime recommendations.

    Returns: (ChipArchitecture, is_apple_silicon, apple_chip_generation)
    """
    apple_chip = _detect_apple_chip_generation(cpu_model)

    if os_name == "Darwin":  # macOS
        # Check for Apple Silicon
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.optional.arm64"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip() == "1":
                return (ChipArchitecture.APPLE_SILICON, True, apple_chip)
        except Exception as e:
            print(f"[WARN] Apple Silicon sysctl check failed: {e}")

        # Fallback: Check CPU model for Apple chip
        if apple_chip:
            return (ChipArchitecture.APPLE_SILICON, True, apple_chip)

        # Intel Mac
        return (ChipArchitecture.INTEL_MAC, False, None)

    elif os_name == "Windows":
        return (ChipArchitecture.INTEL_WINDOWS, False, None)

    elif os_name == "Linux":
        # Check architecture
        arch = platform.machine().lower()
        if "arm" in arch or "aarch64" in arch:
            return (ChipArchitecture.ARM_LINUX, False, None)
        return (ChipArchitecture.INTEL_LINUX, False, None)

    return (ChipArchitecture.UNKNOWN, False, None)


def _detect_mlx() -> tuple[bool, List[str]]:
    """Detect if MLX is available (Apple Silicon only) and list models."""
    mlx_available = False
    mlx_models = []

    try:
        # Check if mlx package is installed
        result = subprocess.run(
            ["python3", "-c", "import mlx; print('ok')"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and "ok" in result.stdout:
            mlx_available = True

        # Check for mlx-lm models (common location)
        mlx_model_dir = Path.home() / ".cache" / "huggingface" / "hub"
        if mlx_model_dir.exists():
            for item in mlx_model_dir.iterdir():
                if item.is_dir() and "mlx" in item.name.lower():
                    mlx_models.append(item.name)

        # Also check mlx-community models specifically
        result = subprocess.run(
            ["python3", "-c", "from mlx_lm import load; print('mlx_lm available')"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            mlx_available = True
    except Exception as e:
        print(f"[WARN] MLX detection failed: {e}")

    return (mlx_available, mlx_models[:10])  # Limit to 10 models


def _get_recommended_llm_runtime(
    chip_arch: ChipArchitecture,
    ollama_available: bool,
    mlx_available: bool
) -> str:
    """
    Get recommended LLM runtime based on chip architecture.

    Apple Silicon → MLX (better Metal optimization, faster inference)
    Intel/AMD → Ollama (better CPU optimization)
    """
    if chip_arch == ChipArchitecture.APPLE_SILICON:
        # MLX is preferred for Apple Silicon
        if mlx_available:
            return "mlx"
        elif ollama_available:
            return "ollama"  # Fallback
        return "mlx"  # Recommend installing

    # For non-Apple Silicon, Ollama is the standard
    if ollama_available:
        return "ollama"
    return "ollama"  # Recommend installing


def detect_system_specs() -> SystemSpecs:
    """Detect current system specifications."""

    # RAM Detection
    total_ram_gb = 0.0
    available_ram_gb = 0.0

    try:
        if platform.system() == "Darwin":  # macOS
            # Total RAM
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                total_ram_gb = int(result.stdout.strip()) / (1024**3)

            # Available RAM (approximation via vm_stat)
            result = subprocess.run(
                ["vm_stat"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                lines = result.stdout.split('\n')
                free_pages = 0
                page_size = 16384  # Default for Apple Silicon

                for line in lines:
                    if "page size" in line.lower():
                        try:
                            page_size = int(line.split()[-2])
                        except Exception as e:
                            print(f"[WARN] Page size parse failed: {e}")
                    elif "Pages free" in line:
                        try:
                            free_pages = int(line.split(':')[1].strip().rstrip('.'))
                        except Exception as e:
                            print(f"[WARN] Free pages parse failed: {e}")
                    elif "Pages inactive" in line:
                        try:
                            free_pages += int(line.split(':')[1].strip().rstrip('.'))
                        except Exception as e:
                            print(f"[WARN] Inactive pages parse failed: {e}")

                available_ram_gb = (free_pages * page_size) / (1024**3)

        elif platform.system() == "Windows":
            # Windows RAM detection
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                c_ulong = ctypes.c_ulong
                class MEMORYSTATUS(ctypes.Structure):
                    _fields_ = [
                        ('dwLength', c_ulong),
                        ('dwMemoryLoad', c_ulong),
                        ('dwTotalPhys', c_ulong),
                        ('dwAvailPhys', c_ulong),
                    ]
                mem = MEMORYSTATUS()
                kernel32.GlobalMemoryStatus(ctypes.byref(mem))
                total_ram_gb = mem.dwTotalPhys / (1024**3)
                available_ram_gb = mem.dwAvailPhys / (1024**3)
            except Exception as e:
                print(f"[WARN] Windows RAM detection failed: {e}")

        else:  # Linux
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemTotal'):
                        total_ram_gb = int(line.split()[1]) / (1024**2)
                    elif line.startswith('MemAvailable'):
                        available_ram_gb = int(line.split()[1]) / (1024**2)
    except Exception as e:
        print(f"[WARN] RAM detection failed: {e}")

    # CPU Detection
    cpu_cores = os.cpu_count() or 1
    cpu_model = "Unknown"

    try:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                cpu_model = result.stdout.strip()
        elif platform.system() == "Windows":
            result = subprocess.run(
                ["wmic", "cpu", "get", "name"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    cpu_model = lines[1].strip()
        else:  # Linux
            with open('/proc/cpuinfo') as f:
                for line in f:
                    if line.startswith('model name'):
                        cpu_model = line.split(':')[1].strip()
                        break
    except Exception as e:
        print(f"[WARN] CPU model detection failed: {e}")

    # Chip Architecture Detection
    chip_arch, is_apple_silicon, apple_chip = _detect_chip_architecture(
        platform.system(), cpu_model
    )

    # GPU Detection
    has_gpu = False
    gpu_model = None
    gpu_memory_gb = None

    if is_apple_silicon:
        has_gpu = True
        chip_name = apple_chip or "M-series"
        gpu_model = f"Apple {chip_name} (Unified Memory - Metal)"
        gpu_memory_gb = total_ram_gb  # Shared with system
    else:
        try:
            if platform.system() == "Darwin":
                # Check for discrete GPU on Intel Mac
                result = subprocess.run(
                    ["system_profiler", "SPDisplaysDataType"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and "NVIDIA" in result.stdout or "AMD" in result.stdout:
                    has_gpu = True
                    gpu_model = "Discrete GPU (Intel Mac)"
        except Exception as e:
            print(f"[WARN] GPU detection failed: {e}")

    # Docker Detection
    docker_available = False
    docker_memory_limit_gb = None

    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            docker_available = True
            info = json.loads(result.stdout)
            if info.get("MemTotal"):
                docker_memory_limit_gb = info["MemTotal"] / (1024**3)
    except Exception as e:
        print(f"[WARN] Docker detection failed: {e}")

    # Ollama Detection
    ollama_available = False
    ollama_models = []

    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            ollama_available = True
            lines = result.stdout.strip().split('\n')[1:]  # Skip header
            for line in lines:
                if line.strip():
                    model_name = line.split()[0]
                    ollama_models.append(model_name)
    except Exception as e:
        print(f"[WARN] Ollama detection failed: {e}")

    # MLX Detection (Apple Silicon only)
    mlx_available = False
    mlx_models = []
    if is_apple_silicon:
        mlx_available, mlx_models = _detect_mlx()

    # Get recommended runtime
    recommended_runtime = _get_recommended_llm_runtime(
        chip_arch, ollama_available, mlx_available
    )

    # Determine actual architecture (arm64 for Apple Silicon, not x86_64)
    actual_arch = platform.machine()
    if is_apple_silicon and actual_arch == "x86_64":
        # Running under Rosetta 2 or wrong detection
        actual_arch = "arm64"

    return SystemSpecs(
        total_ram_gb=round(total_ram_gb, 1),
        available_ram_gb=round(available_ram_gb, 1),
        cpu_cores=cpu_cores,
        cpu_model=cpu_model,
        os_name=platform.system(),
        os_version=platform.release(),
        architecture=actual_arch,
        chip_architecture=chip_arch,
        is_apple_silicon=is_apple_silicon,
        apple_chip_generation=apple_chip,
        has_gpu=has_gpu,
        gpu_model=gpu_model,
        gpu_memory_gb=gpu_memory_gb,
        docker_available=docker_available,
        docker_memory_limit_gb=docker_memory_limit_gb,
        ollama_available=ollama_available,
        ollama_models=ollama_models,
        mlx_available=mlx_available,
        mlx_models=mlx_models,
        recommended_llm_runtime=recommended_runtime,
    )


# =============================================================================
# RESOURCE PROFILES
# =============================================================================

class ResourceProfile(Enum):
    """Available resource profiles (matches resource-profiles.yaml)."""
    LIGHT = "light"        # ≤12GB RAM, ≤6 cores
    STANDARD = "standard"  # 12-24GB RAM, 6-12 cores
    HEAVY = "heavy"        # 24GB+ RAM, 12+ cores


@dataclass
class ProfileConfig:
    """Configuration for a resource profile."""
    name: str
    description: str

    # What runs
    use_containers: bool
    use_postgresql: bool
    use_redis: bool
    use_rabbitmq: bool
    use_celery_workers: bool
    use_local_llm: bool
    use_tracing: bool

    # Limits
    max_workers: int
    recommended_llm_size: str  # "none", "3b", "7b", "14b", "30b"
    injection_mode: str  # "minimal", "hybrid", "full"

    # RAM requirements
    min_ram_gb: float
    recommended_ram_gb: float

    # Messages
    dashboard_notice: str
    upgrade_message: str


PROFILE_CONFIGS: Dict[ResourceProfile, ProfileConfig] = {
    ResourceProfile.LIGHT: ProfileConfig(
        name="Light",
        description="Minimal footprint for development (≤12GB RAM systems)",
        use_containers=True,
        use_postgresql=True,
        use_redis=True,
        use_rabbitmq=True,
        use_celery_workers=False,
        use_local_llm=True,  # 4GB limit
        use_tracing=True,
        max_workers=1,
        recommended_llm_size="3b",
        injection_mode="minimal",
        min_ram_gb=8,
        recommended_ram_gb=12,
        dashboard_notice="⚡ Running in LIGHT mode (≤12GB RAM). 4GB Ollama limit.",
        upgrade_message="For larger LLM models (7B+), upgrade to 16GB+ RAM.",
    ),

    ResourceProfile.STANDARD: ProfileConfig(
        name="Standard",
        description="Balanced for typical development (12-24GB RAM systems)",
        use_containers=True,
        use_postgresql=True,
        use_redis=True,
        use_rabbitmq=True,
        use_celery_workers=True,
        use_local_llm=True,  # 8GB limit
        use_tracing=True,
        max_workers=2,
        recommended_llm_size="7b",
        injection_mode="hybrid",
        min_ram_gb=12,
        recommended_ram_gb=24,
        dashboard_notice="🔄 Running in STANDARD mode (12-24GB RAM). 8GB Ollama limit.",
        upgrade_message="For larger LLM models (14B+), upgrade to 32GB+ RAM.",
    ),

    ResourceProfile.HEAVY: ProfileConfig(
        name="Heavy",
        description="Maximum performance (24GB+ RAM systems)",
        use_containers=True,
        use_postgresql=True,
        use_redis=True,
        use_rabbitmq=True,
        use_celery_workers=True,
        use_local_llm=True,  # 16GB limit
        use_tracing=True,
        max_workers=4,
        recommended_llm_size="14b",
        injection_mode="hybrid",
        min_ram_gb=24,
        recommended_ram_gb=32,
        dashboard_notice="🚀 Running in HEAVY mode (24GB+ RAM). 16GB Ollama limit.",
        upgrade_message="You have maximum resources. Consider 14B or larger LLM models.",
    ),
}


def get_recommended_profile(specs: SystemSpecs) -> ResourceProfile:
    """
    Get recommended profile based on system specs.

    Thresholds match resource-profiles.yaml:
    - light: max_ram_gb: 12, max_cpus: 6
    - standard: max_ram_gb: 24, max_cpus: 12
    - heavy: above standard thresholds
    """
    ram = specs.total_ram_gb
    cpus = specs.cpu_cores

    # Use the more conservative estimate (RAM is usually the bottleneck)
    if ram > 24 or (ram > 20 and cpus > 12):
        return ResourceProfile.HEAVY
    elif ram > 12 or (ram > 10 and cpus > 6):
        return ResourceProfile.STANDARD
    else:
        return ResourceProfile.LIGHT


def get_profile_config(profile: ResourceProfile) -> ProfileConfig:
    """Get configuration for a profile."""
    return PROFILE_CONFIGS[profile]


def can_run_local_llm(specs: SystemSpecs, model_size: str = "7b") -> Dict[str, Any]:
    """Check if system can run a local LLM of given size."""

    # Model size requirements (approximate)
    model_requirements = {
        "3b": {"ram_gb": 4, "description": "Small, fast, basic capabilities"},
        "7b": {"ram_gb": 8, "description": "Good balance of speed and capability"},
        "14b": {"ram_gb": 16, "description": "High capability, slower"},
        "30b": {"ram_gb": 32, "description": "Maximum capability, requires significant RAM"},
        "70b": {"ram_gb": 64, "description": "State-of-the-art, requires enterprise hardware"},
    }

    req = model_requirements.get(model_size, model_requirements["7b"])
    available = specs.available_ram_gb
    total = specs.total_ram_gb

    # Need model RAM + ~4GB for Context DNA + ~4GB for system
    required = req["ram_gb"] + 8

    can_run = total >= required
    recommended = available >= req["ram_gb"]

    return {
        "can_run": can_run,
        "recommended": recommended,
        "model_size": model_size,
        "required_ram_gb": required,
        "available_ram_gb": available,
        "total_ram_gb": total,
        "model_description": req["description"],
        "warning": None if can_run else f"Insufficient RAM. Need {required}GB total, have {total}GB.",
        "recommendation": (
            f"✅ {model_size} model fits comfortably" if recommended else
            f"⚠️ {model_size} model may cause swapping. Consider smaller model." if can_run else
            f"❌ Cannot run {model_size} model. Need more RAM."
        ),
    }


# =============================================================================
# PROFILE STATE MANAGEMENT
# =============================================================================

PROFILE_STATE_FILE = Path(__file__).parent / ".resource_profile.json"


def save_active_profile(profile: ResourceProfile, specs: SystemSpecs):
    """Save the active profile and detected specs."""
    data = {
        "profile": profile.value,
        "specs": specs.to_dict(),
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }
    with open(PROFILE_STATE_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def load_active_profile() -> Optional[Dict[str, Any]]:
    """Load the active profile state."""
    if PROFILE_STATE_FILE.exists():
        try:
            with open(PROFILE_STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Active profile load failed: {e}")
    return None


def get_dashboard_status() -> Dict[str, Any]:
    """Get status for dashboard display."""
    specs = detect_system_specs()
    profile = get_recommended_profile(specs)
    config = get_profile_config(profile)
    llm_status = can_run_local_llm(specs, config.recommended_llm_size)

    return {
        "system": {
            "total_ram_gb": specs.total_ram_gb,
            "available_ram_gb": specs.available_ram_gb,
            "cpu_cores": specs.cpu_cores,
            "cpu_model": specs.cpu_model,
            "os": f"{specs.os_name} {specs.os_version}",
            "architecture": specs.architecture,
            "has_gpu": specs.has_gpu,
            "gpu_model": specs.gpu_model,
            "docker_available": specs.docker_available,
        },
        "chip": {
            "architecture": specs.chip_architecture.value,
            "is_apple_silicon": specs.is_apple_silicon,
            "apple_chip_generation": specs.apple_chip_generation,
            "display_name": _get_chip_display_name(specs),
        },
        "llm_runtime": {
            "recommended": specs.recommended_llm_runtime,
            "ollama_available": specs.ollama_available,
            "ollama_models": specs.ollama_models,
            "mlx_available": specs.mlx_available,
            "mlx_models": specs.mlx_models,
            "install_instructions": _get_llm_install_instructions(specs),
        },
        "profile": {
            "name": config.name,
            "value": profile.value,
            "description": config.description,
            "dashboard_notice": config.dashboard_notice,
            "upgrade_message": config.upgrade_message,
        },
        "capabilities": {
            "containers": config.use_containers,
            "postgresql": config.use_postgresql,
            "redis": config.use_redis,
            "celery": config.use_celery_workers,
            "local_llm": config.use_local_llm,
            "tracing": config.use_tracing,
            "max_workers": config.max_workers,
            "recommended_llm": config.recommended_llm_size,
            "injection_mode": config.injection_mode,
        },
        "llm": llm_status,
        "requirements": {
            "minimal": "≤8GB RAM - File-based only",
            "standard": "8-16GB RAM - PostgreSQL + Redis",
            "full": "16-32GB RAM - All containers + 7B LLM",
            "heavy": "32GB+ RAM - Full stack + 14B LLM",
        },
    }


def _get_chip_display_name(specs: SystemSpecs) -> str:
    """Get human-readable chip display name."""
    if specs.is_apple_silicon and specs.apple_chip_generation:
        return f"Apple {specs.apple_chip_generation}"
    elif specs.is_apple_silicon:
        return "Apple Silicon"
    elif specs.chip_architecture == ChipArchitecture.INTEL_MAC:
        return "Intel Mac"
    elif specs.chip_architecture == ChipArchitecture.INTEL_WINDOWS:
        return "Intel/AMD (Windows)"
    elif specs.chip_architecture == ChipArchitecture.INTEL_LINUX:
        return "Intel/AMD (Linux)"
    elif specs.chip_architecture == ChipArchitecture.ARM_LINUX:
        return "ARM (Linux)"
    return "Unknown"


def _get_llm_install_instructions(specs: SystemSpecs) -> str:
    """Get LLM runtime install instructions based on chip."""
    if specs.is_apple_silicon:
        if not specs.mlx_available:
            return "pip install mlx mlx-lm  # Recommended for Apple Silicon"
        return "MLX installed ✅"
    else:
        if not specs.ollama_available:
            return "Install Ollama from https://ollama.ai"
        return "Ollama installed ✅"


# =============================================================================
# CLI
# =============================================================================

def print_status():
    """Print formatted status for CLI."""
    status = get_dashboard_status()

    print("=" * 70)
    print("  CONTEXT DNA SYSTEM RESOURCES")
    print("=" * 70)

    # System Info
    print("\n📊 SYSTEM SPECIFICATIONS")
    print("-" * 40)
    sys_info = status["system"]
    print(f"  RAM: {sys_info['total_ram_gb']}GB total, {sys_info['available_ram_gb']}GB available")
    print(f"  CPU: {sys_info['cpu_cores']} cores - {sys_info['cpu_model'][:50]}")
    print(f"  OS:  {sys_info['os']} ({sys_info['architecture']})")

    # Chip Architecture
    chip = status["chip"]
    chip_icon = "🍎" if chip["is_apple_silicon"] else "💻"
    chip_display = chip["apple_chip_generation"] or chip["architecture"].replace("_", " ").title()
    print(f"  Chip: {chip_icon} {chip_display}")

    print(f"  GPU: {'✅ ' + sys_info['gpu_model'] if sys_info['has_gpu'] else '❌ Not detected'}")
    print(f"  Docker: {'✅ Available' if sys_info['docker_available'] else '❌ Not installed'}")

    # LLM Runtimes
    print("\n🦙 LLM RUNTIMES")
    print("-" * 40)
    llm_runtime = status["llm_runtime"]
    ollama_status = "✅ Installed" if llm_runtime["ollama_available"] else "❌ Not installed"
    mlx_status = "✅ Installed" if llm_runtime["mlx_available"] else "❌ Not installed"

    if chip["is_apple_silicon"]:
        # Apple Silicon: MLX recommended
        print(f"  MLX (Recommended): {mlx_status}")
        print(f"  Ollama (Fallback): {ollama_status}")
        if not llm_runtime["mlx_available"]:
            print("  💡 Install MLX: pip install mlx mlx-lm")
            print("     MLX is optimized for Apple Silicon Metal GPUs")
    else:
        # Intel/AMD: Ollama recommended
        print(f"  Ollama (Recommended): {ollama_status}")
        if mlx_status:
            print(f"  MLX: ❌ Not supported (requires Apple Silicon)")
        if not llm_runtime["ollama_available"]:
            print("  💡 Install Ollama: https://ollama.ai")

    print(f"\n  ⭐ Recommended Runtime: {llm_runtime['recommended'].upper()}")

    if llm_runtime["ollama_models"]:
        print(f"  Ollama Models: {', '.join(llm_runtime['ollama_models'][:3])}")
    if llm_runtime["mlx_models"]:
        print(f"  MLX Models: {', '.join(llm_runtime['mlx_models'][:3])}")

    # Profile
    print(f"\n🎯 RECOMMENDED PROFILE: {status['profile']['name'].upper()}")
    print("-" * 40)
    print(f"  {status['profile']['description']}")
    print(f"\n  {status['profile']['dashboard_notice']}")
    print(f"\n  💡 {status['profile']['upgrade_message']}")

    # Capabilities
    print("\n⚡ ENABLED CAPABILITIES")
    print("-" * 40)
    caps = status["capabilities"]
    print(f"  PostgreSQL: {'✅' if caps['postgresql'] else '❌'}")
    print(f"  Redis: {'✅' if caps['redis'] else '❌'}")
    print(f"  Celery Workers: {'✅' if caps['celery'] else '❌'} (max: {caps['max_workers']})")
    print(f"  Local LLM: {'✅' if caps['local_llm'] else '❌'} (recommended: {caps['recommended_llm']})")
    print(f"  Tracing: {'✅' if caps['tracing'] else '❌'}")
    print(f"  Injection Mode: {caps['injection_mode']}")

    # LLM Recommendation
    print("\n🦙 LOCAL LLM STATUS")
    print("-" * 40)
    llm = status["llm"]
    print(f"  {llm['recommendation']}")
    if llm['warning']:
        print(f"  ⚠️  {llm['warning']}")

    # Requirements Reference
    print("\n📋 PROFILE REQUIREMENTS")
    print("-" * 40)
    for name, req in status["requirements"].items():
        marker = "→" if name == status["profile"]["value"] else " "
        print(f"  {marker} {name.upper()}: {req}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == "--json":
            print(json.dumps(get_dashboard_status(), indent=2))
        elif sys.argv[1] == "--specs":
            specs = detect_system_specs()
            print(json.dumps(specs.to_dict(), indent=2))
        elif sys.argv[1] == "--profile":
            specs = detect_system_specs()
            profile = get_recommended_profile(specs)
            config = get_profile_config(profile)
            print(f"Profile: {profile.value}")
            print(f"Containers: {config.use_containers}")
            print(f"Local LLM: {config.use_local_llm}")
        else:
            print_status()
    else:
        print_status()
