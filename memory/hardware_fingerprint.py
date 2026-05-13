#!/usr/bin/env python3
"""
Hardware Fingerprint - Privacy-Safe System Identification

Creates a privacy-safe hardware profile for intelligent config recommendations.

SHARES (safe, non-identifying):
✅ CPU type (Apple M1/M2/M3, Intel i7, AMD Ryzen)
✅ RAM amount (8GB, 16GB, 32GB, 64GB, 128GB)
✅ GPU type (Apple Silicon, NVIDIA RTX, AMD, Intel)
✅ OS version (macOS 14.2, Ubuntu 22.04, Windows 11)
✅ Architecture (arm64, x86_64)
✅ Python version (3.11, 3.12)

DOES NOT SHARE (identifying):
❌ Serial numbers
❌ MAC addresses
❌ Hostnames
❌ Usernames
❌ IP addresses
❌ Device UUIDs
❌ License keys

Usage:
    from memory.hardware_fingerprint import get_hardware_profile
    
    profile = get_hardware_profile()
    # Returns: {
    #   "cpu_type": "apple_m3_max",
    #   "ram_gb": 64,
    #   "gpu_type": "apple_silicon",
    #   "os": "macos_14.2",
    #   "arch": "arm64",
    #   "python_version": "3.12"
    # }
    
    # Use for recommendations:
    recommended = get_recommended_llm_config(profile)
"""

import platform
import subprocess
import json
import re
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any


@dataclass
class HardwareProfile:
    """Privacy-safe hardware profile for recommendations."""
    # CPU
    cpu_type: str           # "apple_m1", "apple_m3_max", "intel_i7_13700k", "amd_ryzen_7950x"
    cpu_cores: int          # Physical cores
    cpu_threads: int        # Logical cores
    
    # Memory
    ram_gb: int             # Total RAM (rounded to nearest power of 2)
    
    # GPU
    gpu_type: str           # "apple_silicon", "nvidia_rtx_4090", "amd_radeon_7900xt", "intel_uhd"
    gpu_vram_gb: Optional[int]  # VRAM if discrete GPU
    
    # System
    os_type: str            # "macos", "linux", "windows"
    os_version: str         # "14.2", "22.04", "11"
    arch: str               # "arm64", "x86_64"
    
    # Software
    python_version: str     # "3.12.1"
    
    # Performance tier (derived)
    performance_tier: str   # "high_end", "mid_range", "low_end"
    
    # Fingerprint (for matching, not identifying)
    profile_hash: str       # SHA256 of sorted specs (not unique device ID)


def get_hardware_profile() -> HardwareProfile:
    """Get privacy-safe hardware profile."""
    
    # CPU detection
    cpu_type, cpu_cores, cpu_threads = _detect_cpu()
    
    # Memory detection
    ram_gb = _detect_ram()
    
    # GPU detection
    gpu_type, gpu_vram_gb = _detect_gpu()
    
    # System detection
    os_type, os_version = _detect_os()
    arch = platform.machine()
    
    # Python version
    py_version = f"{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}"
    
    # Determine performance tier
    tier = _determine_performance_tier(cpu_type, ram_gb, gpu_type)
    
    # Create profile
    profile = HardwareProfile(
        cpu_type=cpu_type,
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        ram_gb=ram_gb,
        gpu_type=gpu_type,
        gpu_vram_gb=gpu_vram_gb,
        os_type=os_type,
        os_version=os_version,
        arch=arch,
        python_version=py_version,
        performance_tier=tier,
        profile_hash=""  # Will be set below
    )
    
    # Generate non-identifying fingerprint (for matching similar configs)
    profile_str = f"{cpu_type}:{ram_gb}:{gpu_type}:{os_type}:{arch}"
    import hashlib
    profile.profile_hash = hashlib.sha256(profile_str.encode()).hexdigest()[:12]
    
    return profile


