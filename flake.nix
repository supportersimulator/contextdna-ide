# flake.nix — Reproducible setup for the ContextDNA Mothership.
#
# Every dependency is pinned by hash. The same `nix run .#setup` today and
# in 6 months pulls byte-identical versions of age, awscli, nats-server,
# docker-compose, python, jq, etc. No "it worked yesterday" drift.
#
# Quick start (no Nix yet):
#   curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
#   exit  # re-open shell
#   git clone git@github.com:supportersimulator/contextdna-ide.git
#   cd contextdna-ide
#   nix run .#setup     # full bootstrap
#
# After Nix is installed:
#   nix develop             # drop into a shell with all tools pinned and ready
#   nix run .#setup         # → wraps scripts/setup-mothership.sh + configure-*.sh
#   nix run .#restore -- ~/USB/bundle.age   # full automated recovery
#   nix run .#seal          # → wraps scripts/seal-recovery-bundle.sh
#   nix run .#probe         # read-only health check of all subsystems
#   nix flake check         # verify the flake itself
#
# Optional (full system management on macOS):
#   nix run nix-darwin/master -- switch --flake .#aaron-mac
#   # See nix/darwin-configuration.nix
#
# Update pinned versions (do this intentionally, not by surprise):
#   nix flake update
#   # commit flake.lock to lock in the new versions

