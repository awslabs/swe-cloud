import json
import logging
from http import HTTPStatus
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from urllib.parse import (
    parse_qs,
    urlsplit,
)

from manifest_service.controller import (
    ManifestController,
    ServerError,
)
from manifest_service.manifests import BadRequestError

log = logging.getLogger(__name__)

manifest_path = '/v1/manifests'


class Handler(BaseHTTPRequestHandler):
    """
    Routes requests to the controller. Redirects are not sent as HTTP
    redirects. They are described in a JSON body with HTTP status 200, so that
    browser clients can follow them on their own schedule.
    """
    protocol_version = 'HTTP/1.1'
    controller = ManifestController()

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/health':
            self._respond(HTTPStatus.OK, {'up': True})
        elif url.path.startswith(manifest_path + '/'):
            token = url.path[len(manifest_path) + 1:]
            self._handle(lambda: self.controller.inspect_token(token))
        else:
            self._respond(HTTPStatus.NOT_FOUND, {'Code': 'NotFoundError', 'Message': url.path})

    def do_PUT(self):
        url = urlsplit(self.path)
        # Drain any request body, it is ignored
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)
        if url.path == manifest_path:
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            self._handle(lambda: self.controller.request_manifest(catalog=params.get('catalog'),
                                                                  format=params.get('format'),
                                                                  filters=params.get('filters')))
        else:
            self._respond(HTTPStatus.NOT_FOUND, {'Code': 'NotFoundError', 'Message': url.path})

    def _handle(self, view):
        try:
            body = view()
        except BadRequestError as e:
            self._respond(HTTPStatus.BAD_REQUEST, {'Code': 'BadRequestError', 'Message': str(e)})
        except ServerError as e:
            message = e.args[0] if len(e.args) == 1 else str(e.args)
            log.error('Request failed: %s', message)
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR,
                          {'Code': 'ServerError', 'Message': message})
        except Exception:
            log.exception('Unexpected error')
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR,
                          {'Code': 'InternalServerError', 'Message': 'An internal server error occurred.'})
        else:
            self._respond(HTTPStatus.OK, body)

    def _respond(self, status: HTTPStatus, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        log.info('%s - %s', self.address_string(), format % args)


def serve(host: str, port: int):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log.info('Manifest service listening on %s:%d', host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