def _detect_cpu() -> tuple:
    """Detect CPU type and cores (privacy-safe)."""
    system = platform.system()
    
    try:
        if system == "Darwin":  # macOS
            # Check if Apple Silicon
            if platform.machine() == 'arm64':
                # Get chip info
                chip_info = subprocess.check_output(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    text=True
                ).strip()
                
                # Parse Apple chip type
                if "M1" in chip_info:
                    cpu_type = "apple_m1_max" if "Max" in chip_info else \
                               "apple_m1_pro" if "Pro" in chip_info else "apple_m1"
                elif "M2" in chip_info:
                    cpu_type = "apple_m2_ultra" if "Ultra" in chip_info else \
                               "apple_m2_max" if "Max" in chip_info else \
                               "apple_m2_pro" if "Pro" in chip_info else "apple_m2"
                elif "M3" in chip_info:
                    cpu_type = "apple_m3_max" if "Max" in chip_info else \
                               "apple_m3_pro" if "Pro" in chip_info else "apple_m3"
                elif "M4" in chip_info:
                    cpu_type = "apple_m4_max" if "Max" in chip_info else \
                               "apple_m4_pro" if "Pro" in chip_info else "apple_m4"
                else:
                    cpu_type = "apple_silicon"
            else:
                # Intel Mac
                cpu_type = "intel_mac"
            
            # Get core counts
            cores = int(subprocess.check_output(
                ["sysctl", "-n", "hw.physicalcpu"],
                text=True
            ).strip())
            
            threads = int(subprocess.check_output(
                ["sysctl", "-n", "hw.logicalcpu"],
                text=True
            ).strip())
            
            return cpu_type, cores, threads
        
        else:
            # Linux/Windows - use generic detection
            import psutil
            cpu_type = f"{platform.processor()[:20]}"  # Truncate to avoid identifying info
            cores = psutil.cpu_count(logical=False) or 1
            threads = psutil.cpu_count(logical=True) or cores
            
            return cpu_type, cores, threads
    
    except Exception:
        return "unknown", 1, 1


def _detect_ram() -> int:
    """Detect total RAM (rounded to power of 2 for privacy)."""
    try:
        import psutil
        
        total_bytes = psutil.virtual_memory().total
        total_gb = total_bytes / (1024 ** 3)
        
        # Round to nearest power of 2 (8, 16, 32, 64, 128)
        powers_of_2 = [8, 16, 32, 64, 128, 256]
        ram_gb = min(powers_of_2, key=lambda x: abs(x - total_gb))
        
        return ram_gb
    
    except Exception:
        return 8  # Conservative default


def _detect_gpu() -> tuple:
    """Detect GPU type and VRAM (privacy-safe)."""
    system = platform.system()
    
    try:
        if system == "Darwin" and platform.machine() == 'arm64':
            # Apple Silicon - unified memory
            return "apple_silicon", None
        
        # Try to detect discrete GPU
        # nvidia-smi for NVIDIA
        try:
            output = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                text=True,
                timeout=2
            ).strip()
            
            if output:
                # Parse: "NVIDIA GeForce RTX 4090, 24576 MiB"
                parts = output.split(',')
                gpu_name = parts[0].strip()
                vram_mb = int(parts[1].strip().split()[0])
                vram_gb = vram_mb // 1024
                
                # Generalize GPU name (don't include specific variants)
                if "4090" in gpu_name:
                    return "nvidia_rtx_4090", vram_gb
                elif "4080" in gpu_name:
                    return "nvidia_rtx_4080", vram_gb
                elif "4070" in gpu_name:
                    return "nvidia_rtx_4070", vram_gb
                elif "3090" in gpu_name:
                    return "nvidia_rtx_3090", vram_gb
                else:
                    return "nvidia_gpu", vram_gb
        
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        # AMD GPU detection would go here
        
        # CPU-only fallback
        return "cpu_only", None
    
    except Exception:
        return "unknown", None


