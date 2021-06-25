{
  description = "A Nix derivation graph profiler";

  inputs.nixpkgs.url = github:NixOS/nixpkgs/nixpkgs-unstable;
  inputs.flake-utils.url = github:numtide/flake-utils;

  outputs = { self, nixpkgs, flake-utils }: flake-utils.lib.eachDefaultSystem (system:
    with import nixpkgs { inherit system; };
    rec {
      packages.nixprof = python3Packages.buildPythonApplication {
        propagatedBuildInputs = with python3Packages; [ networkx pydot click moreutils ];
        name = "nixprof";
        src = ./.;
      };

      defaultPackage = packages.nixprof;
    }
  );
}
