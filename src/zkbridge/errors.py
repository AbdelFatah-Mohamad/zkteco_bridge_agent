# -*- coding: utf-8 -*-
"""Turning what the protocol library raises into something a human can act on.

The vendored library collapses every exception in ``__send_command`` into
``ZKNetworkError(str(e))``, so a socket timeout, a refused connection and a
malformed header all arrive as the same type and are distinguishable only by
their message text. Type-based classification is therefore impossible and the
bridge has to earn the distinction itself: a TCP pre-flight first, then string
markers taken from the vendored source.

That coupling is a second, independent reason the two copies of ``zk/`` must not
drift -- a divergence would break classification silently and turn every failure
into ``bridge_error``.
"""

UNREACHABLE = 'device_unreachable'
AUTH_FAILED = 'device_auth_failed'
PROTOCOL_ERROR = 'device_protocol_error'
BUSY = 'device_busy'
IDENTITY_MISMATCH = 'device_identity_mismatch'
PAYLOAD_REJECTED = 'payload_rejected'
BRIDGE_ERROR = 'bridge_error'

# Literals that must still exist in vendor/zk/base.py. A test asserts they do.
MARKERS = (
    (AUTH_FAILED, ('unauthenticated', 'unauth')),
    (UNREACHABLE, ('tcp error', "can't reach device", 'connection refused',
                   'no route to host', 'network is unreachable')),
    (PROTOCOL_ERROR, ('tcp packet invalid', 'tcp header incomplete',
                      'udp header incomplete', 'invalid response',
                      'unpack', 'received a bad response')),
    (BUSY, ("can't set user", "can't delete user", "can't read sizes",
            "can't read users", 'device is busy')),
)


class PreflightError(Exception):
    """The TCP probe failed, which is the only reliable 'unreachable' signal."""


def classify(exc):
    """Return (code, detail). Never raises."""
    if isinstance(exc, PreflightError):
        return UNREACHABLE, str(exc)[:500]
    text = str(exc or '').lower()
    if 'timed out' in text or 'timeout' in text:
        # A timeout after a successful probe is the device going quiet
        # mid-conversation, not an unplugged cable.
        return PROTOCOL_ERROR if 'connect' not in text else UNREACHABLE, str(exc)[:500]
    for code, markers in MARKERS:
        if any(marker in text for marker in markers):
            return code, str(exc)[:500]
    return BRIDGE_ERROR, str(exc)[:500]
