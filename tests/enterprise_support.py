"""Fixtures and HTTP helpers for enterprise integration tests.

No automatic correction or fake response: a failed upload/confirmation is
returned unchanged. The key is synthetic, scoped to each test, never a .env key.
"""
import base64
from contextlib import closing
import os
from pathlib import Path
from shutil import copyfile, copytree
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.public_policy_package import bootstrap
from webapp.storage import Store


class PublicKnowledgeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        directory = TemporaryDirectory(prefix='taxpearls-public-knowledge-')
        cls.addClassCleanup(directory.cleanup)
        cls._knowledge_dir = Path(directory.name)
        cls._knowledge_ready = False

    def store_with_public_knowledge(self, path, rules_dir, *, keepalive=False):
        # Build under the fixture's own field key and deployment settings,
        # before creating any users. Closing the seed checkpoints its WAL.
        cls = type(self)
        seed_path = cls._knowledge_dir / 'public.db'
        originals = 'policy-originals'
        if not cls._knowledge_ready:
            with closing(Store(seed_path, keepalive=True)) as seed:
                bootstrap(seed, rules_dir)
            cls._knowledge_ready = True
        # Each test can change its DB and retained originals independently.
        copyfile(seed_path, path)
        copytree(seed_path.parent / originals, path.parent / originals, copy_function=copyfile)
        return Store(path, keepalive=keepalive)


def material_key(target):
    return patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY': base64.b64encode(b't' * 32).decode(),
                                  'TAXPEARLS_AI_ENABLED': '0', 'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '0'})(target)


def audit(client, *, files, data=None):
    items = files.items() if isinstance(files, dict) else files
    uploaded = client.post('/api/enterprise/materials', data=data or {},
                           files=[('files', value) for _, value in items])
    if uploaded.status_code != 200:
        return uploaded
    batch = uploaded.json()
    return confirm(client, batch)


def confirm(client, batch):
    return client.post('/api/enterprise/materials/' + batch['id'] + '/confirm',
                       json={'expected_revision': batch['revision']})
