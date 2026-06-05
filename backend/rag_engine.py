"""
WartinLabs RAG Engine
─────────────────────
Local vector search using:
  • sentence-transformers/all-MiniLM-L6-v2  (80 MB, runs on CPU)
  • FAISS IndexFlatIP (cosine similarity after L2-normalisation)

Knowledge base: ../knowledge/wartinlabs.md
Index cache:    ./rag_cache.pkl  (rebuilt automatically when .md changes)
"""

from __future__ import annotations

import hashlib
import logging
import pickle
import re
import sys
from pathlib import Path
from typing import List, Tuple

import numpy as np
from loguru import logger

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
CACHE_FILE    = Path(__file__).resolve().parent / "rag_cache.pkl"

# ── module-level singletons ──────────────────────────────────
_model  = None
_index  = None
_chunks: List[str] = []
_sources: List[str] = []


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _load_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model all-MiniLM-L6-v2 …")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Embedding model ready")
    return _model


def _md5_of_knowledge() -> str:
    h = hashlib.md5()
    for f in sorted(KNOWLEDGE_DIR.glob("*.md")):
        h.update(f.read_bytes())
    return h.hexdigest()


def _chunk_text(text: str, max_words: int = 100) -> List[Tuple[str, str]]:
    """Split markdown into overlapping chunks, tagging each with its section."""
    results: List[Tuple[str, str]] = []
    section = "General"
    buf: List[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^#{1,3}\s+(.+)$", line)
        if m:
            if buf:
                results.append((" ".join(buf), section))
                buf = buf[-25:]          # 25-word overlap
            section = m.group(1).strip()
            buf.append(f"[{section}]")
            continue
        buf.extend(line.split())
        if len(buf) >= max_words:
            results.append((" ".join(buf), section))
            buf = buf[-25:]

    if buf:
        results.append((" ".join(buf), section))
    return results


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def build_index(force: bool = False) -> None:
    """Build the FAISS index from all .md files in knowledge/."""
    global _index, _chunks, _sources

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    current_hash = _md5_of_knowledge()

    # Try loading cache
    if not force and CACHE_FILE.exists():
        try:
            import faiss
            with open(CACHE_FILE, "rb") as fh:
                c = pickle.load(fh)
            if c.get("hash") == current_hash:
                _chunks  = c["chunks"]
                _sources = c["sources"]
                _index   = faiss.deserialize_index(c["index"])
                logger.info(f"RAG: loaded {len(_chunks)} chunks from cache")
                return
            logger.info("RAG: knowledge changed – rebuilding index")
        except Exception as exc:
            logger.warning(f"RAG: cache load failed ({exc}) – rebuilding")

    # Build from scratch
    raw_chunks: List[Tuple[str, str]] = []
    for md in sorted(KNOWLEDGE_DIR.glob("*.md")):
        logger.info(f"RAG: indexing {md.name}")
        raw_chunks.extend(_chunk_text(md.read_text(encoding="utf-8")))

    if not raw_chunks:
        logger.warning("RAG: no .md files found in knowledge/")
        return

    _chunks  = [c[0] for c in raw_chunks]
    _sources = [c[1] for c in raw_chunks]

    model      = _load_model()
    logger.info(f"RAG: embedding {len(_chunks)} chunks …")
    embeddings = model.encode(
        _chunks, show_progress_bar=True, batch_size=64,
        normalize_embeddings=True,        # cosine via inner-product
    ).astype("float32")

    import faiss
    _index = faiss.IndexFlatIP(embeddings.shape[1])
    _index.add(embeddings)

    try:
        with open(CACHE_FILE, "wb") as fh:
            pickle.dump({
                "hash":    current_hash,
                "chunks":  _chunks,
                "sources": _sources,
                "index":   faiss.serialize_index(_index),
            }, fh)
        logger.info(f"RAG: index saved to {CACHE_FILE}")
    except Exception as exc:
        logger.warning(f"RAG: could not save cache: {exc}")

    logger.info(f"RAG: ready – {len(_chunks)} chunks, dim={embeddings.shape[1]}")


def retrieve(query: str, top_k: int = 5, min_score: float = 0.20) -> str:
    """
    Return a context string (≤ top_k passages) for the given query.
    Returns empty string if RAG is not ready or nothing is relevant.
    """
    if _index is None or not _chunks:
        return ""

    try:
        model = _load_model()
        q_emb = model.encode(
            [query], show_progress_bar=False,
            normalize_embeddings=True,
        ).astype("float32")

        import faiss
        scores, idxs = _index.search(q_emb, top_k)

        passages = []
        for score, idx in zip(scores[0], idxs[0]):
            if idx < 0 or float(score) < min_score:
                continue
            src   = _sources[idx] if idx < len(_sources) else "General"
            chunk = _chunks[idx]
            passages.append(f"[{src}]: {chunk}")

        context = "\n\n".join(passages)
        logger.debug(f"RAG: {len(passages)} chunks retrieved for '{query[:60]}'")
        return context

    except Exception as exc:
        logger.error(f"RAG retrieve error: {exc}")
        return ""


def ensure_index() -> None:
    """Called once at startup to guarantee the index is ready."""
    if _index is None:
        build_index()
