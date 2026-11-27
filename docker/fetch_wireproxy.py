"""A rögzített wireproxy-kiadás letöltése és ellenőrzése a Docker-buildben (VPN-terv Rev. 5, 8. fejezet).

Használat: fetch_wireproxy.py <verzió> <arch> <archívum-sha256> <bináris-sha256 vagy ->

Az archívum SHA-256-ja a kiadás checksums.txt-jéből van rögzítve; eltérésnél a
build megáll. Ha a bináris hash-e is ismert (az amd64-et a PoC mérte), azt is
ellenőrzi. A kimenet: /out/wireproxy (0755).
"""

import hashlib
import io
import os
import sys
import tarfile
import urllib.request

REPOSITORY = "https://github.com/windtf/wireproxy"


def main() -> None:
    version, arch, archive_sha256, binary_sha256 = sys.argv[1:5]
    url = f"{REPOSITORY}/releases/download/{version}/wireproxy_linux_{arch}.tar.gz"
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()

    digest = hashlib.sha256(data).hexdigest()
    if digest != archive_sha256:
        sys.exit(f"wireproxy {version} {arch}: archive checksum mismatch ({digest})")

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.isfile() and os.path.basename(member.name) == "wireproxy"
        ]
        if len(members) != 1:
            sys.exit(f"wireproxy {version} {arch}: expected one binary in the archive")
        handle = archive.extractfile(members[0])
        if handle is None:
            sys.exit(f"wireproxy {version} {arch}: binary not readable")
        binary = handle.read()

    binary_digest = hashlib.sha256(binary).hexdigest()
    if binary_sha256 != "-" and binary_digest != binary_sha256:
        sys.exit(f"wireproxy {version} {arch}: binary checksum mismatch ({binary_digest})")

    os.makedirs("/out", exist_ok=True)
    target = "/out/wireproxy"
    with open(target, "wb") as output:
        output.write(binary)
    os.chmod(target, 0o755)
    print(f"wireproxy {version} {arch}: archive ok, binary sha256 {binary_digest}")


if __name__ == "__main__":
    main()