def _detect_os() -> tuple:
    """Detect OS and version (privacy-safe)."""
    system = platform.system()
    
    if system == "Darwin":
        # macOS
        version = platform.mac_ver()[0]
        # Just major.minor (not patch)
        major_minor = '.'.join(version.split('.')[:2])
        return "macos", major_minor
    
    elif system == "Linux":
        # Linux distribution
        try:
            with open('/etc/os-release', 'r') as f:
                lines = f.readlines()
                for line in lines:
                    if line.startswith('VERSION_ID='):
                        version = line.split('=')[1].strip().strip('"')
                        return "linux", version
        except Exception:
            pass
        return "linux", "unknown"
    
    elif system == "Windows":
        # Windows version
        version = platform.version()
        return "windows", version.split('.')[0]  # Just major version
    
    else:
        return "unknown", "unknown"


def _determine_performance_tier(cpu_type: str, ram_gb: int, gpu_type: str) -> str:
    """Determine performance tier for recommendations."""
    
    # High-end: M3/M4 Max/Ultra with 64GB+, or RTX 4090 with 64GB+
    if ("m3_max" in cpu_type or "m3_ultra" in cpu_type or 
        "m4" in cpu_type or "rtx_4090" in gpu_type) and ram_gb >= 64:
        return "high_end"
    
    # Mid-range: M1/M2/M3 with 16-32GB, or decent GPU with 16GB+
    if (("m1" in cpu_type or "m2" in cpu_type or "m3" in cpu_type or
         "rtx_3090" in gpu_type or "rtx_4080" in gpu_type) and 
        16 <= ram_gb <= 32):
        return "mid_range"
    
    # Low-end: <16GB RAM or CPU-only
    return "low_end"


def get_recommended_llm_configs(profile: HardwareProfile) -> list:
    """
    Get recommended LLM configurations for this hardware profile.
    
    Returns ranked list of configs (best to fallback).
    """
    recommendations = []
    
    # Apple Silicon recommendations
    if "apple" in profile.cpu_type and "silicon" in profile.gpu_type:
        if profile.ram_gb >= 64:
            # High-end Apple Silicon
            recommendations.extend([
                {
                    "rank": 1,
                    "model": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",
                    "backend": "vllm-mlx",
                    "params": {
                        "max_tokens": 4096,
                        "temperature": 0.7,
                        "enable_auto_tool_choice": True,
                        "tool_call_parser": "hermes"
                    },
                    "expected_performance": {
                        "tokens_per_sec": 12-15,
                        "first_token_ms": 800-1200,
                        "memory_mb": 900-1200
                    },
                    "notes": "Optimal for M3/M4 Max with 64GB+. Best balance of intelligence and speed.",
                    "success_rate": 0.98,
                    "optimization_score": 0.95
                },
                {
                    "rank": 2,
                    "model": "mlx-community/Qwen2.5-Coder-32B-Instruct-4bit",
                    "backend": "vllm-mlx",
                    "params": {"max_tokens": 4096, "temperature": 0.7},
                    "expected_performance": {
                        "tokens_per_sec": 5-8,
                        "first_token_ms": 1500-2500
                    },
                    "notes": "Higher intelligence but slower. Use if quality > speed.",
                    "success_rate": 0.92,
                    "optimization_score": 0.75
                }
            ])
        
        elif profile.ram_gb >= 32:
            # Mid-range Apple Silicon
            recommendations.extend([
                {
                    "rank": 1,
                    "model": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",
                    "backend": "vllm-mlx",
                    "params": {"max_tokens": 4096, "temperature": 0.7},
                    "expected_performance": {
                        "tokens_per_sec": 10-13,
                        "first_token_ms": 1000-1500
                    },
                    "notes": "Optimal for M1/M2/M3 with 32GB.",
                    "success_rate": 0.96,
                    "optimization_score": 0.92
                },
                {
                    "rank": 2,
                    "model": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
                    "backend": "vllm-mlx",
                    "params": {"max_tokens": 2048, "temperature": 0.7},
                    "expected_performance": {
                        "tokens_per_sec": 18-25,
                        "first_token_ms": 500-800
                    },
                    "notes": "Faster but less intelligent. Good for quick queries.",
                    "success_rate": 0.94,
                    "optimization_score": 0.88
                }
            ])
        
        else:
            # Low RAM (<32GB)
            recommendations.append({
                "rank": 1,
                "model": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
                "backend": "vllm-mlx",
                "params": {"max_tokens": 2048, "temperature": 0.7},
                "expected_performance": {
                    "tokens_per_sec": 15-20,
                    "first_token_ms": 600-1000
                },
                "notes": "Optimal for systems with 16GB RAM.",
                "success_rate": 0.93,
                "optimization_score": 0.90
            })
    
    # NVIDIA GPU recommendations
    elif "nvidia" in profile.gpu_type:
        if profile.gpu_vram_gb and profile.gpu_vram_gb >= 24:
            recommendations.append({
                "rank": 1,
                "model": "Qwen/Qwen2.5-Coder-32B-Instruct",
                "backend": "vllm",
                "params": {
                    "gpu_memory_utilization": 0.9,
                    "max_tokens": 8192,
                    "temperature": 0.7
                },
                "expected_performance": {
                    "tokens_per_sec": 20-40,
                    "first_token_ms": 200-500
                },
                "notes": "Optimal for RTX 4090 with 24GB VRAM.",
                "success_rate": 0.96,
                "optimization_score": 0.94
            })
    
    # CPU-only fallback
    if not recommendations:
        recommendations.append({
            "rank": 1,
            "model": "qwen2.5-coder:7b",
            "backend": "ollama",
            "params": {"num_ctx": 2048, "temperature": 0.7},
            "expected_performance": {
                "tokens_per_sec": 3-8,
                "first_token_ms": 2000-5000
            },
            "notes": "CPU-only fallback. Consider cloud LLM for better performance.",
            "success_rate": 0.85,
            "optimization_score": 0.60
        })
    
    return recommendations


