# -*- coding: utf-8 -*-
"""The page the person at the customer's office looks at.

Bound to 127.0.0.1 with no option to change it. There is no legitimate reason
for this to be reachable from the LAN, and an option to make it so would
eventually be switched on by somebody.

Loopback is not an authorisation boundary on a machine other people log in to,
so four separate things guard it: an access code that lives in the admin-only
config directory, a signed session cookie, a Host/Origin check against DNS
rebinding, and a CSRF token on everything that changes state.
"""

import json
import logging
import secrets
import time

from collections import deque

import requests

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

from .. import config as config_mod
from ..i18n import STRINGS, direction, t
from ..logsetup import remember_secret, tail
from ..version import PROTOCOL_VERSION, VERSION

_logger = logging.getLogger(__name__)

PIN_WINDOW = 300
PIN_MAX_ATTEMPTS = 5


def create_app(paths, engine, status, spool):
    app = Flask(__name__)
    identity = config_mod.install_identity(paths)
    app.secret_key = identity['session_secret']
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      PERMANENT_SESSION_LIFETIME=12 * 3600)
    attempts = deque()

    def settings():
        return config_mod.load_config(paths)

    def lang():
        return settings().get('ui_lang') or 'en'

    def render(template, **context):
        values = settings()
        language = values.get('ui_lang') or 'en'
        return render_template(
            template, t=lambda key: t(key, language), lang=language,
            dir=direction(language), version=VERSION, cfg=values,
            status=status, csrf=session.get('csrf', ''), **context)

    # -- guards ----------------------------------------------------

    @app.before_request
    def _guard():
        port = settings().get('ui_port') or config_mod.DEFAULT_UI_PORT
        allowed_hosts = {'127.0.0.1:%s' % port, 'localhost:%s' % port}
        host = request.headers.get('Host') or ''
        if host not in allowed_hosts:
            # A page in this PC's browser could otherwise be steered at
            # 127.0.0.1 by whatever site the user is visiting.
            abort(403)
        if request.method not in ('GET', 'HEAD'):
            origin = request.headers.get('Origin') or request.headers.get('Referer') or ''
            if origin and not any(('//%s' % h) in origin for h in allowed_hosts):
                abort(403)
            if request.endpoint not in ('sign_in',):
                sent = request.form.get('csrf') or request.headers.get('X-CSRF-Token')
                if not sent or not secrets.compare_digest(sent, session.get('csrf', '')):
                    abort(403)
        if request.endpoint in ('sign_in', 'static'):
            return None
        if not session.get('ok'):
            return redirect(url_for('sign_in'))
        if not config_mod.is_configured(settings()) and request.endpoint not in (
                'setup', 'api_status'):
            return redirect(url_for('setup'))
        return None

    @app.after_request
    def _headers(response):
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:")
        return response

    # -- sign in ---------------------------------------------------

    @app.route('/signin', methods=['GET', 'POST'])
    def sign_in():
        error = ''
        if request.method == 'POST':
            now = time.time()
            while attempts and now - attempts[0] > PIN_WINDOW:
                attempts.popleft()
            if len(attempts) >= PIN_MAX_ATTEMPTS:
                error = t('too_many', lang())
            else:
                attempts.append(now)
                expected = config_mod.read_or_create_pin(paths)
                if secrets.compare_digest(request.form.get('pin', '').strip().upper(),
                                          expected):
                    session['ok'] = True
                    session['csrf'] = secrets.token_urlsafe(32)
                    session.permanent = True
                    return redirect(url_for('home'))
                error = t('wrong_pin', lang())
        return render('pin.html', error=error)

    # -- pages -----------------------------------------------------

    @app.route('/')
    def home():
        return render('status.html', snapshot=engine.snapshot(), spool=spool.stats())

    @app.route('/setup', methods=['GET', 'POST'])
    def setup():
        message = error = ''
        values = settings()
        if request.method == 'POST':
            candidate = dict(values)
            candidate.update(
                odoo_base_url=(request.form.get('odoo_base_url') or '').strip().rstrip('/'),
                token=(request.form.get('token') or '').strip(),
                bridge_label=(request.form.get('bridge_label') or '').strip(),
                ui_lang=request.form.get('ui_lang') or 'en',
            )
            ok, detail = _probe(candidate)
            if ok:
                config_mod.save_config(paths, candidate)
                remember_secret(candidate['token'])
                engine.request_poll_now()
                message = t('test_ok', candidate['ui_lang'])
                values = candidate
            else:
                # A token Odoo rejects is never written over a working one.
                error = t('test_failed', candidate['ui_lang']) + detail
                values = candidate
        return render('setup.html', message=message, error=error, values=values,
                      languages=sorted(STRINGS))

    @app.route('/devices')
    def devices():
        return render('devices.html', snapshot=engine.snapshot())

    @app.route('/logs')
    def logs():
        return render('logs.html', lines=tail(paths.log_file, 300))

    @app.route('/settings', methods=['GET', 'POST'])
    def settings_page():
        message = error = ''
        values = settings()
        if request.method == 'POST':
            candidate = dict(values)
            interval = (request.form.get('poll_interval_s') or '').strip()
            candidate.update(
                odoo_base_url=(request.form.get('odoo_base_url') or '').strip().rstrip('/'),
                bridge_label=(request.form.get('bridge_label') or '').strip(),
                ui_lang=request.form.get('ui_lang') or 'en',
                log_level=request.form.get('log_level') or 'INFO',
                poll_interval_s=int(interval) if interval.isdigit() else None,
            )
            token = (request.form.get('token') or '').strip()
            if token:
                candidate['token'] = token
            ok, detail = _probe(candidate)
            if ok:
                config_mod.save_config(paths, candidate)
                remember_secret(candidate['token'])
                engine.request_poll_now()
                message = t('saved', candidate['ui_lang'])
                values = candidate
            else:
                error = t('test_failed', candidate['ui_lang']) + detail
        return render('settings.html', message=message, error=error, values=values,
                      languages=sorted(STRINGS))

    # -- small api the page polls ----------------------------------

    @app.route('/api/status')
    def api_status():
        return jsonify(dict(status, snapshot=engine.snapshot(), spool=spool.stats(),
                            version=VERSION, protocol=PROTOCOL_VERSION))

    @app.route('/api/poll-now', methods=['POST'])
    def api_poll_now():
        engine.request_poll_now()
        return jsonify({'ok': True})

    @app.route('/api/test-device', methods=['POST'])
    def api_test_device():
        """Submit a local job and return at once.

        A handler must never touch a socket: one unreachable device would
        otherwise freeze a web worker for ten seconds and tell the operator
        nothing.
        """
        key = (request.get_json(silent=True) or {}).get('device_key')
        block = (engine.snapshot()['devices'].get(key) or {}).get('block')
        if not block:
            return jsonify({'ok': False, 'error': 'unknown device'}), 404
        engine.submit({'job_id': 0, 'operation': 'identity', 'device': block,
                       'request': {}, '_source': 'local'}, source='local')
        return jsonify({'ok': True})

    def _probe(values):
        """One real poll, to prove the address and the token before saving."""
        base = (values.get('odoo_base_url') or '').rstrip('/')
        if not base:
            return False, 'no address'
        if base.startswith('http://') and not values.get('allow_insecure'):
            return False, 'refusing plain HTTP'
        try:
            response = requests.post(
                '%s/zkteco/bridge/v1/poll' % base,
                data=json.dumps({'protocol': PROTOCOL_VERSION, 'agent_version': VERSION,
                                 'capabilities': [], 'max_jobs': 0}),
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer %s' % values.get('token', '')},
                timeout=(10, 30),
                verify=values.get('ca_bundle') or True)
        except Exception as exc:
            return False, str(exc)[:200]
        if response.status_code in (200, 429):
            return True, ''
        if response.status_code == 401:
            return False, 'the token was rejected'
        return False, 'HTTP %s' % response.status_code

    return app
