"""
Jina embeddings client + semantic-index construction.

An "index" here is a float32 matrix of L2-normalized row vectors, so
cosine similarity against a normalized query reduces to a dot product.
"""

from typing import List

import numpy as np
import requests

from app import config


def embed_texts(texts: List[str]) -> np.ndarray:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.EMBED_API_KEY}",
    }
    data = {"model": config.EMBED_MODEL, "input": texts}
    resp = requests.post(config.EMBED_API_URL, headers=headers, json=data, timeout=120)
    resp.raise_for_status()
    embeddings = [item["embedding"] for item in resp.json()["data"]]
    return np.array(embeddings, dtype=np.float32)


def build_index(chunks: List[str]) -> np.ndarray:
    """Embed all chunks in batches and L2-normalize each row."""
    if not chunks:
        return np.array([], dtype=np.float32)
    batch_size = config.EMBED_BATCH_SIZE
    all_embeddings = []
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        print(f"   Embedding batch {i // batch_size + 1} ({len(batch)} chunks) …")
        all_embeddings.append(embed_texts(batch))
    result = np.vstack(all_embeddings) if all_embeddings else np.array([], dtype=np.float32)
    norms = np.linalg.norm(result, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    return result / norms


def embed_query(question: str) -> np.ndarray:
    """Embed a single query and L2-normalize it into a column vector."""
    q_arr = embed_texts([question])
    norm = np.linalg.norm(q_arr)
    return (q_arr / norm if norm != 0 else q_arr).T
