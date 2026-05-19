-- 014 — quotes.embedding
--
-- Quotes are first-class searchable content for the chat agent:
-- "find quotes about cost overruns", "what has HOK said about
-- airport design", "show quotes from architects on adaptive reuse"
-- all want vector NN over the quote text + speaker + project
-- context. Embedding text is constructed in core/embeddings/embed.py
-- with text + speaker name/title/firm + linked project name so the
-- vector encodes the quote, who said it, and what they were talking
-- about.

CREATE EXTENSION IF NOT EXISTS vector;   -- no-op if already present

ALTER TABLE quotes
    ADD COLUMN IF NOT EXISTS embedding vector(1536);

CREATE INDEX IF NOT EXISTS idx_quotes_embedding
    ON quotes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

COMMENT ON COLUMN quotes.embedding IS
    'text-embedding-3-small (1536-dim). Input: text + speaker + linked '
    'project name. Populated by core/embeddings/embed.py:embed_quotes().';
