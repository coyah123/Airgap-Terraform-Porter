"""Which CA certificates HTTPS downloads trust (offline; macOS simulated).

    python -m unittest discover -s tests
"""
from __future__ import annotations

import ssl
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fetch_ops  # noqa: E402

# Any real root CA in PEM form works for the simulation. Use one the OS store
# already has (Windows / Linux / macOS all expose some via get_ca_certs).
_ROOTS = ssl.create_default_context().get_ca_certs(binary_form=True)


def _pem(der: bytes) -> str:
    return ssl.DER_cert_to_PEM_cert(der)


@unittest.skipUnless(_ROOTS, "no OS root certificates visible to Python here")
class MacKeychain(unittest.TestCase):
    def setUp(self):
        fetch_ops._ssl_ctx = None
        self.calls = []

    def tearDown(self):
        fetch_ops._ssl_ctx = None

    def fake_security(self, argv, *a, **k):
        self.calls.append(argv)
        body = "".join(_pem(d) for d in _ROOTS[:5])
        return subprocess.CompletedProcess(argv, 0, stdout="junk\n" + body + "trailing", stderr="")

    def test_empty_python_store_uses_keychain(self):
        """python.org macOS Python before 'Install Certificates': trusts nothing,
        so the Keychain's roots must be loaded."""
        empty = lambda *a, **k: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # noqa: E731
        with mock.patch.object(fetch_ops.sys, "platform", "darwin"), \
                mock.patch.object(fetch_ops.os.path, "exists", lambda p: True), \
                mock.patch.object(fetch_ops.ssl, "create_default_context", empty), \
                mock.patch.object(fetch_ops, "_has_default_cafile", lambda: False), \
                mock.patch.object(fetch_ops.subprocess, "run", self.fake_security):
            ctx = fetch_ops.ssl_context()
        self.assertGreaterEqual(len(ctx.get_ca_certs()), 1)
        self.assertTrue(any("macOS Keychain" in s for s in fetch_ops.ssl_sources))
        self.assertTrue(all(c[:3] == ["/usr/bin/security", "find-certificate", "-a"]
                            for c in self.calls), self.calls)
        self.assertEqual(len(self.calls), len(fetch_ops._MAC_KEYCHAINS))

    def test_not_macos_never_calls_security(self):
        with mock.patch.object(fetch_ops.sys, "platform", "linux"), \
                mock.patch.object(fetch_ops.subprocess, "run", self.fake_security):
            fetch_ops.ssl_context()
        self.assertEqual(self.calls, [])

    def test_split_pems(self):
        text = "x" + _pem(_ROOTS[0]) + "noise" + _pem(_ROOTS[-1])
        self.assertEqual(len(fetch_ops.split_pems(text)), 2)


if __name__ == "__main__":
    unittest.main()