{
  description = "ContextDNA Mothership — reproducible setup, recovery, and ops via Nix";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";

    # nix-darwin: macOS system management (launchd, homebrew bridge)
    nix-darwin = {
      url = "github:LnL7/nix-darwin";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # home-manager: user-level config (Claude settings, 3-Surgeons config)
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # agenix: age-encrypted secrets in the Nix store
    # (Same age tool we already use for recovery bundles — perfect alignment)
    agenix = {
      url = "github:ryantm/agenix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, nix-darwin, home-manager, agenix, ... }:
    let
      # Tools that every ContextDNA workflow needs, pinned by hash via nixpkgs
      _commonTools = pkgs: with pkgs; [
        # Encryption + secrets
        age              # the encryption tool (recovery bundles + agenix)
        gnupg            # for SSH key signing if needed

        # Cloud + network
        awscli2          # S3-compatible storage
        curl jq          # universal HTTP + JSON

        # Fleet coordination
        nats-server      # JetStream-capable NATS
        natscli          # `nats` CLI

        # Python toolchain (for the existing bash + Python codebase)
        python312
        python312Packages.pip
        python312Packages.virtualenv
        uv               # fast pip replacement

        # Container runtime support
        docker
        docker-compose

        # PostgreSQL client (for pg_dump in backup scripts)
        postgresql_16

        # Dev experience
        git
        gh               # GitHub CLI
        ripgrep
        fd
        tree
        coreutils
        gnused
        gnutar
        gzip
      ];

    in flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        commonTools = _commonTools pkgs;

        # Wrapper that ensures we're invoking the repo's bash scripts with
        # the pinned tools in PATH. Identical to a `nix develop` session
        # but bound to a single command for `nix run`.
        mkApp = name: scriptName: extraEnv: pkgs.writeShellApplication {
          name = name;
          runtimeInputs = commonTools;
          text = ''
            set -euo pipefail
            # Find the repo root — usually $PWD, but allow override
            REPO="''${REPO_DIR:-$PWD}"
            if [ ! -f "$REPO/scripts/${scriptName}" ]; then
              echo "ContextDNA: cannot find scripts/${scriptName} in $REPO" >&2
              echo "Set REPO_DIR=/path/to/contextdna-ide and re-run." >&2
              exit 2
            fi
            ${extraEnv}
            exec bash "$REPO/scripts/${scriptName}" "$@"
          '';
        };

      in {
        # ─────────────────────────────────────────────────────────────────
        # `nix develop` — drop into a shell with every tool pinned & ready
        # ─────────────────────────────────────────────────────────────────
        devShells.default = pkgs.mkShell {
          name = "contextdna-mothership";
          packages = commonTools ++ [
            agenix.packages.${system}.default  # `agenix` CLI for managing
                                               # nix-store age secrets
          ];

          shellHook = ''
            echo ""
            echo "━━━ ContextDNA Mothership — Nix Dev Shell ━━━"
            echo "  Tools pinned: age $(age --version | head -1), aws $(aws --version 2>&1 | cut -d' ' -f1),"
            echo "                nats-server $(nats-server --version 2>&1 | head -1 | awk '{print $2}'),"
            echo "                python $(python3 --version | cut -d' ' -f2), psql $(psql --version | awk '{print $3}')"
            echo ""
            echo "  Common commands:"
            echo "    bash scripts/setup-mothership.sh"
            echo "    bash scripts/configure-services.sh"
            echo "    bash scripts/configure-ecosystem.sh"
            echo "    bash scripts/seal-recovery-bundle.sh"
            echo "    bash scripts/unseal-recovery-bundle.sh <bundle.age>"
            echo ""
            echo "  Or use the Nix wrappers (same scripts, guaranteed pinned env):"
            echo "    nix run .#setup"
            echo "    nix run .#restore -- <bundle.age>"
            echo "    nix run .#seal"
            echo "    nix run .#probe"
            echo ""
          '';
        };

        # ─────────────────────────────────────────────────────────────────
        # `nix run .#<name>` — invoke specific workflows
        # ─────────────────────────────────────────────────────────────────
        apps = {
          # First-time bootstrap on a fresh machine
          setup = {
            type = "app";
            program = "${mkApp "contextdna-setup" "setup-mothership.sh" ""}/bin/contextdna-setup";
          };

          # Service configuration (LLMs, local LLM, NATS, Docker, etc.)
          services = {
            type = "app";
            program = "${mkApp "contextdna-services" "configure-services.sh" ""}/bin/contextdna-services";
          };

          # Ecosystem (Multi-Fleet, 3-Surgeons, Superpowers, MCP)
          ecosystem = {
            type = "app";
            program = "${mkApp "contextdna-ecosystem" "configure-ecosystem.sh" ""}/bin/contextdna-ecosystem";
          };

          # Read-only health check across all subsystems
          probe = {
            type = "app";
            program = (pkgs.writeShellApplication {
              name = "contextdna-probe";
              runtimeInputs = commonTools;
              text = ''
                set -uo pipefail
                REPO="''${REPO_DIR:-$PWD}"
                echo "━━━ ContextDNA Health Probe ━━━"
                echo ""
                bash "$REPO/scripts/setup-mothership.sh" --check     || true
                echo ""
                bash "$REPO/scripts/configure-services.sh" --probe   || true
                echo ""
                bash "$REPO/scripts/configure-ecosystem.sh" --probe  || true
              '';
            }) + "/bin/contextdna-probe";
          };

          # Create encrypted recovery bundle
          seal = {
            type = "app";
            program = "${mkApp "contextdna-seal" "seal-recovery-bundle.sh" ""}/bin/contextdna-seal";
          };

          # Full automated recovery from encrypted bundle
          restore = {
            type = "app";
            program = "${mkApp "contextdna-restore" "unseal-recovery-bundle.sh" ""}/bin/contextdna-restore";
          };

          default = self.apps.${system}.setup;
        };

        # ─────────────────────────────────────────────────────────────────
        # `nix flake check` — verify the flake itself
        # ─────────────────────────────────────────────────────────────────
        checks = {
          # All scripts must pass bash -n (syntax check)
          scripts-syntax = pkgs.runCommand "contextdna-scripts-syntax" {} ''
            set -e
            mkdir -p $out
            for script in ${./scripts}/*.sh; do
              echo "checking $script"
              ${pkgs.bash}/bin/bash -n "$script"
            done
            echo OK > $out/result
          '';
        };

        # ─────────────────────────────────────────────────────────────────
        # `nix fmt` — keep flake formatted
        # ─────────────────────────────────────────────────────────────────
        formatter = pkgs.nixpkgs-fmt;
      }
    ) // {
      # ─────────────────────────────────────────────────────────────────────
      # `darwin-rebuild switch --flake .#mothership` — full macOS system
      # config: installs everything, sets up launchd services, registers
      # all the contextdna daemons. Optional but powerful.
      # ─────────────────────────────────────────────────────────────────────
      darwinConfigurations.mothership = nix-darwin.lib.darwinSystem {
        system = "aarch64-darwin";  # change to x86_64-darwin for Intel Mac
        specialArgs = { inherit self; };
        modules = [
          ./nix/darwin-configuration.nix
          home-manager.darwinModules.home-manager
          agenix.darwinModules.default
          {
            home-manager.useGlobalPkgs = true;
            home-manager.useUserPackages = true;
            home-manager.users.aaron = import ./nix/home-manager.nix;
          }
        ];
      };
    };
}
