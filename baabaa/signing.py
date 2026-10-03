"""Ed25519 signature checks (RFC 8032), for the release list that updates come from.

The Python standard library verifies no signatures, so this is the RFC's own verification algorithm
(section 5.1.7) in plain integer arithmetic: about 4 ms per check. It only verifies; releases are
signed on the maintainer's machine with `openssl pkeyutl -sign -rawin` (docs/RELEASING.md).
"""

import hashlib

# The keys that sign baabaa's releases (raw 32-byte public keys, hex). A new key is added here in a
# release signed by an old one, so installed copies learn it before it is used.
RELEASE_KEYS = {
    "e5a15f0ca0668d23": "e5a15f0ca0668d23a9577e058bc10444b12e04a682bb45141c1bb8fd040f8c46",
}

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _add(p, q):  # points in extended coordinates (X, Y, Z, T)
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(p, q) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _decompress(b: bytes):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_recover_x(_GY, 0), _GY, 1, _recover_x(_GY, 0) * _GY % _P)


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True when `signature` is a valid Ed25519 signature of `message` by `public`."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decompress(public)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


def key_id(public_hex: str) -> str:
    return public_hex[:16]


def verify_release_file(data: bytes, signature_text: str, keys: dict | None = None) -> str | None:
    """Check a release file against its signature file ("<key id> <signature hex>"). Returns the id of
    the key that signed it, or None when no trusted key did."""
    keys = RELEASE_KEYS if keys is None else keys
    parts = (signature_text or "").split()
    if len(parts) != 2 or parts[0] not in keys:
        return None
    try:
        sig = bytes.fromhex(parts[1])
        public = bytes.fromhex(keys[parts[0]])
    except ValueError:
        return None
    return parts[0] if verify(public, data, sig) else None
