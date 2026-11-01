"""UI and graph compression preserves negotiation, byte ranges and private streams."""
import asyncio
from pathlib import Path
import unittest

from starlette.applications import Starlette
from starlette.responses import Response, JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from webapp.static_compression import StaticCompressionMiddleware


BODY = b'const example = "public asset";\n' * 200


class StaticCompressionTests(unittest.TestCase):
    def setUp(self):
        async def text(request):
            return Response(BODY, media_type='text/javascript',
                            headers={'ETag': '"original"', 'Vary': 'Origin'})

        async def small(request):
            return Response(b'let ready=true;', media_type='text/javascript')

        async def graph(request):
            if request.query_params.get('denied'):
                return JSONResponse({'detail': 'Access denied'}, status_code=401)
            return JSONResponse({'nodes': [{'id': str(i), 'label': '公开政策原文' * 50}
                                           for i in range(200)], 'edges': []},
                                headers={'Cache-Control': 'private, no-store', 'Vary': 'Cookie'})

        self.client = TestClient(StaticCompressionMiddleware(Starlette(routes=[
            Route('/bundle.js', text, methods=['GET', 'POST']), Route('/small.js', small),
            Route('/api/private.js', text), Route('/report', text),
            Route('/api/knowledge/graph', graph), Route('/api/training/knowledge/graph', graph),
        ])))
        self.addCleanup(self.client.close)

    def test_gzip_decodes_to_the_exact_source_and_preserves_variant_headers(self):
        response = self.client.get('/bundle.js', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(response.content, BODY)
        self.assertEqual(response.headers['Content-Encoding'], 'gzip')
        self.assertLess(int(response.headers['Content-Length']), len(BODY))
        self.assertEqual(response.headers['ETag'], 'W/"original"')
        self.assertEqual({part.strip().lower() for part in response.headers['Vary'].split(',')},
                         {'origin', 'accept-encoding'})

    def test_identity_and_explicit_rejection_remain_uncompressed(self):
        for encoding in ['identity', 'br', 'gzip;q=0', '*;q=1, gzip;q=0', 'gzip;q=invalid']:
            with self.subTest(encoding=encoding):
                response = self.client.get('/bundle.js', headers={'Accept-Encoding': encoding})
                self.assertNotIn('Content-Encoding', response.headers)
                self.assertEqual(response.content, BODY)
                self.assertEqual(response.headers['ETag'], '"original"')
                self.assertIn('Accept-Encoding', response.headers['Vary'])

    def test_wildcards_and_case_insensitive_codings_are_supported(self):
        for encoding in ['GZIP', 'gzip;q=0.5', '*;q=1']:
            with self.subTest(encoding=encoding):
                response = self.client.get('/bundle.js', headers={'Accept-Encoding': encoding})
                self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                self.assertEqual(response.content, BODY)

    def test_private_paths_and_post_requests_are_untouched(self):
        for method, path in [('GET', '/api/private.js'), ('GET', '/report'), ('POST', '/bundle.js')]:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, headers={'Accept-Encoding': 'gzip'})
                self.assertNotIn('Content-Encoding', response.headers)
                self.assertEqual(response.headers['Vary'], 'Origin')
                self.assertEqual(response.content, BODY)

    def test_small_responses_are_not_compressed(self):
        response = self.client.get('/small.js', headers={'Accept-Encoding': 'gzip'})
        self.assertNotIn('Content-Encoding', response.headers)
        self.assertEqual(response.content, b'let ready=true;')

    def test_graph_responses_compress_without_changing_data_or_private_headers(self):
        for path in ['/api/knowledge/graph?audit_id=example', '/api/training/knowledge/graph']:
            with self.subTest(path=path):
                original = self.client.get(path, headers={'Accept-Encoding': 'identity'})
                response = self.client.get(path, headers={'Accept-Encoding': 'gzip'})
                self.assertEqual(response.content, original.content)
                self.assertEqual(response.headers.get('Content-Encoding'), 'gzip')
                self.assertLess(int(response.headers['Content-Length']), len(original.content) // 5)
                self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
                self.assertEqual({part.strip().lower() for part in response.headers['Vary'].split(',')},
                                 {'cookie', 'accept-encoding'})

    def test_graph_compression_honors_negotiation_and_does_not_cache_errors(self):
        for path in ['/api/knowledge/graph', '/api/training/knowledge/graph']:
            for encoding in ['identity', 'gzip;q=0', '*;q=1, gzip;q=0', 'br', 'gzip;q=invalid']:
                with self.subTest(path=path, encoding=encoding):
                    response = self.client.get(path, headers={'Accept-Encoding': encoding})
                    self.assertNotIn('Content-Encoding', response.headers)
                    self.assertEqual(len(response.json()['nodes']), 200)
                    self.assertIn('Accept-Encoding', response.headers['Vary'])
            # Authentication/routing errors pass through and cannot be cached.
            denied = self.client.get(path + '?denied=1', headers={'Accept-Encoding': 'gzip'})
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.headers['Cache-Control'], 'private, no-store')
            self.assertNotIn('Content-Encoding', denied.headers)
            response = self.client.post(path, headers={'Accept-Encoding': 'gzip'})
            self.assertEqual(response.status_code, 405)
            self.assertNotIn('Content-Encoding', response.headers)

    def test_private_ndjson_chunks_are_forwarded_immediately(self):
        sent = []

        async def endpoint(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'application/x-ndjson'),
                                    (b'cache-control', b'private, no-store'), (b'vary', b'Cookie')]})
            await send({'type': 'http.response.body', 'body': b'{"progress":1}\n', 'more_body': True})
            self.assertEqual(sent[-1]['body'], b'{"progress":1}\n')
            await send({'type': 'http.response.body', 'body': b'{"complete":true}\n'})

        async def receive():
            return {'type': 'http.request', 'body': b''}

        async def send(message):
            sent.append(message)

        for path in ['/api/checks', '/api/knowledge/ask', '/api/training/knowledge/ask']:
            with self.subTest(path=path):
                sent.clear()
                asyncio.run(StaticCompressionMiddleware(endpoint)(
                    {'type': 'http', 'method': 'GET', 'path': path,
                     'headers': [(b'accept-encoding', b'gzip')]}, receive, send))
                self.assertEqual(dict(sent[0]['headers'])[b'vary'], b'Cookie')
                self.assertNotIn(b'content-encoding', dict(sent[0]['headers']))
                self.assertEqual(len(sent), 3)

    def test_real_public_assets_and_ranges(self):
        from webapp.app import app
        root = Path(__file__).resolve().parents[1]
        # No application lifespan is needed to serve these public files.
        client = TestClient(app)
        self.addCleanup(client.close)
        for url, file in [('/', 'index.html'), ('/auth.js', 'auth.js'),
                          ('/auth-ocean.mjs', 'auth-ocean.bundle.mjs'),
                          ('/workspace.css', 'workspace.css'),
                          ('/vendor/three.module.mjs', 'vendor/three.module.mjs')]:
            with self.subTest(url=url):
                response = client.get(url, headers={'Accept-Encoding': 'gzip'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                self.assertEqual(response.content, (root / 'webapp/static' / file).read_bytes())
        response = client.get('/auth.js', headers={'Accept-Encoding': 'gzip', 'Range': 'bytes=0-63'})
        self.assertEqual(response.status_code, 206)
        self.assertNotIn('Content-Encoding', response.headers)
        self.assertEqual(response.content, (root / 'webapp/static/auth.js').read_bytes()[:64])
        response = client.get('/auth-wordmark.png', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Content-Encoding', response.headers)

    def test_ocean_brotli_negotiation_sends_the_prebuilt_bytes(self):
        from webapp.app import app
        root = Path(__file__).resolve().parents[1] / 'webapp/static'
        client = TestClient(app)
        self.addCleanup(client.close)
        for accepted in ['br', 'gzip, br', 'BR ; q=1, gzip;q=0.5', '*;q=0.8']:
            with self.subTest(accepted=accepted):
                # Inspect wire bytes without requiring a Python Brotli decoder.
                with client.stream('GET', '/auth-ocean.mjs', headers={'Accept-Encoding': accepted}) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['Content-Encoding'], 'br')
                    self.assertIn('Accept-Encoding', response.headers['Vary'])
                    self.assertTrue(response.headers['Content-Type'].startswith('text/javascript'))
                    encoded = b''.join(response.iter_raw())
                    self.assertEqual(encoded, (root / 'auth-ocean.bundle.mjs.br').read_bytes())
                    self.assertEqual(int(response.headers['Content-Length']), len(encoded))
        for accepted, encoding in [('gzip', 'gzip'), ('br;q=0, gzip', 'gzip'),
                                   ('br;q=0.2, gzip;q=0.8', 'gzip'),
                                   ('br;q=invalid', None), ('br;q=NaN', None),
                                   ('br;q=2', None), ('identity', None)]:
            with self.subTest(accepted=accepted):
                response = client.get('/auth-ocean.mjs', headers={'Accept-Encoding': accepted})
                self.assertEqual(response.headers.get('Content-Encoding'), encoding)
                self.assertEqual(response.content, (root / 'auth-ocean.bundle.mjs').read_bytes())

    def test_ocean_brotli_range_uses_identity_bytes(self):
        from webapp.app import app
        client = TestClient(app)
        self.addCleanup(client.close)
        response = client.get('/auth-ocean.mjs', headers={'Accept-Encoding': 'br, gzip', 'Range': 'bytes=0-63'})
        self.assertEqual(response.status_code, 206)
        self.assertNotIn('Content-Encoding', response.headers)
        source = Path(__file__).resolve().parents[1] / 'webapp/static/auth-ocean.bundle.mjs'
        self.assertEqual(response.content, source.read_bytes()[:64])


if __name__ == '__main__':
    unittest.main()
