"""A local certificate authority made with the system `openssl`, for HTTPS on the LAN.

Browsers grant the microphone and app install only over HTTPS. baabaa creates its own CA once and
issues a server certificate for this host's names and addresses; install the CA certificate
(`/ca.crt`) on each device once. The server certificate is reissued when the addresses change.
"""

import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

SERVER_DAYS = 397  # browsers reject server certificates valid for longer than 398 days


class TLSError(Exception):
    pass


def _openssl(*args, cwd=None):
    exe = shutil.which("openssl")
    if not exe:
        raise TLSError("The openssl command is needed for HTTPS (or run with --http).")
    res = subprocess.run([exe, *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise TLSError(f"openssl {args[0]} failed: {res.stderr.strip()[:300]}")
    return res.stdout


# Only long-standing options, so the same commands work with OpenSSL and with macOS's LibreSSL: keys from
# `ecparam`, extensions from a configuration file (no -addext, no -pkeyopt).
CONF = """[req]
distinguished_name = dn
[dn]
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"""


def _ec_key(path: Path) -> None:
    _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(path))
    os.chmod(path, 0o600)


def ensure_ca(folder: Path, hostname: str) -> tuple[Path, Path]:
    key, crt = folder / "ca.key", folder / "ca.crt"
    if key.exists() and crt.exists():
        return key, crt
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "ca.cnf"
        conf.write_text(CONF)
        _ec_key(key)
        _openssl("req", "-x509", "-new", "-key", str(key), "-out", str(crt), "-days", "3650", "-sha256",
                 "-subj", f"/CN=baabaa local CA ({hostname})/O=baabaa", "-config", str(conf), "-extensions", "v3_ca")
    return key, crt


def ensure_server_cert(folder: Path, hostname: str, names: list[str], addresses: list[str]) -> tuple[Path, Path]:
    ca_key, ca_crt = ensure_ca(folder, hostname)
    key, crt, meta_path = folder / "server.key", folder / "server.crt", folder / "server.json"
    sans = sorted({f"DNS:{n}" for n in names} | {f"IP:{a}" for a in addresses})
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    fresh = meta.get("sans") == sans and time.time() < meta.get("renew_after", 0)
    if key.exists() and crt.exists() and fresh:
        return key, crt
    with tempfile.TemporaryDirectory() as tmp:
        csr, conf, ext = Path(tmp) / "server.csr", Path(tmp) / "req.cnf", Path(tmp) / "ext.cnf"
        conf.write_text(CONF)
        ext.write_text("[v3_server]\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
                       f"subjectAltName={','.join(sans)}\n")
        _ec_key(key)
        _openssl("req", "-new", "-key", str(key), "-out", str(csr), "-subj", f"/CN={hostname}/O=baabaa", "-config", str(conf))
        _openssl("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
                 "-out", str(crt), "-days", str(SERVER_DAYS), "-sha256", "-extfile", str(ext), "-extensions", "v3_server")
    meta_path.write_text(json.dumps({"sans": sans, "renew_after": time.time() + (SERVER_DAYS - 30) * 86400}))
    return key, crt


def server_context(key: Path, crt: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(crt), str(key))
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx
