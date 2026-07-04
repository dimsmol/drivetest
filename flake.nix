{
  description = "drivetest - SSD/NVMe health, integrity and performance tester";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});

      # External CLIs drivetest shells out to. The Python package itself is
      # stdlib-only (see pyproject.toml); these are the real runtime dependencies
      # and are put on the wrapped tool's PATH below. nvme-cli is only needed for
      # NVMe targets, but bundling it keeps a single self-contained tool.
      runtimeTools =
        pkgs: with pkgs; [
          fio # write+verify and read benchmarks
          smartmontools # smartctl - SMART/health baseline and diff
          nvme-cli # nvme - NVMe SMART/temperature
          util-linux # lsblk / findmnt / wipefs - device model and safety guards
        ];
    in
    {
      packages = forAllSystems (
        system: pkgs: rec {
          default = drivetest;

          drivetest = pkgs.python3Packages.buildPythonApplication {
            pname = "drivetest";
            version = "0.1.0";
            src = ./.;
            pyproject = true;
            build-system = [ pkgs.python3Packages.hatchling ];

            # Put the external CLIs on PATH so the installed tool is self-contained.
            makeWrapperArgs = [
              "--prefix PATH : ${pkgs.lib.makeBinPath (runtimeTools pkgs)}"
            ];

            # The test suite shells out to real tools (fio, smartctl) and inspects
            # devices, so it does not belong in the sandboxed build. Run it in the
            # dev shell instead (see `nix develop`).
            doCheck = false;

            meta = {
              description = "Health, integrity and performance test for a storage device (SSD/NVMe)";
              mainProgram = "drivetest";
              license = pkgs.lib.licenses.mit;
              platforms = pkgs.lib.platforms.linux;
            };
          };
        }
      );

      apps = forAllSystems (
        system: pkgs: rec {
          default = drivetest;
          drivetest = {
            type = "app";
            program = "${self.packages.${system}.drivetest}/bin/drivetest";
          };
        }
      );

      devShells = forAllSystems (
        system: pkgs: {
          default = pkgs.mkShell {
            # Python for running the suite plus the external CLIs the tool drives.
            # ruff/pyright lint and type-check; uv runs import-linter on the fly
            # (it is not packaged in nixpkgs) via `uvx --from import-linter`.
            packages = [
              (pkgs.python3.withPackages (ps: [ ps.pytest ]))
              pkgs.ruff
              pkgs.pyright
              pkgs.uv
            ]
            ++ runtimeTools pkgs;

            shellHook = ''
              echo "drivetest dev shell - run the tool with ./drivetest, checks per README."
            '';
          };
        }
      );
    };
}
