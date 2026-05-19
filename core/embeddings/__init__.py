"""
Embeddings sweep.

Populates `embedding` columns on articles / projects / claims so the
chat agent's semantic_search tool (Track A) can do nearest-neighbor
lookup. Idempotent — only embeds rows where the column is NULL.
"""

from core.embeddings.embed import (
    embed_articles,
    embed_claims,
    embed_projects,
)

__all__ = ["embed_articles", "embed_claims", "embed_projects"]
