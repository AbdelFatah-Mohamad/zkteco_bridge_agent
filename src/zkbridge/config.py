# -*- coding: utf-8 -*-
"""Where the bridge keeps its settings and its state, per platform.

On Windows this is %ProgramData%, never %APPDATA%: a service account's APPDATA
is C:\\Windows\\System32\\config\\systemprofile\\AppData\\Roaming, which is not
where anyone will look and not where the installer can put anything useful.
"""

import json
import os
import pathlib
import platform
import secrets
import stat
import sys
import uuid

APP_DIR_NAME = 'ZKBridge'
VENDOR_DIR_NAME = 'AlshayebCo'
DEFAULT_UI_PORT = 8709

# Unambiguous alphabet: no O/0, no I/1/l. Somebody reads this off a screen and
# types it on another one.
PIN_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
PIN_LENGTH = 10


def _is_windows():
    return platform.system() == 'Windows'


class Paths(object):
    """Every file the agent owns, decided once."""

    def __init__(self, root=None):
        if root:
            self.root = pathlib.Path(root)
        elif _is_windows():
            base = os.environ.get('ProgramData', r'C:\ProgramData')
            self.root = pathlib.Path(base) / VENDOR_DIR_NAME / APP_DIR_NAME
        else:
            # Matches systemd's ConfigurationDirectory/StateDirectory/LogsDirectory
            # so the unit can create them with the right owner.
            self.root = pathlib.Path('/etc/zkbridge')
            self.state_root = pathlib.Path('/var/lib/zkbridge')
            self.log_root = pathlib.Path('/var/log/zkbridge')
        if _is_windows() or root:
            self.state_root = self.root
            self.log_root = self.root / 'logs'

    @property
    def config_file(self):
        return self.root / 'config.json'

    @property
    def pin_file(self):
        return self.root / 'ui_pin.txt'

    @property
    def state_dir(self):
        return self.state_root / 'state'

    @property
    def spool_db(self):
        return self.state_root / 'state' / 'bridge_state.db'

    @property
    def log_file(self):
        return self.log_root / 'zkbridge.log'

    def ensure(self):
        for folder in (self.root, self.state_dir, self.log_root):
            folder.mkdir(parents=True, exist_ok=True)
        self.harden()

    def harden(self):
        """Make the directory unreadable to other users of this PC.

        The token lives here in plaintext -- see the note in load_config -- so
        this is the only thing standing between it and anyone with a login.
        """
        warnings = []
        if _is_windows():
            # ProgramData grants Users: Read by inheritance, which would leave
            # the token world-readable on a shared office PC.
            import subprocess
            try:
                subprocess.run(
                    ['icacls', str(self.root), '/inheritance:r',
                     '/grant:r', 'SYSTEM:(OI)(CI)F',
                     '/grant:r', 'BUILTIN\\Administrators:(OI)(CI)F'],
                    check=False, capture_output=True, timeout=30)
            except Exception as exc:                      # pragma: no cover
                warnings.append('could not restrict %s: %s' % (self.root, exc))
        else:
            for folder in (self.root, self.state_dir):
                try:
                    os.chmod(folder, stat.S_IRWXU)
                except OSError as exc:
                    warnings.append('could not chmod %s: %s' % (folder, exc))
            for path in (self.config_file, self.pin_file):
                if path.exists():
                    try:
                        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                    except OSError as exc:
                        warnings.append('could not chmod %s: %s' % (path, exc))
        return warnings


DEFAULTS = {
    'schema': 1,
    'odoo_base_url': '',
    'token': '',
    'bridge_label': '',
    'poll_interval_s': None,      # None follows the server
    'log_level': 'INFO',
    'ui_port': DEFAULT_UI_PORT,
    'ui_lang': 'en',
    'allow_insecure': False,
    'ca_bundle': None,
    'max_parallel_devices': 4,
}


def load_config(paths):
    """The stored settings, or the defaults when nothing has been set up yet.

    The token is plaintext, protected by the filesystem ACL and nothing else.
    DPAPI and keyring are deliberately rejected: DPAPI ties the ciphertext to
    the service account, so changing that account or restoring the PC from an
    image silently bricks the bridge, and the recovery is "re-enter the token"
    -- which is exactly what plaintext costs anyway, without a Windows-only
    code path. Anyone who can read this directory can read the token; that set
    is administrators, and the answer is one-click revocation in Odoo.
    """
    values = dict(DEFAULTS)
    if paths.config_file.exists():
        try:
            values.update(json.loads(paths.config_file.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            pass
    return values


def save_config(paths, values):
    """Write atomically: a half-written config file is a bridge that will not start."""
    paths.ensure()
    tmp = paths.config_file.with_suffix('.tmp')
    tmp.write_text(json.dumps(values, indent=2, ensure_ascii=False), encoding='utf-8')
    if not _is_windows():
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, paths.config_file)
    return values


BRIDGE_PATH = '/zkteco/bridge/v1'


def normalise_base_url(value):
    """The Odoo address, however it was pasted.

    Odoo's bridge form shows the full endpoint (".../zkteco/bridge/v1") under
    "What to enter on the PC", and the program appends that path itself. Taking
    the label at its word would produce ".../v1/zkteco/bridge/v1/poll" and a
    404 that nobody at the customer's office could diagnose, so both forms are
    accepted.
    """
    base = (value or '').strip().rstrip('/')
    for suffix in (BRIDGE_PATH + '/poll', BRIDGE_PATH + '/result', BRIDGE_PATH):
        if base.endswith(suffix):
            base = base[:-len(suffix)].rstrip('/')
            break
    return base


def is_configured(values):
    return bool(values.get('odoo_base_url') and values.get('token'))


def read_or_create_pin(paths):
    """The code that guards the local page.

    127.0.0.1 is not an authorisation boundary on a machine other people log in
    to. Reaching the page therefore needs either the ability to read this file
    -- which means administrator -- or to have been told the code by one.
    """
    paths.ensure()
    if paths.pin_file.exists():
        pin = paths.pin_file.read_text(encoding='utf-8').strip()
        if pin:
            return pin
    pin = ''.join(secrets.choice(PIN_ALPHABET) for _ in range(PIN_LENGTH))
    paths.pin_file.write_text(pin, encoding='utf-8')
    if not _is_windows():
        os.chmod(paths.pin_file, stat.S_IRUSR | stat.S_IWUSR)
    return pin


def install_identity(paths):
    """A uuid for this installation, kept in state.

    Not authentication. It is what lets Odoo notice one token polling from two
    installations, and what tells it a re-imaged PC is a re-image rather than a
    duplicate.
    """
    paths.ensure()
    path = paths.state_dir / 'install.json'
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding='utf-8'))
            if stored.get('install_id') and stored.get('session_secret'):
                return stored
        except (OSError, ValueError):
            pass
    stored = {'install_id': uuid.uuid4().hex,
              'session_secret': secrets.token_hex(32)}
    path.write_text(json.dumps(stored), encoding='utf-8')
    if not _is_windows():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return stored


def describe_host():
    return {
        'hostname': platform.node()[:128],
        'os': ('%s-%s' % (platform.system(), platform.release()))[:128],
        'python': sys.version.split()[0],
    }
