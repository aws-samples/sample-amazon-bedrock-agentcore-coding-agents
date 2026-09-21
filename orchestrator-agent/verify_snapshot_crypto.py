"""Fail an image build unless both Python crypto backends use snapsafe libraries."""
import _ssl
import json
from pathlib import Path
import re
import subprocess


def verify_linkage(extension, *, require_ssl=False):
    output = subprocess.check_output(["ldd", str(extension)], text=True)
    libraries = {
        match.group(1): str(Path(match.group(2)).resolve())
        for match in re.finditer(r"\b(lib(?:ssl|crypto)\.so\.\S+)\s+=>\s+(\S+)", output)
    }
    required = {"libcrypto.so.3"}
    if require_ssl:
        required.add("libssl.so.3")
    if not required.issubset(libraries):
        raise RuntimeError(
            f"{extension}: expected dynamic OpenSSL linkage; a bundled/static backend is not admitted")
    owners = {}
    for library, path in libraries.items():
        owner = subprocess.check_output(
            ["rpm", "-qf", "--qf", "%{NAME}", path], text=True).strip()
        if owner != "openssl-snapsafe-libs":
            raise RuntimeError(f"{extension}: {library} belongs to {owner}, not openssl-snapsafe-libs")
        owners[library] = {"path": path, "package": owner}
    return owners


def main():
    from cryptography.hazmat.bindings import _rust
    from cryptography.hazmat.backends.openssl.backend import backend

    result = {
        "python_ssl": verify_linkage(_ssl.__file__, require_ssl=True),
        "cryptography": verify_linkage(_rust.__file__),
        "python_openssl_version": _ssl.OPENSSL_VERSION,
        "cryptography_openssl_version": backend.openssl_version_text(),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
