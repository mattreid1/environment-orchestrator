{ pkgs, codex ? pkgs.codex }:
let
  python = pkgs.python3.withPackages (p: [ p.aiohttp ]);
  path = pkgs.lib.makeBinPath [ python codex pkgs.bubblewrap pkgs.openssh pkgs.coreutils pkgs.nix pkgs.e2fsprogs ];
in pkgs.rustPlatform.buildRustPackage {
  pname = "environment-orchestrator";
  version = "0.2.0";
  src = ./rust;
  cargoLock.lockFile = ./rust/Cargo.lock;
  nativeBuildInputs = [ pkgs.pkg-config pkgs.makeWrapper ];
  buildInputs = [ pkgs.sqlite ];
  postInstall = ''
    mkdir -p $out/lib/environment-orchestrator
    cp ${./cli.py} $out/lib/environment-orchestrator/cli.py
    cp ${./codex.py} $out/lib/environment-orchestrator/codex.py
    wrapProgram $out/bin/environment-orchestrator --prefix PATH : ${path}
    ln -s environment-orchestrator $out/bin/environment-orchestrator-service
    for entry in 'cli environment-vm' 'codex environment-codex'; do
      set -- $entry
      makeWrapper ${python}/bin/python3 $out/bin/$2 --add-flags $out/lib/environment-orchestrator/$1.py --prefix PATH : ${path}
    done
  '';
  meta = {
    description = "Private on-demand Firecracker execution environments";
    mainProgram = "environment-orchestrator-service";
    platforms = [ "x86_64-linux" "aarch64-linux" ];
  };
}
