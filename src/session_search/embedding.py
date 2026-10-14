"""One pinned offline CPU embedder, loaded only by the background indexer."""
from __future__ import annotations

import math
import json
from pathlib import Path

MODEL = 'Qwen/Qwen3-Embedding-0.6B'
REVISION = '97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3'
DIMENSIONS = 1024
NATIVE_DIMENSIONS = 1024
MAX_LENGTH = 512
QUERY_INSTRUCTION = 'Given a query, retrieve relevant passages from Codex conversations and tasks.'
DEFAULT_MODEL_PATH = Path.home() / '.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B/snapshots' / REVISION


class Embedder:
    model_identifier = MODEL
    revision = REVISION
    dimension = DIMENSIONS
    dimensions = DIMENSIONS
    max_length = MAX_LENGTH
    max_tokens = MAX_LENGTH
    identity = f'{MODEL}@{REVISION}:cpu-float32:l2:codex-conversations-query-v1:max512:mrl1024'

    def __init__(self, model_path=None, threads=2, *, dimensions=DIMENSIONS, max_length=MAX_LENGTH):
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 32 <= dimensions <= NATIVE_DIMENSIONS:
            raise ValueError("dimensions must be between 32 and 1024")
        if isinstance(max_length, bool) or not isinstance(max_length, int) or not 64 <= max_length <= 32768:
            raise ValueError("max_length must be between 64 and 32768")
        self.dimension = self.dimensions = dimensions
        self.max_length = self.max_tokens = max_length
        self.identity = f"{MODEL}@{REVISION}:cpu-float32:l2:codex-conversations-query-v1:max{max_length}:mrl{dimensions}"
        if not isinstance(threads, int) or isinstance(threads, bool) or threads < 1:
            raise ValueError('threads must be a positive integer')
        path = Path(model_path) if model_path else DEFAULT_MODEL_PATH
        if path.resolve() != DEFAULT_MODEL_PATH.resolve() or path.resolve().name != REVISION:
            raise ValueError('Only the exact pinned local snapshot is supported')
        if not path.is_dir():
            raise FileNotFoundError(f'Pinned offline embedding model is missing: {path}')
        config = json.loads((path / 'config.json').read_text())
        if config.get('model_type') != 'qwen3' or config.get('hidden_size') != NATIVE_DIMENSIONS:
            raise ValueError('Local snapshot configuration differs from pinned Qwen profile')
        for name in ('model.safetensors', 'tokenizer.json', 'tokenizer_config.json', 'modules.json'):
            if not (path / name).is_file():
                raise FileNotFoundError(f'Pinned snapshot file is missing: {name}')
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(threads)
        self.model = SentenceTransformer(
            str(path), device='cpu', local_files_only=True,
            trust_remote_code=False, model_kwargs={'torch_dtype': 'float32'},
        )
        self.model.max_seq_length = self.max_length
        self.tokenizer = self.model.tokenizer
        if self.model.get_sentence_embedding_dimension() != NATIVE_DIMENSIONS:
            raise ValueError('Embedding model dimension differs from pinned profile')

    def count_tokens(self, text):
        return len(self.tokenizer(text, add_special_tokens=True, truncation=False)['input_ids'])

    def encode(self, texts):
        texts = list(texts)
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError('Embedding texts must be nonempty strings')
        for text in texts:
            count = self.count_tokens(text)
            if count > self.max_length:
                raise ValueError(f'Embedding input has {count} tokens; maximum {self.max_length}; refusing truncation')
        if not texts:
            return []
        vectors = self.model.encode(texts, prompt='', batch_size=1,
                                    show_progress_bar=False, convert_to_numpy=True,
                                    normalize_embeddings=True).tolist()
        reduced = []
        for vector in vectors:
            if len(vector) != NATIVE_DIMENSIONS or not all(math.isfinite(x) for x in vector):
                raise ValueError('Invalid embedding vector')
            vector = vector[:self.dimensions]
            norm = math.sqrt(sum(x*x for x in vector))
            if norm <= 0:
                raise ValueError('Zero embedding vector')
            reduced.append([x/norm for x in vector])
        vectors = reduced
        return vectors

    def encode_query(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Query must be nonempty text')
        return self.encode([f'Instruct: {QUERY_INSTRUCTION}\nQuery: {text}'])[0]
