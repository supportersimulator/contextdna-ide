#!/usr/bin/env python3
"""Apply 3-Surgeons CLI patches: DeepSeek primary + Keychain fallback.

Idempotent — safe to run repeatedly. Detects already-patched files and skips.
Backs up originals to <file>.orig.bak (once).

Usage:
    python3 apply.py                     # apply patches (cardio routes direct to api.deepseek.com)
    python3 apply.py --revert            # restore originals from .orig.bak
    python3 apply.py --check             # report patch state (no changes)
    python3 apply.py --route-via-bridge  # route cardio via ContextDNA Claude
                                         # Bridge (localhost:8855/v1) for unified
                                         # observability + Anthropic auto-fallback.
                                         # Falls through to direct DeepSeek as a
                                         # 3s-level fallback if bridge is down.
    python3 apply.py --route-direct      # restore direct cardio (api.deepseek.com)

Env opt-in:
    THREE_SURGEONS_VIA_BRIDGE=1  When set during `apply.py` (no flag), the bridge
                                 route is applied automatically (same as
                                 --route-via-bridge). Default = 0 = direct.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path.home() / ".claude" / "plugins" / "cache" / \
    "3-surgeons-marketplace" / "3-surgeons" / "1.0.0"
CONFIG_PY = PLUGIN_ROOT / "three_surgeons" / "core" / "config.py"
MAIN_PY = PLUGIN_ROOT / "three_surgeons" / "cli" / "main.py"
HOME_3S_CONFIG = Path.home() / ".3surgeons" / "config.yaml"

# ContextDNA Claude Bridge — unified observability endpoint
BRIDGE_BASE_URL = "http://localhost:8855/v1"
BRIDGE_HEALTH_URL = "http://localhost:8855/health"

PATCH_MARKER = "# [3s-patch] keychain-fallback v1"
BRIDGE_MARKER = "# [3s-patch] bridge-route v1"

# ---------------------------------------------------------------------------
# Patch 1: core/config.py — Keychain fallback for get_api_key()
# ---------------------------------------------------------------------------

CONFIG_OLD_IMPORTS = """from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

import httpx
"""

CONFIG_NEW_IMPORTS = """from __future__ import annotations

# [3s-patch] keychain-fallback v1
import os
import shutil as _shutil_for_kc
import subprocess as _subprocess_for_kc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

import httpx


# [3s-patch] keychain-fallback v1
# Cache Keychain lookups per-process so we never re-shell-out repeatedly.
# DO NOT log the values — Keychain secrets are sensitive.
_KEYCHAIN_CACHE: Dict[str, Optional[str]] = {}
_KEYCHAIN_SERVICE = os.environ.get("THREE_SURGEONS_KEYCHAIN_SERVICE", "fleet-nerve")


def _keychain_lookup(account: str) -> Optional[str]:
    \"\"\"Look up a secret from macOS Keychain (service=fleet-nerve, account=<account>).

    Returns None if `security` is unavailable, lookup fails, or value < 6 chars.
    Cached per-process. Never logged.
    \"\"\"
    if not account:
        return None
    if account in _KEYCHAIN_CACHE:
        return _KEYCHAIN_CACHE[account]
    if not _shutil_for_kc.which("security"):
        _KEYCHAIN_CACHE[account] = None
        return None
    try:
        out = _subprocess_for_kc.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=3.0,
        )
        if out.returncode != 0:
            _KEYCHAIN_CACHE[account] = None
            return None
        value = (out.stdout or "").strip()
        if len(value) < 6:
            _KEYCHAIN_CACHE[account] = None
            return None
        _KEYCHAIN_CACHE[account] = value
        return value
    except (_subprocess_for_kc.TimeoutExpired, OSError):
        _KEYCHAIN_CACHE[account] = None
        return None
"""

CONFIG_OLD_GET_KEY = """    def get_api_key(self) -> Optional[str]:
        \"\"\"Read API key from the environment variable.

        Returns None if the env var is missing or the value is < 6 characters.
        \"\"\"
        value = os.environ.get(self.api_key_env)
        if value is None or len(value) < 6:
            return None
        return value
