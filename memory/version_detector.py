#!/usr/bin/env python3
"""
Version Detector - Detect Installed App/IDE Versions

Detects exact versions of installed IDEs and apps for version-specific
template matching.

Usage:
    from memory.version_detector import detect_ide_version
    
    cursor_version = detect_ide_version("cursor")
    # Returns: "1.5.2"
    
    # Match to compatible template
    template = get_template_for_version("cursor", "macos", "1.5.2")
"""

import json
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Optional


def detect_ide_version(ide_family: str) -> Optional[str]:
    """
    Detect installed version of an IDE.
    
    Args:
        ide_family: "cursor", "vscode", "pycharm", etc.
    
    Returns:
        Version string like "1.5.2" or None if not detected
    """
    if ide_family == "cursor":
        return _detect_cursor_version()
    elif ide_family == "vscode":
        return _detect_vscode_version()
    elif ide_family == "claude":
        return _detect_claude_code_version()
    elif ide_family == "pycharm":
        return _detect_pycharm_version()
    elif ide_family == "windsurf":
        return _detect_windsurf_version()
    else:
        return None


def _detect_cursor_version() -> Optional[str]:
    """Detect Cursor version."""
    # Method 1: Check Info.plist (macOS)
    info_plist = Path("/Applications/Cursor.app/Contents/Info.plist")
    if info_plist.exists():
        try:
            with open(info_plist, 'rb') as f:
                plist_data = plistlib.load(f)
                version = plist_data.get('CFBundleShortVersionString')
                if version:
                    return version
        except Exception:
            pass
    
    # Method 2: Check version file in config
    version_file = Path.home() / '.cursor' / 'version.txt'
    if version_file.exists():
        try:
            return version_file.read_text().strip()
        except Exception:
            pass
    
    # Method 3: Check package.json
    package_json = Path.home() / '.cursor' / 'package.json'
    if package_json.exists():
        try:
            with open(package_json) as f:
                data = json.load(f)
                return data.get('version')
        except Exception:
            pass
    
    return None


def _detect_vscode_version() -> Optional[str]:
    """Detect VS Code version."""
    # macOS
    info_plist = Path("/Applications/Visual Studio Code.app/Contents/Info.plist")
    if info_plist.exists():
        try:
            with open(info_plist, 'rb') as f:
                plist_data = plistlib.load(f)
                return plist_data.get('CFBundleShortVersionString')
        except Exception:
            pass
    
    # Linux/Windows - try command
    try:
        result = subprocess.run(
            ['code', '--version'],
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.returncode == 0:
            # First line is version
            return result.stdout.strip().split('\n')[0]
    except Exception:
        pass
    
    return None


def _detect_claude_code_version() -> Optional[str]:
    """Detect Claude Code extension version."""
    # Check extensions folder
    extensions_dir = Path.home() / '.vscode' / 'extensions'
    
    if extensions_dir.exists():
        # Find claude-code extension
        for ext_dir in extensions_dir.glob('anthropics.claude-code-*'):
            # Version is in directory name: anthropics.claude-code-1.2.3
            match = re.search(r'claude-code-(\d+\.\d+\.\d+)', ext_dir.name)
            if match:
                return match.group(1)
    
    return None


def _detect_pycharm_version() -> Optional[str]:
    """Detect PyCharm version."""
    # macOS
    info_plist = Path("/Applications/PyCharm.app/Contents/Info.plist")
    if info_plist.exists():
        try:
            with open(info_plist, 'rb') as f:
                plist_data = plistlib.load(f)
                return plist_data.get('CFBundleShortVersionString')
        except Exception:
            pass
    
    return None


def _detect_windsurf_version() -> Optional[str]:
    """Detect Windsurf version."""
    # Similar to Cursor detection
    info_plist = Path("/Applications/Windsurf.app/Contents/Info.plist")
    if info_plist.exists():
        try:
            with open(info_plist, 'rb') as f:
                plist_data = plistlib.load(f)
                return plist_data.get('CFBundleShortVersionString')
        except Exception:
            pass
    
    return None


def get_all_ide_versions() -> dict:
    """Get versions of all detected IDEs."""
    ides = ['cursor', 'vscode', 'claude', 'pycharm', 'windsurf']
    
    versions = {}
    for ide in ides:
        version = detect_ide_version(ide)
        if version:
            versions[ide] = version
    
    return versions


def check_version_compatibility(
    template_min: Optional[str],
    template_max: Optional[str],
    user_version: str
) -> bool:
    """
    Check if user's version is compatible with template.
    
    Args:
        template_min: Minimum version required (e.g., "1.5.0")
        template_max: Maximum version tested (e.g., "1.6.0")
        user_version: User's installed version (e.g., "1.5.2")
    
    Returns:
        True if compatible
    """
    try:
        user_ver = parse_version(user_version)
        
        # Check minimum
        if template_min:
            min_ver = parse_version(template_min)
            if user_ver < min_ver:
                return False
        
        # Check maximum (warning, not blocking)
        if template_max:
            max_ver = parse_version(template_max)
            if user_ver > max_ver:
                # User has newer version than tested
                # Still try (might work) but warn
                return True
        
        return True
    
    except Exception:
        # If version parsing fails, assume compatible
        return True


def parse_version(version_str: str) -> tuple:
    """Parse version string to tuple for comparison."""
    # "1.5.2" → (1, 5, 2)
    parts = version_str.split('.')
    return tuple(int(p) for p in parts if p.isdigit())


if __name__ == "__main__":
    print("🔍 IDE Version Detector\n")
    
    versions = get_all_ide_versions()
    
    if versions:
        print("Detected IDE versions:")
        for ide, version in versions.items():
            print(f"  ✅ {ide.capitalize()}: {version}")
    else:
        print("No IDEs detected with version info")
    
    print("\n" + "="*70)
    print("\nVersion Compatibility Example:\n")
    
    # Test compatibility
    user_v = "1.5.2"
    min_v = "1.5.0"
    max_v = "1.6.0"
    
    compat = check_version_compatibility(min_v, max_v, user_v)
    
    print(f"User has: {user_v}")
    print(f"Template requires: {min_v} - {max_v}")
    print(f"Compatible: {'✅ Yes' if compat else '❌ No'}")
