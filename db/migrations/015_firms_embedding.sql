-- 015 — firms.embedding
--
-- Lets the chat agent answer "find architects similar to HOK",
-- "what does Cache Valley Electric specialize in", "show firms that
-- play both engineer and architect roles". Input includes name +
-- aliases + firm_type + distinct roles the firm has played across
-- projects, so the vector encodes the firm's industry profile.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE firms
    ADD COLUMN IF NOT EXISTS embedding vector(1536);

CREATE INDEX IF NOT EXISTS idx_firms_embedding
    ON firms USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

COMMENT ON COLUMN firms.embedding IS
    'text-embedding-3-small (1536-dim). Input: name + aliases + '
    'firm_type + concatenated role labels from roles. Populated by '
    'core/embeddings/embed.py:embed_firms().';
