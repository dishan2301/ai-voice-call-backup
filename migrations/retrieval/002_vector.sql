CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS m8r_embeddings (
 mailbox_id text NOT NULL, message_id text NOT NULL, model text NOT NULL,
 source_hash text NOT NULL, embedding vector(384) NOT NULL, expires_at timestamptz NOT NULL,
 PRIMARY KEY(mailbox_id,message_id,model), FOREIGN KEY(mailbox_id,message_id) REFERENCES m8r_messages ON DELETE CASCADE
);
ALTER TABLE m8r_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE m8r_embeddings FORCE ROW LEVEL SECURITY;
CREATE POLICY mailbox_scope ON m8r_embeddings
 USING (mailbox_id=current_setting('app.m8_mailbox',true)) WITH CHECK (mailbox_id=current_setting('app.m8_mailbox',true));
-- Exact vector ranking over hard-filtered candidates preserves recall. Add ANN only
-- after measuring a corpus that warrants it; selective ANN filters can lose results.
CREATE INDEX IF NOT EXISTS m8r_embedding_model ON m8r_embeddings(mailbox_id,model);