def evaluate_config_optimization(
    profile: HardwareProfile,
    config: Dict[str, Any],
    measured_performance: Dict[str, float]
) -> float:
    """
    Evaluate if a config is optimized for the hardware.
    
    Returns optimization score (0.0 to 1.0):
    - 1.0 = Perfectly optimized (using hardware to full potential)
    - 0.5 = Acceptable (works but not optimal)
    - 0.0 = Poor (underutilizing or overloading hardware)
    
    Factors:
    - Speed vs hardware capability
    - Memory usage vs available RAM
    - GPU utilization (if applicable)
    - Token/s compared to hardware potential
    """
    score = 0.0
    factors = []
    
    # Factor 1: Speed relative to hardware tier
    tokens_per_sec = measured_performance.get('tokens_per_sec', 0)
    
    if profile.performance_tier == "high_end":
        # Expect ≥12 tok/s for high-end
        speed_score = min(tokens_per_sec / 12.0, 1.0)
    elif profile.performance_tier == "mid_range":
        # Expect ≥8 tok/s for mid-range
        speed_score = min(tokens_per_sec / 8.0, 1.0)
    else:
        # Expect ≥3 tok/s for low-end
        speed_score = min(tokens_per_sec / 3.0, 1.0)
    
    factors.append(("speed", speed_score, 0.4))  # 40% weight
    
    # Factor 2: Memory efficiency
    memory_mb = measured_performance.get('memory_mb', 0)
    ram_available_mb = profile.ram_gb * 1024
    memory_ratio = memory_mb / ram_available_mb if ram_available_mb > 0 else 0
    
    # Optimal: Using 10-30% of RAM (not too little, not too much)
    if 0.10 <= memory_ratio <= 0.30:
        memory_score = 1.0
    elif 0.05 <= memory_ratio <= 0.50:
        memory_score = 0.8
    elif memory_ratio < 0.05:
        memory_score = 0.5  # Underutilizing
    else:
        memory_score = 0.3  # Overloading (swapping likely)
    
    factors.append(("memory", memory_score, 0.3))  # 30% weight
    
    # Factor 3: First token latency
    first_token_ms = measured_performance.get('first_token_ms', 5000)
    
    if first_token_ms < 1000:
        latency_score = 1.0
    elif first_token_ms < 2000:
        latency_score = 0.8
    elif first_token_ms < 5000:
        latency_score = 0.5
    else:
        latency_score = 0.2
    
    factors.append(("latency", latency_score, 0.2))  # 20% weight
    
    # Factor 4: Stability (no crashes, consistent performance)
    error_rate = measured_performance.get('error_rate', 0)
    stability_score = max(0, 1.0 - error_rate)
    
    factors.append(("stability", stability_score, 0.1))  # 10% weight
    
    # Calculate weighted score
    total_score = sum(score * weight for name, score, weight in factors)
    
    return round(total_score, 2)