"""

CONFIG_NEW_GET_KEY = """    def get_api_key(self) -> Optional[str]:
        \"\"\"Read API key from env var, falling back to macOS Keychain.

        Lookup order:
          1. os.environ[self.api_key_env]  (must be >= 6 chars)
          2. Keychain: service=fleet-nerve, account=<api_key_env>

        Returns None if neither source has a usable value.
        \"\"\"
        if not self.api_key_env:
            return None
        value = os.environ.get(self.api_key_env)
        if value is not None and len(value) >= 6:
            return value
        # [3s-patch] keychain-fallback v1
        return _keychain_lookup(self.api_key_env)
"""

# ---------------------------------------------------------------------------
# Patch 2: cli/main.py — probe shows provider, never blocks on neurologist FAIL
# ---------------------------------------------------------------------------

# Patch 2b: Wire surgeon fallbacks (cardiologist + neurologist) so LLMProvider
# uses the YAML-configured fallback chain. Replaces _make_neuro and adds _make_cardio.
MAIN_OLD_MAKE_NEURO = '''def _make_neuro(config: Config) -> LLMProvider:
    """Create neurologist LLMProvider with GPU lock for local providers."""
    if config.neurologist.provider in ("ollama", "mlx", "local", "vllm", "lmstudio"):
        from three_surgeons.core.priority_queue import make_gpu_locked_adapter

        lock_dir = Path(config.gpu_lock_path) if config.gpu_lock_path else None
        adapter = make_gpu_locked_adapter(config.neurologist, lock_dir=lock_dir)
        return LLMProvider(config.neurologist, query_adapter=adapter)
    return LLMProvider(config.neurologist)
'''

MAIN_NEW_MAKE_NEURO = '''def _make_neuro(config: Config) -> LLMProvider:
    """Create neurologist LLMProvider with GPU lock + YAML fallback chain.

    [3s-patch] keychain-fallback v1: passes config.neurologist.get_fallback_configs()
    so LLMProvider.query() can fall through to OpenAI/DeepSeek if the local LLM
    is down.
    """
    fallbacks = config.neurologist.get_fallback_configs()
    if config.neurologist.provider in ("ollama", "mlx", "local", "vllm", "lmstudio"):
        from three_surgeons.core.priority_queue import make_gpu_locked_adapter

        lock_dir = Path(config.gpu_lock_path) if config.gpu_lock_path else None
        adapter = make_gpu_locked_adapter(config.neurologist, lock_dir=lock_dir)
        return LLMProvider(config.neurologist, query_adapter=adapter, fallbacks=fallbacks)
    return LLMProvider(config.neurologist, fallbacks=fallbacks)


def _make_cardio(config: Config) -> LLMProvider:
    """[3s-patch] keychain-fallback v1: cardiologist with YAML fallback chain.

    Wires config.cardiologist.get_fallback_configs() into LLMProvider so the
    DeepSeek-primary -> OpenAI-secondary fallback ladder actually triggers.
    """
    return LLMProvider(
        config.cardiologist,
        fallbacks=config.cardiologist.get_fallback_configs(),
    )
'''

MAIN_OLD_PROBE = """@cli.command()
@click.pass_context
def probe(ctx: click.Context) -> None:
    \"\"\"Health check all 3 surgeons with diagnostic details.\"\"\"
    import httpx

    config: Config = ctx.obj["config"]
    click.echo("Probing surgeons...\\n")

    all_ok = True
    for name, surgeon_cfg in [
        ("Cardiologist", config.cardiologist),
        ("Neurologist", config.neurologist),
    ]:
        # Step 1: Check if API key is needed and present
        is_local = surgeon_cfg.provider in ("ollama", "mlx", "local", "vllm", "lmstudio")
        if not is_local and not surgeon_cfg.get_api_key():
            env_var = surgeon_cfg.api_key_env or "(not configured)"
            click.echo(f"  {name}: FAIL -- API key missing. Set {env_var} env var.")
            all_ok = False
            continue

        # Step 2: Check endpoint reachability
        endpoint = surgeon_cfg.endpoint.rstrip("/")
        try:
            models_resp = httpx.get(f"{endpoint}/models", timeout=3.0)
            endpoint_ok = models_resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            click.echo(
                f"  {name}: FAIL -- endpoint unreachable ({endpoint}). "
                f"Is your {surgeon_cfg.provider} server running?"
            )
            all_ok = False
            continue
        except Exception as exc:
            click.echo(f"  {name}: FAIL -- endpoint error: {exc}")
            all_ok = False
            continue

        # Step 3: Check if the configured model exists
        model_found = True
        if endpoint_ok:
            try:
                data = models_resp.json()
                if isinstance(data, dict) and "data" in data:
                    available = [m.get("id", "") for m in data["data"] if isinstance(m, dict)]
                    if available and surgeon_cfg.model not in available:
                        model_found = False
                        click.echo(
                            f"  {name}: WARN -- endpoint OK but model '{surgeon_cfg.model}' "
                            f"not found. Available: {', '.join(available[:5])}"
                        )
            except Exception:
                pass  # models listing is best-effort

        # Step 4: Test actual LLM call
        try:
            provider = LLMProvider(surgeon_cfg)
            resp = provider.ping(timeout_s=10.0)
            if resp.ok:
                status = "OK" if model_found else "OK (model responded but not in /models list)"
                click.echo(f"  {name}: {status} ({resp.latency_ms}ms)")
            else:
                click.echo(f"  {name}: FAIL -- endpoint reachable but query failed: {resp.content[:100]}")
                all_ok = False
        except Exception as exc:
            click.echo(f"  {name}: FAIL -- {exc}")
            all_ok = False

    click.echo(f"\\nAtlas (Claude): always available (this session)")

    if not all_ok:
        click.echo("\\nSome surgeons unreachable. Run '3s init' to reconfigure.")
        ctx.exit(1)
    else:
        click.echo("\\nAll surgeons operational.")
