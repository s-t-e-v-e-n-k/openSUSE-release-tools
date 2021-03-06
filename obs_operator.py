#!/usr/bin/python3 -u
# Without the -u option for unbuffered output nothing shows up in journal or
# kubernetes logs.

import argparse
import http
from http.cookies import SimpleCookie
from http.cookiejar import Cookie, LWPCookieJar
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import json
import tempfile
import os
from osc import conf
from osclib import common
from osclib.sentry import sentry_client
from osclib.sentry import sentry_init
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.parse import parse_qs

# A cookie with an invalid key is intermittently generated on opensuse.org
# domain which causes the operator to crash when parsing the cookie. The desired
# cookie is valid, but cannot be utilize due to the exception. As suggested in
# https://stackoverflow.com/a/47012250, workaround by making EVERYTHING LEGAL!
http.cookies._is_legal_key = lambda _: True

sentry_sdk = sentry_init()

# Available in python 3.7.
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    def handle_error(self, request, client_address):
        super().handle_error(request, client_address)
        sentry_sdk.capture_exception()

class RequestHandler(BaseHTTPRequestHandler):
    COOKIE_NAME = 'openSUSE_session' # Both OBS and IBS.
    GET_PATHS = [
        'origin/config',
        'origin/history',
        'origin/list',
        'origin/package',
        'origin/potentials',
        'origin/projects',
        'origin/report',
        'package/diff',
    ]
    POST_PATHS = [
        'request/submit',
        'staging/select',
    ]

    def do_OPTIONS(self):
        try:
            with OSCRequestEnvironment(self, require_session=False) as oscrc_file:
                self.send_header('Access-Control-Allow-Methods', 'GET, POST')
                self.send_header('Access-Control-Allow-Headers', 'Access-Control-Allow-Origin, Content-Type, X-Requested-With')
        except OSCRequestEnvironmentException as e:
            self.send_header('Allow', 'OPTIONS, GET, POST')
        self.end_headers()

    def do_GET(self):
        url_parts = urlparse(self.path)

        path = url_parts.path.lstrip('/')
        path_parts = path.split('/')
        path_prefix = '/'.join(path_parts[:2])

        query = parse_qs(url_parts.query)

        if path_prefix == '':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()

            self.write_string('namespace: {}\n'.format(common.NAME))
            self.write_string('name: {}\n'.format('OBS Operator'))
            self.write_string('version: {}\n'.format(common.VERSION))
            return

        if len(path_parts) < 3 or path_prefix not in self.GET_PATHS:
            self.send_response(404)
            self.end_headers()
            return

        try:
            with OSCRequestEnvironment(self) as oscrc_file:
                func = getattr(self, 'handle_{}'.format(path_prefix.replace('/', '_')))
                command = func(path_parts[2:], query)

                self.end_headers()
                if command and not self.execute(oscrc_file, command):
                    self.write_string('failed')
        except OSCRequestEnvironmentException as e:
            self.write_string(str(e))

    def do_POST(self):
        url_parts = urlparse(self.path)

        path = url_parts.path.lstrip('/')
        path_parts = path.split('/')
        path_prefix = '/'.join(path_parts[:2])

        query = parse_qs(url_parts.query)

        if len(path_parts) < 2 or path_prefix not in self.POST_PATHS:
            self.send_response(404)
            self.end_headers()
            return

        data = self.data_parse()
        user = data.get('user')
        if self.debug:
            print('data: {}'.format(data))

        try:
            with OSCRequestEnvironment(self, user) as oscrc_file:
                func = getattr(self, 'handle_{}'.format(path_prefix.replace('/', '_')))
                commands = func(path_parts[2:], query, data)
                self.end_headers()

                for command in commands:
                    self.write_string('$ {}\n'.format(' '.join(command)))
                    if not self.execute(oscrc_file, command):
                        self.write_string('failed')
                        break
        except OSCRequestEnvironmentException as e:
            self.write_string(str(e))

    def data_parse(self):
        if int(self.headers['Content-Length']) == 0:
            return {}
        data = self.rfile.read(int(self.headers['Content-Length']))
        return json.loads(data.decode('utf-8'))

    def apiurl_get(self):
        if self.apiurl:
            return self.apiurl

        host = self.headers.get('Host')
        if not host:
            return None

        # Strip port if present.
        domain = host.split(':', 2)[0]
        if '.' not in domain:
            return None

        # Remove first subdomain and replace with api subdomain.
        domain_parent = '.'.join(domain.split('.')[-2:])
        return 'https://api.{}'.format(domain_parent)

    def origin_domain_get(self):
        origin = self.headers.get('Origin')
        if origin is not None:
            # Strip port if present.
            domain = urlparse(origin).netloc.split(':', 2)[0]
            if '.' in domain:
                return '.'.join(domain.split('.')[-2:])

        return None

    def session_get(self):
        if self.session:
            return self.session
        else:
            cookie = self.headers.get('Cookie')
            if cookie:
                cookie = SimpleCookie(cookie)
                if self.COOKIE_NAME in cookie:
                    return cookie[self.COOKIE_NAME].value

        return None

    def oscrc_create(self, oscrc_file, apiurl, cookiejar_file, user):
        sentry_dsn = sentry_client().dsn
        sentry_environment = sentry_client().options.get('environment')

        oscrc_file.write('\n'.join([
            '[general]',
            # Passthru sentry_sdk options to allow for reporting on subcommands.
            'sentry_sdk.dsn = {}'.format(sentry_dsn) if sentry_dsn else '',
            'sentry_sdk.environment = {}'.format(sentry_environment) if sentry_environment else '',
            'apiurl = {}'.format(apiurl),
            'cookiejar = {}'.format(cookiejar_file.name),
            'staging.color = 0',
            '[{}]'.format(apiurl),
            'user = {}'.format(user),
            'pass = invalid',
            '',
        ]).encode('utf-8'))
        oscrc_file.flush()

        # In order to avoid osc clearing the cookie file the modified time of
        # the oscrc file must be set further into the past.
        # if int(round(config_mtime)) > int(os.stat(cookie_file).st_mtime):
        recent_past = time.time() - 3600
        os.utime(oscrc_file.name, (recent_past, recent_past))

    def cookiejar_create(self, cookiejar_file, session):
        cookie_jar = LWPCookieJar(cookiejar_file.name)
        cookie_jar.set_cookie(Cookie(0, self.COOKIE_NAME, session,
            None, False,
            '', False, True,
            '/', True,
            True,
            None, None, None, None, {}))
        cookie_jar.save()
        cookiejar_file.flush()

    def execute(self, oscrc_file, command):
        env = os.environ
        env['OSC_CONFIG'] = oscrc_file.name

        # Would be preferrable to stream incremental output, but python http
        # server does not seem to support this easily.
        result = subprocess.run(command, env=env, stdout=self.wfile, stderr=self.wfile)
        return result.returncode == 0

    def write_string(self, string):
        self.wfile.write(string.encode('utf-8'))

    def command_format_add(self, command, query):
        format = None
        if self.headers.get('Accept'):
            format = self.headers.get('Accept').split('/', 2)[1]
            if format != 'json' and format != 'yaml':
                format = None
        if not format and 'format' in query:
            format = query['format'][0]
        if format:
            command.append('--format')
            command.append(format)

    def handle_origin_config(self, args, query):
        command = ['osc', 'origin', '-p', args[0], 'config']
        if 'origins-only' in query:
            command.append('--origins-only')
        return command

    def handle_origin_history(self, args, query):
        command = ['osc', 'origin', '-p', args[0], 'history']
        self.command_format_add(command, query)
        if len(args) > 1:
            command.append(args[1])
        return command

    def handle_origin_list(self, args, query):
        command = ['osc', 'origin', '-p', args[0], 'list']
        if 'force-refresh' in query:
            command.append('--force-refresh')
        self.command_format_add(command, query)
        return command

    def handle_origin_package(self, args, query):
        command = ['osc', 'origin', '-p', args[0], 'package']
        if 'debug' in query:
            command.append('--debug')
        if len(args) > 1:
            command.append(args[1])
        return command

    def handle_origin_potentials(self, args, query):
        command = ['osc', 'origin', '-p', args[0], 'potentials']
        self.command_format_add(command, query)
        if len(args) > 1:
            command.append(args[1])
        return command

    def handle_origin_projects(self, args, query):
        command = ['osc', 'origin', 'projects']
        self.command_format_add(command, query)
        return command

    def handle_origin_report(self, args, query):
        command = ['osc', 'origin', '-p', args[0], 'report']
        if 'force-refresh' in query:
            command.append('--force-refresh')
        return command

    def handle_package_diff(self, args, query):
        # source_project source_package target_project [target_package] [source_revision] [target_revision]
        command = ['osc', 'rdiff', args[0], args[1], args[2]] # len(args) == 3
        if len(args) >= 4:
            command.append(args[3]) # target_package
        if len(args) >= 5:
            command.append('--revision')
            command.append(':'.join(args[4:6]))
        return command

    def handle_request_submit(self, args, query, data):
        command = ['osc', 'sr', args[0], args[1], args[2]]
        command.append('-m')
        if 'message' in query and query['message'][0]:
            command.append(query['message'][0])
        else:
            command.append('created via operator')
        command.append('--yes')
        return [command]

    def staging_command(self, project, subcommand):
        return ['osc', 'staging', '-p', project, subcommand]

    def handle_staging_select(self, args, query, data):
        for staging, requests in data['selection'].items():
            command = self.staging_command(data['project'], 'select')
            if 'move' in data and data['move']:
                command.append('--move')
            command.append(staging)
            command.extend(requests)
            yield command

