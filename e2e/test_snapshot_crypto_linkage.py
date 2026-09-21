"""The image's real linkage verifier must reject bundled or regular OpenSSL."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKERS = (
    ROOT / "orchestrator-agent/verify_snapshot_crypto.py",
    ROOT / "coding-agents/gateway_mcp/app/verify_snapshot_crypto.py",
)


@pytest.fixture(params=CHECKERS, ids=("coordinator", "github-mcp"))
def checker(request):
    spec = importlib.util.spec_from_file_location("snapshot_crypto_check", request.param)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundled_crypto_cannot_pass_without_dynamic_system_linkage(checker, monkeypatch):
    monkeypatch.setattr(checker.subprocess, "check_output", lambda *args, **kwargs:
                        "libc.so.6 => /lib64/libc.so.6 (0x1234)\n")
    with pytest.raises(RuntimeError, match="bundled/static"):
        checker.verify_linkage("cryptography/_rust.abi3.so")


@pytest.mark.parametrize("owner", ("openssl-libs", "openssl-snapsafe-libs"))
def test_checks_the_actual_resolved_library_package(checker, monkeypatch, tmp_path, owner):
    crypto = tmp_path / "libcrypto.so.3.5.8"
    ssl = tmp_path / "libssl.so.3.5.8"
    for path in (crypto, ssl):
        path.touch()
    owned = []

    def output(command, **kwargs):
        if command[0] == "ldd":
            return (f"libcrypto.so.3 => {crypto} (0x1234)\n"
                    f"libssl.so.3 => {ssl} (0x5678)\n")
        assert command[:4] == ["rpm", "-qf", "--qf", "%{NAME}"]
        owned.append(Path(command[-1]))
        return owner

    monkeypatch.setattr(checker.subprocess, "check_output", output)
    if owner == "openssl-libs":
        with pytest.raises(RuntimeError, match="belongs to openssl-libs"):
            checker.verify_linkage("_ssl.so", require_ssl=True)
    else:
        result = checker.verify_linkage("_ssl.so", require_ssl=True)
        assert set(result) == {"libcrypto.so.3", "libssl.so.3"}
        assert set(owned) == {crypto.resolve(), ssl.resolve()}
