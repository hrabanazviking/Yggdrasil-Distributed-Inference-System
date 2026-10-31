"""Ed25519 signatures — a pure-Python implementation of RFC 8032.

Y-011 chose this deliberately: the mesh's identity foundation must
run on every node class (Pi to server) with zero native dependencies,
and certificate issuance/verification is rare, small work — a few
signatures at join time, not a hot path. Correctness is proven
against the RFC 8032 test vectors in tests/test_identity.py.

HONEST LIMIT: this implementation is NOT constant-time and must
never be presented as side-channel resistant. If a measured need
for high-rate or hardened signing appears, ADR-001's compiled-path
criteria govern replacing this module behind its tiny interface
(generate_seed / public_key / sign / verify).
"""

import hashlib

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _Q - 2, _Q)


_D = (-121665 * _inv(121666)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = (4 * _inv(5)) % _Q
_B = (_xrecover(_BY), _BY)


def _edwards(p: tuple[int, int], q_: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = q_
    denom = _inv(1 + _D * x1 * x2 * y1 * y2)
    x3 = (x1 * y2 + x2 * y1) * denom % _Q
    denom_y = _inv(1 - _D * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * denom_y % _Q
    return x3, y3


def _scalarmult(p: tuple[int, int], e: int) -> tuple[int, int]:
    if e == 0:
        return (0, 1)
    q_ = _scalarmult(p, e // 2)
    q_ = _edwards(q_, q_)
    if e & 1:
        q_ = _edwards(q_, p)
    return q_


def _encode_int(y: int) -> bytes:
    return y.to_bytes(32, "little")


def _encode_point(p: tuple[int, int]) -> bytes:
    x, y = p
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    out = bytearray(32)
    for i in range(256):
        if bits[i]:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _decode_int(data: bytes) -> int:
    return int.from_bytes(data, "little")


def _decode_point(data: bytes) -> tuple[int, int]:
    if len(data) != 32:
        raise ValueError("encoded point must be 32 bytes")
    y = sum(2**i * ((data[i // 8] >> (i % 8)) & 1) for i in range(255))
    sign = (data[31] >> 7) & 1
    x = _xrecover(y)
    if x & 1 != sign:
        x = _Q - x
    return (x, y)


def _secret_expand(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = 2**254 + sum(2**i * ((h[i // 8] >> (i % 8)) & 1) for i in range(3, 254))
    return a, h[32:]


def generate_seed() -> bytes:
    """A fresh 32-byte private seed from the OS CSPRNG."""
    import os

    return os.urandom(32)


def public_key(seed: bytes) -> bytes:
    """Derive the 32-byte public key for a 32-byte seed."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    a, _ = _secret_expand(seed)
    return _encode_point(_scalarmult(_B, a))


def sign(seed: bytes, message: bytes) -> bytes:
    """Produce the 64-byte Ed25519 signature of message under seed."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    a, prefix = _secret_expand(seed)
    pk = _encode_point(_scalarmult(_B, a))
    r = _decode_int(hashlib.sha512(prefix + message).digest()) % _L
    r_enc = _encode_point(_scalarmult(_B, r))
    k = _decode_int(hashlib.sha512(r_enc + pk + message).digest()) % _L
    s = (r + k * a) % _L
    return r_enc + _encode_int(s)


def verify(pk: bytes, message: bytes, signature: bytes) -> bool:
    """True iff signature is a valid Ed25519 signature of message."""
    if len(pk) != 32 or len(signature) != 64:
        return False
    try:
        a_point = _decode_point(pk)
        r_point = _decode_point(signature[:32])
    except ValueError:
        return False
    s = _decode_int(signature[32:])
    if s >= _L:
        return False
    k = _decode_int(hashlib.sha512(signature[:32] + pk + message).digest()) % _L
    left = _scalarmult(_B, s)
    right = _edwards(r_point, _scalarmult(a_point, k))
    return left == right
