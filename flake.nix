{
  description = "Neuro-formal verification (NFV) replication: Python -> Dafny / CBMC with LLM front ends";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      pythonFor = pkgs: pkgs.python3.withPackages (ps: [ ps.pytest ]);

      # The verifiers the pipeline calls (Dafny bundles its own Z3 wrapper; CBMC/goto-cc
      # preprocess C with the system C compiler, so it must be on PATH too).
      verifiersFor = pkgs: [ pkgs.dafny pkgs.cbmc pkgs.stdenv.cc ];

      nfvFor = pkgs: pkgs.writeShellApplication {
        name = "nfv";
        runtimeInputs = [ (pythonFor pkgs) ] ++ verifiersFor pkgs;
        text = ''
          export PYTHONPATH="${self}''${PYTHONPATH:+:$PYTHONPATH}"
          exec python3 -m nfv --dataset "''${NFV_DATASET:-${self}/data/nl2post_safe.jsonl}" "$@"
        '';
      };
    in
    {
      packages = forAll (pkgs: {
        default = nfvFor pkgs;
        nfv = nfvFor pkgs;
      });

      apps = forAll (pkgs: {
        default = { type = "app"; program = "${nfvFor pkgs}/bin/nfv"; };
      });

      devShells = forAll (pkgs: {
        # `nix develop`: everything needed to build the dataset, run the pipeline and the tests.
        default = pkgs.mkShell {
          packages = [ (pythonFor pkgs) pkgs.git ] ++ verifiersFor pkgs;
          shellHook = ''
            export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
            alias nfv="python3 -m nfv"
          '';
        };

        # `nix develop .#local-llm`: adds llama.cpp (llama-server) to serve a local model over an
        # OpenAI-compatible API, e.g. `llama-server -m model.gguf --port 8080 -c 32768`.
        local-llm = pkgs.mkShell {
          packages = [ (pythonFor pkgs) pkgs.git pkgs.llama-cpp ] ++ verifiersFor pkgs;
          shellHook = ''
            export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
      });

      checks = forAll (pkgs: {
        # Offline smoke tests (scripted LLM outputs; real Dafny/CBMC). No network, no GPU, no claude.
        smoke = pkgs.runCommand "nfv-smoke-tests"
          { nativeBuildInputs = [ (pythonFor pkgs) ] ++ verifiersFor pkgs; } ''
          cp -r ${self} src && chmod -R u+w src && cd src
          export HOME=$TMPDIR DOTNET_CLI_HOME=$TMPDIR
          python3 -m pytest -q tests
          touch $out
        '';
      });
    };
}