"""

MAIN_NEW_PROBE = """# [3s-patch] keychain-fallback v1
def _try_surgeon_with_fallbacks(surgeon_cfg) -> tuple:
    \"\"\"Try primary surgeon, then each fallback. Return (ok, info_dict).

    info_dict keys: provider, model, latency_ms (on ok), error (on fail),
                    fallback_used (bool), fallback_chain (list of provider names tried).
    \"\"\"
    import httpx as _httpx
    chain = []
    candidates = [surgeon_cfg] + surgeon_cfg.get_fallback_configs()
    last_error = None
    for idx, cfg in enumerate(candidates):
        chain.append(cfg.provider)
        is_local_c = cfg.provider in ("ollama", "mlx", "local", "vllm", "lmstudio")
        # Skip remote candidates with no key
        if not is_local_c and not cfg.get_api_key():
            last_error = f"{cfg.provider}=key-missing"
            continue
        # Endpoint reachability
        endpoint = cfg.endpoint.rstrip("/")
        try:
            _httpx.get(f"{endpoint}/models", timeout=3.0)
        except (_httpx.ConnectError, _httpx.TimeoutException):
            last_error = f"{cfg.provider}=unreachable"
            continue
        except Exception as exc:
            last_error = f"{cfg.provider}=error({exc})"
            continue
        # Actual ping
        try:
            provider = LLMProvider(cfg)
            resp = provider.ping(timeout_s=10.0)
            if resp.ok:
                return True, {
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "latency_ms": resp.latency_ms,
                    "fallback_used": idx > 0,
                    "fallback_chain": chain,
                }
            last_error = f"{cfg.provider}=query-failed:{resp.content[:60]}"
        except Exception as exc:
            last_error = f"{cfg.provider}=exception:{exc}"
    return False, {
        "error": last_error or "no candidates",
        "fallback_chain": chain,
    }


