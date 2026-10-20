# -*- coding: utf-8 -*-
"""Start the bridge: one process, three long-lived threads."""

import argparse
import logging
import socket
import sys
import threading
import webbrowser

from waitress import serve

from . import config as config_mod
from . import logsetup
from .engine import Engine
from .poller import Poller
from .spool import Spool
from .version import VERSION
from .web.app import create_app

_logger = logging.getLogger(__name__)


def single_instance_guard(port):
    """Bind the loopback port before anything else.

    The listening socket *is* the lock: it is the one resource two instances
    genuinely conflict over, and the one mechanism that behaves the same on
    Windows and Linux -- no fcntl/msvcrt split and no stale-pid-file guessing.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        # Windows: SO_REUSEADDR there would let a second process bind the same
        # port, which is the opposite of a lock.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        # Without this, a restart within a minute of the last stop finds the
        # port in TIME_WAIT, concludes another copy is running and exits -- and
        # the service manager restarts it ten seconds later into the same
        # wall. On Linux it does not allow two listeners, so the lock holds.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(('127.0.0.1', port))
    except OSError:
        sock.close()
        return None
    sock.listen(64)
    return sock


def main(argv=None):
    parser = argparse.ArgumentParser(prog='zkbridge', description='ZKTeco LAN bridge')
    parser.add_argument('--data-dir', help='override where settings and state are kept')
    parser.add_argument('--open', action='store_true', help='open the page in a browser')
    parser.add_argument('--version', action='store_true')
    args = parser.parse_args(argv)

    if args.version:
        print(VERSION)
        return 0

    paths = config_mod.Paths(args.data_dir)
    paths.ensure()
    values = config_mod.load_config(paths)
    logsetup.configure(paths.log_file, values.get('log_level') or 'INFO')
    logsetup.remember_secret(values.get('token'))
    pin = config_mod.read_or_create_pin(paths)
    logsetup.remember_secret(pin)

    port = int(values.get('ui_port') or config_mod.DEFAULT_UI_PORT)
    lock = single_instance_guard(port)
    if lock is None:
        # Somebody double-clicked the icon again. Show them the page they
        # wanted rather than a second copy or an error.
        _logger.info('another instance already holds 127.0.0.1:%s', port)
        webbrowser.open('http://127.0.0.1:%s/' % port)
        return 0

    for warning in paths.harden():
        _logger.warning('%s', warning)

    spool = Spool(paths.spool_db)
    engine = Engine(spool, max_parallel=values.get('max_parallel_devices') or 4)
    status = {'state': 'starting', 'message': '', 'last_poll_at': None,
              'next_poll_at': None, 'bridge_name': None}
    poller = Poller(paths, engine, spool, status)

    app = create_app(paths, engine, status, spool)

    poll_thread = threading.Thread(target=poller.run, name='poller', daemon=True)
    poll_thread.start()

    _logger.info('ZKTeco bridge %s listening on http://127.0.0.1:%s', VERSION, port)
    if not config_mod.is_configured(values):
        # Printed, not just logged: the installer reads it back to the person
        # standing there, and it is the only moment the code is convenient.
        print('Not configured yet. Open http://127.0.0.1:%s/ — access code: %s'
              % (port, pin), flush=True)
    if args.open:
        webbrowser.open('http://127.0.0.1:%s/' % port)

    try:
        # waitress is the WSGI server here because it is pure Python and
        # identical on Windows and Linux; gunicorn is fork-based and could not
        # ship to a customer's office PC.
        serve(app, sockets=[lock], threads=4, ident='zkbridge')
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        engine.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
