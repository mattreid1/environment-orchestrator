{
  description = "On-demand Firecracker environments for host agent harnesses";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/c59305bab2065cfecc4944690d9eedbb56f3a9fa";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      eachSystem = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));
    in {
      packages = eachSystem (pkgs: {
        default = import ./package.nix { inherit pkgs; };
        test-tools = pkgs.python3.withPackages (p: [ p.aiohttp ]);
      });
      checks = eachSystem (pkgs: { rust = self.packages.${pkgs.stdenv.hostPlatform.system}.default; });
    };
}
