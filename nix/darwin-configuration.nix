# nix/darwin-configuration.nix — optional full macOS system management.
#
# This declares the entire ContextDNA stack as a *system configuration*:
# pinned tool versions, launchd services for the daemons, Homebrew brews
# for things Nix doesn't package well (e.g. Docker Desktop), etc.
#
# Activate:
#   nix run nix-darwin/master -- switch --flake .#mothership
#
# After activation, your system has every contextdna daemon running, every
# tool installed, every plist managed — and `darwin-rebuild switch` is the
# *single* knob to change anything. The same flake on a different Mac
# produces a byte-identical setup.

{ pkgs, self, ... }:

{
  # ── System packages (installed for all users, pinned by nixpkgs hash) ──────
  environment.systemPackages = with pkgs; [
    age           # recovery bundle encryption
    awscli2       # S3-compatible backup uploads
    nats-server   # JetStream NATS
    natscli       # `nats` CLI
    curl jq gh git
    python312
    python312Packages.pip
    uv
    docker docker-compose
    postgresql_16
    ripgrep fd tree
  ];

  # ── Homebrew bridge for things Nix doesn't package as well ────────────────
  homebrew = {
    enable = true;
    onActivation.autoUpdate = false;   # explicit upgrades only
    onActivation.cleanup = "uninstall"; # remove anything not declared here

    casks = [
      "docker"          # Docker Desktop GUI (Nix has cli but Desktop is easier)
      "claude"          # Claude Code (Anthropic CLI)
    ];

    brews = [
      "mlx-lm"          # for local LLM serving on Apple Silicon
    ];
  };

  # ── launchd services (managed centrally) ───────────────────────────────────
  launchd.user.agents = {
    # NATS JetStream server
    nats-server = {
      serviceConfig = {
        Label = "io.nats.server";
        ProgramArguments = [
          "${pkgs.nats-server}/bin/nats-server"
          "-js"
          "-m" "8222"
        ];
        RunAtLoad = true;
        KeepAlive = true;
        StandardOutPath = "/tmp/nats-server.log";
        StandardErrorPath = "/tmp/nats-server.err";
      };
    };

    # Daily PostgreSQL backup at 03:00
    contextdna-backup-pg = {
      serviceConfig = {
        Label = "io.contextdna.backup-pg";
        ProgramArguments = [
          "/bin/bash" "-lc"
          "cd $HOME/dev/contextdna-ide && set -a && . ./.env && set +a && bash infra/backup/pg-dump.sh"
        ];
        StartCalendarInterval = [{ Hour = 3; Minute = 0; }];
        StandardOutPath = "/tmp/contextdna-backup-pg.log";
        StandardErrorPath = "/tmp/contextdna-backup-pg.err";
      };
    };

    # Weekly JetStream snapshot at Sunday 04:00
    contextdna-backup-js = {
      serviceConfig = {
        Label = "io.contextdna.backup-jetstream";
        ProgramArguments = [
          "/bin/bash" "-lc"
          "cd $HOME/dev/contextdna-ide && set -a && . ./.env && set +a && bash infra/backup/jetstream-snapshot.sh"
        ];
        StartCalendarInterval = [{ Weekday = 0; Hour = 4; Minute = 0; }];
        StandardOutPath = "/tmp/contextdna-backup-js.log";
        StandardErrorPath = "/tmp/contextdna-backup-js.err";
      };
    };
  };

  # ── System defaults (small QoL stuff, optional) ───────────────────────────
  system.defaults = {
    NSGlobalDomain.AppleShowAllExtensions = true;
    finder.AppleShowAllFiles = true;
  };

  # ── Nix settings ──────────────────────────────────────────────────────────
  nix = {
    settings = {
      experimental-features = [ "nix-command" "flakes" ];
      trusted-users = [ "@admin" ];
    };
    # GC weekly to keep the store small
    gc = {
      automatic = true;
      interval = { Day = 7; };
    };
  };

  # nix-darwin housekeeping
  system.stateVersion = 4;
  services.nix-daemon.enable = true;
  programs.zsh.enable = true;
}