@cli.command()
@click.pass_context
def probe(ctx: click.Context) -> None:
    \"\"\"Health check all 3 surgeons with diagnostic details.

    [3s-patch] DeepSeek-primary + Keychain-aware probe. Shows which provider
    succeeded; degraded-but-operational mode (cardiologist OK, neurologist FAIL)
    exits 0 since 2-of-3 consensus is still possible with Atlas.
    \"\"\"
    config: Config = ctx.obj["config"]
    click.echo("Probing surgeons...\\n")

    cardio_ok, cardio_info = _try_surgeon_with_fallbacks(config.cardiologist)
    if cardio_ok:
        tag = "OK"
        if cardio_info.get("fallback_used"):
            tag = "OK (fallback)"
        click.echo(
            f"  Cardiologist: {tag} (provider={cardio_info['provider']}, "
            f"model={cardio_info['model']}, {cardio_info['latency_ms']}ms)"
        )
    else:
        click.echo(
            f"  Cardiologist: FAIL ({cardio_info.get('error', 'unknown')}; "
            f"chain={'+'.join(cardio_info.get('fallback_chain', []))})"
        )

    neuro_ok, neuro_info = _try_surgeon_with_fallbacks(config.neurologist)
    if neuro_ok:
        tag = "OK"
        if neuro_info.get("fallback_used"):
            tag = "OK (fallback)"
        click.echo(
            f"  Neurologist:  {tag} (provider={neuro_info['provider']}, "
            f"model={neuro_info['model']}, {neuro_info['latency_ms']}ms)"
        )
    else:
        click.echo(
            f"  Neurologist:  FAIL ({neuro_info.get('error', 'unknown')}; "
            f"chain={'+'.join(neuro_info.get('fallback_chain', []))})"
        )

    click.echo(f"\\nAtlas (Claude): always available (this session)")

    # Degraded-but-operational: cardiologist OK alone is enough for 2-of-3.
    if cardio_ok and neuro_ok:
        click.echo("\\nAll surgeons operational.")
    elif cardio_ok:
        click.echo("\\nDEGRADED: Cardiologist + Atlas operational. Neurologist down — "
                   "consult will run with 2-of-3 consensus.")
    else:
        click.echo("\\nCRITICAL: No remote LLM reachable. Run '3s init' to reconfigure.")
        ctx.exit(1)
