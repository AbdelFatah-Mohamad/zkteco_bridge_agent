# -*- coding: utf-8 -*-
"""Identity the agent reports on every poll."""

import hashlib
import pathlib

VERSION = '1.1.2'
PROTOCOL_VERSION = 1

# Operations this build can actually carry out. The server never hands out a
# job that is not declared here, so an older agent keeps doing attendance while
# the one thing it cannot do waits, named, in the queue.
CAPABILITIES = ['identity', 'list_users', 'pull_attendance',
                'push_users', 'delete_user', 'enroll_finger']


def zk_fingerprint():
    """Hash of the vendored protocol library.

    Reported to Odoo so a divergence between this copy and the module's becomes
    visible to a human, instead of a mysterious failure a year later.
    """
    folder = pathlib.Path(__file__).parent / 'vendor' / 'zk'
    digest = hashlib.sha256()
    for path in sorted(folder.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
