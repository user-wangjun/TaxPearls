"""Compress UI text and complete graph JSON; forward business streams untouched."""
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipMiddleware


def encoding_quality(header: str, coding: str) -> float:
    qualities = {}
    for item in header.split(','):
        name, *parameters = item.strip().lower().split(';')
        name = name.strip()
        quality = next((p.strip()[2:].strip() for p in parameters if p.strip().startswith('q=')), '1')
        try:
            value = float(quality)
            qualities[name] = value if 0 <= value <= 1 else 0
        except ValueError:
            qualities[name] = 0
    return qualities.get(coding, qualities.get('*', 0))


class StaticCompressionMiddleware:
    def __init__(self, app):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=1024, compresslevel=5)
        self.graph_compressed = GZipMiddleware(app, minimum_size=1024, compresslevel=1)

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        graph_json = path in {'/api/knowledge/graph', '/api/training/knowledge/graph'}
        public_text = (
            path in {'/', '/logo', '/favicon.ico', '/training-certificates', '/training-staff',
                     '/vendor/three.module.mjs'}
            or path.startswith('/p/')
            or (path.count('/') == 1 and path.endswith(('.js', '.mjs', '.css')))
        )
        headers = Headers(scope=scope) if scope['type'] == 'http' else None
        if (scope['type'] != 'http' or scope['method'] != 'GET' or not (public_text or graph_json)
                or headers.get('range')):
            await self.app(scope, receive, send)
            return

        # Only UI assets and the two complete graph responses are eligible;
        # uploads, reports and incremental AI/check streams bypass compression.
        accepted = headers.get('accept-encoding', '')
        gzip_quality = encoding_quality(accepted, 'gzip')

        async def send_variant(message):
            if message['type'] == 'http.response.start':
                outgoing = MutableHeaders(scope=message)
                if graph_json:
                    outgoing['Cache-Control'] = 'private, no-store'
                vary = {part.strip().lower() for part in outgoing.get('vary', '').split(',')}
                if 'accept-encoding' not in vary and '*' not in vary:
                    outgoing.add_vary_header('Accept-Encoding')
                # Encoded and identity bytes cannot share a strong validator.
                etag = outgoing.get('etag', '')
                if outgoing.get('content-encoding') == 'gzip' and etag and not etag.startswith('W/'):
                    outgoing['etag'] = 'W/' + etag
            await send(message)

        if gzip_quality > 0:
            # Starlette checks for a literal gzip token. Preserve the other
            # codings and weights so routes can still choose prebuilt Brotli.
            normalized = (accepted + ', gzip;q=' + str(gzip_quality)).encode('latin-1')
            compressed_scope = {**scope, 'headers': [
                (key, value) for key, value in scope['headers'] if key.lower() != b'accept-encoding'
            ] + [(b'accept-encoding', normalized)]}
            compressor = self.graph_compressed if graph_json else self.compressed
            await compressor(compressed_scope, receive, send_variant)
        else:
            await self.app(scope, receive, send_variant)
