CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS m8r_mailboxes (
 mailbox_id text PRIMARY KEY, owner_user_id text NOT NULL, workspace_id text NOT NULL,
 provider text NOT NULL CHECK(provider IN ('gmail','synthetic')),
 content_enabled boolean NOT NULL DEFAULT false, retention_days integer NOT NULL DEFAULT 90 CHECK(retention_days BETWEEN 1 AND 3650)
);
CREATE TABLE IF NOT EXISTS m8r_checkpoints (
 mailbox_id text PRIMARY KEY REFERENCES m8r_mailboxes ON DELETE CASCADE,
 cursor text NOT NULL DEFAULT '', page_token text NOT NULL DEFAULT '',
 generation bigint NOT NULL DEFAULT 0, mode text NOT NULL DEFAULT 'initial' CHECK(mode IN ('initial','history')), status text NOT NULL DEFAULT 'never',
 updated_at timestamptz, complete boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS m8r_threads (
 mailbox_id text NOT NULL REFERENCES m8r_mailboxes ON DELETE CASCADE, thread_id text NOT NULL,
 PRIMARY KEY(mailbox_id,thread_id)
);
CREATE TABLE IF NOT EXISTS m8r_messages (
 mailbox_id text NOT NULL, message_id text NOT NULL, thread_id text NOT NULL,
 subject text NOT NULL DEFAULT '', sender_email text NOT NULL, sender_name text NOT NULL DEFAULT '',
 received_at timestamptz NOT NULL, unread boolean NOT NULL DEFAULT false, has_attachments boolean NOT NULL DEFAULT false,
 deleted boolean NOT NULL DEFAULT false, source_hash text NOT NULL, indexed_at timestamptz NOT NULL DEFAULT now(),
 metadata_fts tsvector GENERATED ALWAYS AS (to_tsvector('simple',coalesce(subject,'') || ' ' || coalesce(sender_name,''))) STORED,
 PRIMARY KEY(mailbox_id,message_id), FOREIGN KEY(mailbox_id,thread_id) REFERENCES m8r_threads ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS m8r_contacts (
 mailbox_id text NOT NULL REFERENCES m8r_mailboxes ON DELETE CASCADE, contact_id text NOT NULL, name text NOT NULL,
 PRIMARY KEY(mailbox_id,contact_id)
);
CREATE TABLE IF NOT EXISTS m8r_addresses (
 mailbox_id text NOT NULL, email text NOT NULL CHECK(email=lower(email)), contact_id text NOT NULL, verified boolean NOT NULL DEFAULT false,
 PRIMARY KEY(mailbox_id,email), FOREIGN KEY(mailbox_id,contact_id) REFERENCES m8r_contacts ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS m8r_companies (
 mailbox_id text NOT NULL REFERENCES m8r_mailboxes ON DELETE CASCADE, company_id text NOT NULL, name text NOT NULL,
 PRIMARY KEY(mailbox_id,company_id)
);
CREATE TABLE IF NOT EXISTS m8r_aliases (
 mailbox_id text NOT NULL, company_id text NOT NULL, alias text NOT NULL CHECK(alias=lower(alias)),
 PRIMARY KEY(mailbox_id,company_id,alias), FOREIGN KEY(mailbox_id,company_id) REFERENCES m8r_companies ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS m8r_domains (
 mailbox_id text NOT NULL, company_id text NOT NULL, domain text NOT NULL CHECK(domain=lower(domain)), verified boolean NOT NULL DEFAULT false,
 PRIMARY KEY(mailbox_id,company_id,domain), FOREIGN KEY(mailbox_id,company_id) REFERENCES m8r_companies ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS m8r_company_contacts (
 mailbox_id text NOT NULL, company_id text NOT NULL, contact_id text NOT NULL,
 PRIMARY KEY(mailbox_id,company_id,contact_id),
 FOREIGN KEY(mailbox_id,company_id) REFERENCES m8r_companies ON DELETE CASCADE,
 FOREIGN KEY(mailbox_id,contact_id) REFERENCES m8r_contacts ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS m8r_participants (
 mailbox_id text NOT NULL, message_id text NOT NULL, role text NOT NULL CHECK(role IN ('from','to','cc')),
 email text NOT NULL CHECK(email=lower(email)), PRIMARY KEY(mailbox_id,message_id,role,email),
 FOREIGN KEY(mailbox_id,message_id) REFERENCES m8r_messages ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS m8r_attachments (
 mailbox_id text NOT NULL, message_id text NOT NULL, attachment_id text NOT NULL,
 filename text NOT NULL, mime_type text NOT NULL, size_bytes bigint NOT NULL CHECK(size_bytes>=0),
 PRIMARY KEY(mailbox_id,message_id,attachment_id), FOREIGN KEY(mailbox_id,message_id) REFERENCES m8r_messages ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS m8r_content (
 mailbox_id text NOT NULL, message_id text NOT NULL, ciphertext bytea NOT NULL,
 search_fts tsvector NOT NULL, expires_at timestamptz NOT NULL,
 PRIMARY KEY(mailbox_id,message_id), FOREIGN KEY(mailbox_id,message_id) REFERENCES m8r_messages ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS m8r_received ON m8r_messages(mailbox_id,received_at DESC,message_id);
CREATE INDEX IF NOT EXISTS m8r_sender ON m8r_messages(mailbox_id,sender_email,received_at DESC);
CREATE INDEX IF NOT EXISTS m8r_thread ON m8r_messages(mailbox_id,thread_id,received_at DESC);
CREATE INDEX IF NOT EXISTS m8r_unread ON m8r_messages(mailbox_id,unread,received_at DESC) WHERE NOT deleted;
CREATE INDEX IF NOT EXISTS m8r_participant ON m8r_participants(mailbox_id,email,role,message_id);
CREATE INDEX IF NOT EXISTS m8r_fts ON m8r_messages USING gin(metadata_fts);
CREATE INDEX IF NOT EXISTS m8r_content_fts ON m8r_content USING gin(search_fts);
CREATE INDEX IF NOT EXISTS m8r_subject_trgm ON m8r_messages USING gin(subject gin_trgm_ops);
CREATE INDEX IF NOT EXISTS m8r_contact_trgm ON m8r_contacts USING gin(name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS m8r_alias_trgm ON m8r_aliases USING gin(alias gin_trgm_ops);
CREATE INDEX IF NOT EXISTS m8r_domain ON m8r_domains(mailbox_id,domain) WHERE verified;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['mailboxes','checkpoints','threads','messages','contacts','addresses','companies',
                          'aliases','domains','company_contacts','participants','attachments','content'] LOOP
  EXECUTE format('ALTER TABLE m8r_%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE m8r_%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY mailbox_scope ON m8r_%I USING (mailbox_id = current_setting(''app.m8_mailbox'',true)) WITH CHECK (mailbox_id = current_setting(''app.m8_mailbox'',true))',t);
 END LOOP;
END $$;
