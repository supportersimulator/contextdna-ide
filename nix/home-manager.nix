# nix/home-manager.nix — user-level config managed declaratively.
#
# Anything that's a dotfile in your home directory should live here.
# This makes ~/.config/git, ~/.zshrc, ~/.claude/settings.json, etc.
# all reproducible from one place.
#
# After editing, run:
#   darwin-rebuild switch --flake .#mothership
# (or `home-manager switch --flake .#aaron` if not using nix-darwin)

{ config, pkgs, ... }:

{
  home.stateVersion = "24.11";

  # ── Git ───────────────────────────────────────────────────────────────────
  programs.git = {
    enable = true;
    # userName + userEmail intentionally NOT declared here so the same
    # flake works for any user. Override with a private overlay.
  };

  # ── Zsh config (minimal — keep your existing rc if you have one) ──────────
  programs.zsh = {
    enable = true;
    initExtra = ''
      # ContextDNA convenience: drop into the dev shell from anywhere
      alias mothership="cd $HOME/dev/contextdna-ide && nix develop"
      alias cdna-probe="cd $HOME/dev/contextdna-ide && nix run .#probe"
      alias cdna-seal="cd $HOME/dev/contextdna-ide && nix run .#seal"
    '';
  };

  # ── Claude Code settings ──────────────────────────────────────────────────
  # If you have a baseline ~/.claude/settings.json you want every machine
  # to share, declare it here as a `home.file` entry. Personal/private
  # overrides go through the recovery bundle (encrypted).
  home.file.".claude/settings.json".text = builtins.toJSON {
    # Minimal sensible defaults — recovery bundle overlays your real settings
    permissions = {
      defaultMode = "ask";  # safer than "allow" on a fresh install
    };
    hooks = {
      # Wire UserPromptSubmit to the contextdna webhook only if the file exists
      # (no error if it's missing on a fresh setup)
    };
  };

  # ── 3-Surgeons starter config ─────────────────────────────────────────────
  # Same shape as configure-ecosystem.sh writes — keeping them in sync.
  home.file.".3surgeons/config.yaml".text = ''
    # 3-Surgeons configuration — managed declaratively via home-manager
    # Override with a recovery bundle (which gets restored AFTER home-manager
    # activation, so the bundle wins for personalization).

    surgeons:
      cardiologist:
        provider: deepseek
        endpoint: https://api.deepseek.com/v1
        model: deepseek-chat
        api_key_env: DEEPSEEK_API_KEY

      neurologist:
        provider: mlx
        endpoint: http://localhost:5044/v1
        model: mlx-community/Qwen3-4B-4bit
        api_key_env: ""

    budgets:
      daily_external_usd: 5.00
      autonomous_ab_usd: 0.50

    review:
      depth: iterative

    evidence_db: ~/.3surgeons/evidence.db
  '';

  # ── User packages (per-user, in addition to system-level) ────────────────
  home.packages = with pkgs; [
    # Anything you want available only for this user goes here
    # e.g. shell prompts, language servers, etc.
  ];

  programs.home-manager.enable = true;
}