"""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _patch_file(path: Path, replacements: list[tuple[str, str]], revert: bool) -> str:
    """Apply or revert a list of (old, new) string replacements. Returns status."""
    backup = path.with_suffix(path.suffix + ".orig.bak")
    if revert:
        if not backup.is_file():
            return f"SKIP {path.name}: no backup found"
        shutil.copy2(backup, path)
        return f"REVERTED {path.name} from {backup.name}"

    content = path.read_text()
    if PATCH_MARKER in content:
        return f"SKIP {path.name}: already patched ({PATCH_MARKER})"

    if not backup.is_file():
        shutil.copy2(path, backup)

    new_content = content
    for old, new in replacements:
        if old not in new_content:
            return (f"FAIL {path.name}: expected block not found — upstream may have changed. "
                    f"Original backed up at {backup.name}.")
        new_content = new_content.replace(old, new, 1)

    path.write_text(new_content)
    return f"PATCHED {path.name} (backup: {backup.name})"


def _check_file(path: Path) -> str:
    if not path.is_file():
        return f"MISSING {path}"
    content = path.read_text()
    if PATCH_MARKER in content:
        return f"PATCHED {path.name}"
    return f"UNPATCHED {path.name}"


# ---------------------------------------------------------------------------
# Bridge routing — opt-in via THREE_SURGEONS_VIA_BRIDGE=1 or --route-via-bridge.
# Rewrites the cardiologist endpoint in ~/.3surgeons/config.yaml to point at
# the local ContextDNA Claude Bridge, and inserts a "direct DeepSeek" fallback
# so 3s itself recovers if the bridge daemon dies. Idempotent + reversible
# via --route-direct.
# ---------------------------------------------------------------------------

def _bridge_alive(timeout_s: float = 2.0) -> bool:
    """Quick HTTP liveness probe on the ContextDNA bridge. One retry on
    transient failure since launchd-managed daemons can be mid-restart."""
    import time as _time
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(BRIDGE_HEALTH_URL, timeout=timeout_s) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, TimeoutError):
            if attempt == 2:
                return False
            _time.sleep(0.5)
    return False


def _route_cardio_via_bridge(config_path: Path) -> str:
    """Rewrite cardiologist block in user config to route via the bridge.

    Returns a status string. Idempotent — detects BRIDGE_MARKER and skips.
    Always backs up to <config>.bridge.bak on first apply.
    """
    if not config_path.is_file():
        return f"SKIP {config_path}: config missing — run 3s init first"
    text = config_path.read_text()
    if BRIDGE_MARKER in text:
        return f"SKIP {config_path.name}: already routed via bridge ({BRIDGE_MARKER})"

    # Best-effort liveness check. Don't fail hard — Aaron may opt-in before
    # the daemon is up; we still rewrite (per spec: "best-effort routing").
    if not _bridge_alive():
        print(
            f"WARN bridge {BRIDGE_HEALTH_URL} unreachable — applying anyway. "
            "3s will fall back to direct DeepSeek when bridge is down."
        )

    # Backup once.
    backup = config_path.with_suffix(config_path.suffix + ".bridge.bak")
    if not backup.is_file():
        shutil.copy2(config_path, backup)

    try:
        import yaml  # type: ignore
    except ImportError:
        return (f"FAIL {config_path.name}: pyyaml not installed in invoking python — "
                f"run inside 3s plugin venv or `pip install pyyaml`")

    raw = yaml.safe_load(text) or {}
    surgeons = raw.setdefault("surgeons", {})
    cardio = surgeons.setdefault("cardiologist", {})

    # Stash the original config as the auto-fallback so 3s self-heals if the
    # bridge goes down mid-session (matches CLAUDE.md "best-effort routing"
    # invariant).
    original_endpoint = cardio.get("endpoint", "https://api.deepseek.com/v1")
    original_provider = cardio.get("provider", "deepseek")
    original_model = cardio.get("model", "deepseek-chat")
    original_key_env = cardio.get("api_key_env", "Context_DNA_Deepseek")

    # Don't re-stack our own bridge as a fallback if rerunning.
    fallbacks = list(cardio.get("fallbacks") or [])
    direct_fb = {
        "provider": original_provider,
        "endpoint": original_endpoint,
        "model": original_model,
        "api_key_env": original_key_env,
    }
    # Insert direct as first fallback if not already present.
    if not any(
        (fb or {}).get("endpoint") == original_endpoint
        and (fb or {}).get("provider") == original_provider
        for fb in fallbacks
    ):
        fallbacks.insert(0, direct_fb)

    cardio["provider"] = "openai"  # bridge speaks OpenAI Chat Completions
    cardio["endpoint"] = BRIDGE_BASE_URL
    cardio["model"] = original_model  # preserved in OpenAI response.model
    # Keep api_key_env so /chat/completions sends a Bearer (bridge ignores it
    # for loopback, but 3s LLMProvider needs SOMETHING to satisfy its non-local
    # check). Re-using the DeepSeek env keeps Keychain lookup working.
    cardio["api_key_env"] = original_key_env
    cardio["fallbacks"] = fallbacks

    # Stamp the marker as a YAML comment so future runs detect the routing.
    new_yaml = yaml.safe_dump(raw, sort_keys=False)
    new_text = f"{BRIDGE_MARKER}\n# Cardiologist routed via ContextDNA Claude Bridge ({BRIDGE_BASE_URL}).\n# Direct DeepSeek preserved as 3s-level fallback. Revert: apply.py --route-direct\n{new_yaml}"
    config_path.write_text(new_text)
    return f"BRIDGE-ROUTED {config_path.name} (backup: {backup.name}; cardio→{BRIDGE_BASE_URL})"


def _route_cardio_direct(config_path: Path) -> str:
    """Restore the cardiologist endpoint to api.deepseek.com (revert bridge route)."""
    if not config_path.is_file():
        return f"SKIP {config_path}: config missing"
    text = config_path.read_text()
    if BRIDGE_MARKER not in text:
        return f"SKIP {config_path.name}: not bridge-routed (nothing to revert)"
    backup = config_path.with_suffix(config_path.suffix + ".bridge.bak")
    if backup.is_file():
        shutil.copy2(backup, config_path)
        return f"DIRECT-RESTORED {config_path.name} from {backup.name}"
    return f"FAIL {config_path.name}: marker present but backup missing — manual edit required"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--revert", action="store_true", help="restore from .orig.bak")
    ap.add_argument("--check", action="store_true", help="report state, no changes")
    ap.add_argument("--route-via-bridge", action="store_true",
                    help="route cardiologist via ContextDNA bridge (localhost:8855/v1)")
    ap.add_argument("--route-direct", action="store_true",
                    help="restore direct cardiologist (api.deepseek.com)")
    args = ap.parse_args()

    if not PLUGIN_ROOT.is_dir():
        print(f"ERROR: plugin root not found at {PLUGIN_ROOT}", file=sys.stderr)
        return 2
    for f in (CONFIG_PY, MAIN_PY):
        if not f.is_file():
            print(f"ERROR: expected file missing: {f}", file=sys.stderr)
            return 2

    if args.check:
        for f in (CONFIG_PY, MAIN_PY):
            print(_check_file(f))
        # Bridge-route status check
        if HOME_3S_CONFIG.is_file():
            cfg_text = HOME_3S_CONFIG.read_text()
            if BRIDGE_MARKER in cfg_text:
                print(f"BRIDGE-ROUTED {HOME_3S_CONFIG.name} (cardio→{BRIDGE_BASE_URL})")
            else:
                print(f"DIRECT {HOME_3S_CONFIG.name} (cardio→api.deepseek.com)")
        return 0

    # Routing-only operations: don't touch plugin source, just rewrite the
    # user's ~/.3surgeons/config.yaml. Useful for quick toggle without
    # re-applying the keychain-fallback patches.
    if args.route_via_bridge and args.route_direct:
        print("ERROR: --route-via-bridge and --route-direct are mutually exclusive",
              file=sys.stderr)
        return 2
    if args.route_via_bridge:
        print(_route_cardio_via_bridge(HOME_3S_CONFIG))
        return 0
    if args.route_direct:
        print(_route_cardio_direct(HOME_3S_CONFIG))
        return 0

    print(_patch_file(CONFIG_PY, [
        (CONFIG_OLD_IMPORTS, CONFIG_NEW_IMPORTS),
        (CONFIG_OLD_GET_KEY, CONFIG_NEW_GET_KEY),
    ], revert=args.revert))

    # main.py: replace _make_neuro+add _make_cardio, replace probe, then
    # rewrite cardio constructor callsites to use _make_cardio.
    main_replacements = [
        (MAIN_OLD_MAKE_NEURO, MAIN_NEW_MAKE_NEURO),
        (MAIN_OLD_PROBE, MAIN_NEW_PROBE),
    ]
    # Convert all `LLMProvider(config.cardiologist)` -> `_make_cardio(config)`.
    # Use replace_all-style by appending one big substitution. We do this AFTER
    # the structural patches so the file already has _make_cardio defined.
    # We rely on _patch_file's single-pass replace; safe because each occurrence
    # is identical.
    print(_patch_file(MAIN_PY, main_replacements, revert=args.revert))

    # Second pass: cardio callsites (idempotent — only replaces if not already done).
    if not args.revert:
        try:
            content = MAIN_PY.read_text()
            old_call = "LLMProvider(config.cardiologist)"
            new_call = "_make_cardio(config)"
            if old_call in content:
                count = content.count(old_call)
                content = content.replace(old_call, new_call)
                MAIN_PY.write_text(content)
                print(f"PATCHED main.py cardio callsites ({count} replacements)")
            else:
                print("SKIP main.py cardio callsites: already migrated")
        except Exception as exc:  # pragma: no cover
            print(f"WARN cardio callsite rewrite failed: {exc}")

    # Env-driven opt-in (B3 #2): THREE_SURGEONS_VIA_BRIDGE=1 routes cardio
    # via the ContextDNA bridge for unified observability. Default OFF.
    # Revert: rerun with var unset, then `apply.py --route-direct`.
    if not args.revert:
        env_flag = os.environ.get("THREE_SURGEONS_VIA_BRIDGE", "").strip().lower()
        if env_flag in ("1", "true", "yes", "on"):
            print(_route_cardio_via_bridge(HOME_3S_CONFIG))
        else:
            # Surface current routing state so Aaron sees which mode is active
            if HOME_3S_CONFIG.is_file() and BRIDGE_MARKER in HOME_3S_CONFIG.read_text():
                print(
                    f"NOTE {HOME_3S_CONFIG.name}: cardio routed via bridge "
                    f"(stale — env unset). Run `apply.py --route-direct` to revert."
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