class OSCRequestEnvironment(object):
    def __init__(self, handler, user=None, require_session=True):
        self.handler = handler
        self.user = user
        self.require_session = require_session

    def __enter__(self):
        apiurl = self.handler.apiurl_get()
        origin_domain = self.handler.origin_domain_get()
        if not apiurl or (not self.handler.apiurl and origin_domain and not apiurl.endswith(origin_domain)):
            self.handler.send_response(400)
            self.handler.end_headers()
            if not apiurl:
                raise OSCRequestEnvironmentException('unable to determine apiurl')
            else:
                raise OSCRequestEnvironmentException('origin does not match host domain')

        session = self.handler.session_get()
        if self.require_session and not session:
            self.handler.send_response(401)
            self.handler.end_headers()
            raise OSCRequestEnvironmentException('unable to determine session')

        if self.handler.debug:
            print('apiurl: {}'.format(apiurl))
            print('session: {}'.format(session))

        self.handler.send_response(200)
        self.handler.send_header('Content-type', 'text/plain')
        if origin_domain:
            self.handler.send_header('Access-Control-Allow-Credentials', 'true')
            self.handler.send_header('Access-Control-Allow-Origin', self.handler.headers.get('Origin'))

        self.cookiejar_file = tempfile.NamedTemporaryFile()
        self.oscrc_file = tempfile.NamedTemporaryFile()

        self.cookiejar_file.__enter__()
        self.oscrc_file.__enter__()

        self.handler.oscrc_create(self.oscrc_file, apiurl, self.cookiejar_file, self.user)
        self.handler.cookiejar_create(self.cookiejar_file, session)

        return self.oscrc_file

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cookiejar_file.__exit__(exc_type, exc_val, exc_tb)
        self.oscrc_file.__exit__(exc_type, exc_val, exc_tb)

class OSCRequestEnvironmentException(Exception):
    pass

def main(args):
    conf.get_config() # Allow sentry DSN to be available.
    sentry_sdk = sentry_init()

    RequestHandler.apiurl = args.apiurl
    RequestHandler.session = args.session
    RequestHandler.debug = args.debug

    with ThreadedHTTPServer((args.host, args.port), RequestHandler) as httpd:
        print('listening on {}:{}'.format(args.host, args.port))
        httpd.serve_forever()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='OBS Operator server used to perform staging operations.')
    parser.set_defaults(func=main)

    parser.add_argument('--host', default='', help='host name to which to bind')
    parser.add_argument('--port', type=int, default=8080, help='port number to which to bind')
    parser.add_argument('-A', '--apiurl',
        help='OBS instance API URL to use instead of basing from request origin')
    parser.add_argument('--session',
        help='session cookie value to use instead of any passed cookie')
    parser.add_argument('-d', '--debug', action='store_true',
        help='print debugging information')

    args = parser.parse_args()
    sys.exit(args.func(args))
