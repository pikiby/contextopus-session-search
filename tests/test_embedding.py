# Non-English strings are intentional Unicode/UTF-8 search and JSONL fixtures.
import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
from session_search import embedding


class FakeTokenizer:
    def __call__(self, text, **kwargs):
        return {'input_ids': list(range(len(text)))}


class FakeModel:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.tokenizer = FakeTokenizer()
    def get_sentence_embedding_dimension(self):
        return 1024
    def encode(self, texts, **kwargs):
        self.encoded = texts
        return SimpleNamespace(tolist=lambda: [[1.0] + [0.0]*1023 for text in texts])


class EmbedderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.model_path = Path(temporary.name) / 'snapshots' / embedding.REVISION
        self.model_path.mkdir(parents=True)
        (self.model_path / 'config.json').write_text(json.dumps({
            'model_type': 'qwen3', 'hidden_size': embedding.NATIVE_DIMENSIONS,
        }))
        # Only the injected FakeModel reads these placeholders; no weights or
        # tokenizer cache are required, and production file checks still run.
        for name in ('model.safetensors', 'tokenizer.json', 'tokenizer_config.json', 'modules.json'):
            (self.model_path / name).write_text('{}')
        self.patches = [patch.object(embedding, 'DEFAULT_MODEL_PATH', self.model_path),
                        patch.dict('sys.modules', {
            'torch': SimpleNamespace(set_num_threads=lambda _: None),
            'sentence_transformers': SimpleNamespace(SentenceTransformer=FakeModel),
        })]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.embedder = embedding.Embedder(self.model_path)

    def test_pinned_profile_and_offline(self):
        self.assertEqual(self.embedder.model.args, (str(self.model_path),))
        self.assertEqual(self.embedder.model.kwargs['device'], 'cpu')
        self.assertTrue(self.embedder.model.kwargs['local_files_only'])
        self.assertFalse(self.embedder.model.kwargs['trust_remote_code'])
        self.assertEqual(self.embedder.model.max_seq_length, 512)
        self.assertEqual(self.embedder.model.kwargs['model_kwargs']['torch_dtype'], 'float32')
        self.assertIn(embedding.REVISION, self.embedder.identity)

    def test_other_snapshot_rejected(self):
        with self.assertRaisesRegex(ValueError, 'exact pinned'):
            embedding.Embedder(self.model_path.parent / 'other' / embedding.REVISION)

    def test_missing_snapshot_rejected(self):
        missing = self.model_path.parent / 'missing' / embedding.REVISION
        with patch.object(embedding, 'DEFAULT_MODEL_PATH', missing):
            with self.assertRaisesRegex(FileNotFoundError, 'Pinned offline embedding model is missing'):
                embedding.Embedder(missing)

    def test_missing_snapshot_files_rejected(self):
        for name in ('model.safetensors', 'tokenizer.json', 'tokenizer_config.json', 'modules.json'):
            with self.subTest(missing_file=name):
                path = self.model_path / name
                path.unlink()
                with self.assertRaisesRegex(FileNotFoundError, 'Pinned snapshot file is missing'):
                    embedding.Embedder(self.model_path)
                path.write_text('{}')

    def test_other_configuration_rejected(self):
        (self.model_path / 'config.json').write_text(json.dumps({
            'model_type': 'other', 'hidden_size': embedding.NATIVE_DIMENSIONS,
        }))
        with self.assertRaisesRegex(ValueError, 'configuration differs'):
            embedding.Embedder(self.model_path)

    def test_empty(self):
        self.assertEqual(self.embedder.encode([]), [])
        with self.assertRaises(ValueError):
            self.embedder.encode([' '])
        with self.assertRaises(ValueError):
            self.embedder.encode_query('')

    def test_oversize_includes_query_prompt(self):
        with self.assertRaisesRegex(ValueError, 'refusing truncation'):
            self.embedder.encode(['a'*513])
        with self.assertRaisesRegex(ValueError, 'refusing truncation'):
            self.embedder.encode_query('a'*500)

    def test_query_prompt(self):
        self.assertEqual(len(self.embedder.encode_query('Найти задачу')), 1024)
        self.assertEqual(self.embedder.model.encoded,
                         [f'Instruct: {embedding.QUERY_INSTRUCTION}\nQuery: Найти задачу'])

    def test_invalid_vector(self):
        for vector in ([0.0]*1024, [float('nan')]*1024, [1.0]):
            with self.subTest(vector_length=len(vector)):
                self.embedder.model.encode = lambda *a, **kw: SimpleNamespace(tolist=lambda: [vector])
                with self.assertRaises(ValueError):
                    self.embedder.encode(['text'])


if __name__ == '__main__':
    unittest.main()

class ReductionTests(unittest.TestCase):
    def test_mrl_normalizes_after_slicing_and_validates_limits(self):
        e = object.__new__(embedding.Embedder)
        e.dimensions = 256
        e.max_length = 512
        e.count_tokens = len
        e.model = FakeModel()
        e.model.encode = lambda *a, **kw: SimpleNamespace(tolist=lambda: [[1.0]*1024])
        vector = e.encode(['text'])[0]
        self.assertEqual(len(vector), 256)
        self.assertAlmostEqual(sum(x*x for x in vector), 1)
        for kwargs in ({'dimensions': 0}, {'dimensions': 1025}, {'max_length': 50000}):
            with self.assertRaises(ValueError):
                embedding.Embedder(**kwargs)