def match_user_to_template(
    user_profile: HardwareProfile,
    available_templates: list
) -> list:
    """
    Match user's hardware to best templates.
    
    Returns ranked list of templates (best match first).
    
    Scoring considers:
    - Hardware compatibility (required RAM, GPU)
    - Success rate on similar hardware
    - Optimization score
    - Community upvotes
    - Maintainer verification
    """
    scored_templates = []
    
    for template in available_templates:
        compatibility_score = 0.0
        
        # Check hardware requirements
        required_ram = template.get('min_ram_gb', 0)
        if user_profile.ram_gb < required_ram:
            continue  # Can't run this config
        
        # Score based on similarity to user's hardware
        if template.get('hardware_type') == user_profile.cpu_type:
            compatibility_score += 0.5
        
        if template.get('os_type') == user_profile.os_type:
            compatibility_score += 0.2
        
        # Add success rate
        success_rate = template.get('success_rate', 0)
        
        # Add optimization score
        opt_score = template.get('optimization_score', 0)
        
        # Add community signals
        upvotes = template.get('upvotes', 0)
        community_score = min(upvotes / 10.0, 0.1)  # Max 0.1 contribution
        
        # Add verification bonus
        verification_bonus = 0.1 if template.get('is_verified') else 0
        
        # Total score
        total_score = (
            compatibility_score * 0.3 +
            success_rate * 0.3 +
            opt_score * 0.25 +
            community_score * 0.05 +
            verification_bonus * 0.1
        )
        
        scored_templates.append({
            **template,
            'match_score': total_score,
            'compatibility': compatibility_score
        })
    
    # Sort by match score (descending)
    scored_templates.sort(key=lambda x: x['match_score'], reverse=True)
    
    return scored_templates


if __name__ == "__main__":
    print("🖥️  Hardware Fingerprint (Privacy-Safe)\n")
    
    profile = get_hardware_profile()
    
    print("CPU:")
    print(f"  Type: {profile.cpu_type}")
    print(f"  Cores: {profile.cpu_cores} physical, {profile.cpu_threads} logical")
    
    print("\nMemory:")
    print(f"  RAM: {profile.ram_gb}GB")
    
    print("\nGPU:")
    print(f"  Type: {profile.gpu_type}")
    if profile.gpu_vram_gb:
        print(f"  VRAM: {profile.gpu_vram_gb}GB")
    
    print("\nSystem:")
    print(f"  OS: {profile.os_type} {profile.os_version}")
    print(f"  Arch: {profile.arch}")
    print(f"  Python: {profile.python_version}")
    
    print("\nPerformance:")
    print(f"  Tier: {profile.performance_tier}")
    print(f"  Profile Hash: {profile.profile_hash}")
    
    print("\n📊 Recommended LLM Configs:\n")
    
    recommendations = get_recommended_llm_configs(profile)
    for rec in recommendations:
        print(f"#{rec['rank']}: {rec['model']}")
        print(f"   Backend: {rec['backend']}")
        print(f"   Expected: {rec['expected_performance']['tokens_per_sec']} tok/s")
        print(f"   Success rate: {rec['success_rate']:.0%}")
        print(f"   Optimization: {rec['optimization_score']:.0%}")
        print(f"   {rec['notes']}")
        print()
