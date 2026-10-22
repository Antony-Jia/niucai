"""Restore a cold backup into NEW managed volumes. Requires explicit source-stop attestation."""

import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path


def docker(*args, capture=False):
    result = subprocess.run(["docker", *args], check=True, capture_output=capture, text=capture)
    return result.stdout.strip() if capture else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", type=Path)
    parser.add_argument("--source-stopped", action="store_true", required=True)
    args = parser.parse_args()
    backup = args.backup.resolve()
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8-sig"))
    expected = {"postgres.dump", "workspace.tar", "pi_sessions.tar"}
    if set(manifest["files"]) != expected:
        raise ValueError("unexpected backup manifest files")
    for name, digest in manifest["files"].items():
        hasher = hashlib.sha256()
        with (backup / name).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        if hasher.hexdigest() != digest:
            raise ValueError(f"backup checksum mismatch: {name}")
    existing = docker(
        "ps", "-aq", "--filter", "label=com.docker.compose.project=niucai-managed", capture=True
    )
    if existing:
        raise RuntimeError("Restore only into a fresh destination; managed containers already exist")
    volumes = set(docker("volume", "ls", "-q", capture=True).splitlines())
    if any(f"niucai-managed_{name}" in volumes for name in ["postgres", "workspace", "pi_sessions"]):
        raise RuntimeError("Refuse to overwrite existing managed volumes")
    # Explicitly validate archive entries; no arbitrary paths or links are restored.
    for name in ["workspace", "pi_sessions"]:
        with tarfile.open(backup / (name + ".tar")) as archive:
            for member in archive.getmembers():
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                    raise ValueError("archive contains unsafe path, symlink or special file")
    for name in ["workspace", "pi_sessions"]:
        volume = f"niucai-managed_{name}"
        docker("volume", "create", "--label", "com.niucai.managed=kernel-v2", volume)
        docker(
            "run",
            "--rm",
            "--user",
            "root",
            "-v",
            f"{volume}:/target",
            "-v",
            f"{backup}:/backup:ro",
            "--entrypoint",
            "python",
            "niucai-kernel:kernel-anywhere-v2",
            "-c",
            f"import tarfile; t=tarfile.open('/backup/{name}.tar');t.extractall('/target',filter='data')",
        )
    # PostgreSQL restore is performed only after its initialization and BEFORE API/Worker startup.
    print("Volumes restored and checksums verified. Source remains stopped. Next:")
    print("Provision destination credentials; start only postgres with compose.managed.yml.")
    print("docker cp <backup>/postgres.dump <managed-postgres>:/tmp/restore.dump")
    print("docker exec <managed-postgres> pg_restore -U niucai -d niucai --exit-on-error /tmp/restore.dump")
    print("Then migrate/start API, verify kernel_instance_id=" + manifest["kernel_instance_id"])
    print("Verify history, sessions and artifacts; update addresses; resume admission only at destination.")


if __name__ == "__main__":
    main()
