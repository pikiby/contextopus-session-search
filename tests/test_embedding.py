import unittest
from unittest.mock import patch
from types import SimpleNamespace
from session_search import embedding


class FakeTokenizer:
    def __call__(self, text, **kwargs):
        return {'input_ids': list(range(len(text)))}


class FakeModel:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.tokenizer = FakeTokenizer()
    def get_sentence_embedding_dimension(self):
        return 1024
    def encode(self, texts, **kwargs):
        self.encoded = texts
        return SimpleNamespace(tolist=lambda: [[1.0] + [0.0]*1023 for text in texts])


class EmbedderTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.dict('sys.modules', {
            'torch': SimpleNamespace(set_num_threads=lambda _: None),
            'sentence_transformers': SimpleNamespace(SentenceTransformer=FakeModel),
        })]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.embedder = embedding.Embedder()

    def test_pinned_profile_and_offline(self):
        self.assertTrue(self.embedder.model.kwargs['local_files_only'])
        self.assertFalse(self.embedder.model.kwargs['trust_remote_code'])
        self.assertEqual(self.embedder.model.max_seq_length, 512)
        self.assertEqual(self.embedder.model.kwargs['model_kwargs']['torch_dtype'], 'float32')
        self.assertIn(embedding.REVISION, self.embedder.identity)

    def test_other_snapshot_rejected(self):
        with self.assertRaisesRegex(ValueError, 'exact pinned'):
            embedding.Embedder('/tmp/arbitrary-model')

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
        for vector in ([0.0]*1024, [float('nan')]*1024, [1.0]*1024, [1.0]):
            with self.subTest(vector_length=len(vector)):
                self.embedder.model.encode = lambda *a, **kw: SimpleNamespace(tolist=lambda: [vector])
                with self.assertRaises(ValueError):
                    self.embedder.encode(['text'])


if __name__ == '__main__':
    unittest.main()
