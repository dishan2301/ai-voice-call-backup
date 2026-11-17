ALTER TABLE m8r_checkpoints ADD COLUMN IF NOT EXISTS reconcile_required boolean NOT NULL DEFAULT false;
ALTER TABLE m8r_checkpoints ADD COLUMN IF NOT EXISTS reconcile_after text NOT NULL DEFAULT '';
ALTER TABLE m8r_messages ADD COLUMN IF NOT EXISTS rfc_message_id text NOT NULL DEFAULT '';
ALTER TABLE m8r_messages ADD COLUMN IF NOT EXISTS in_reply_to text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS m8r_reply_parent ON m8r_messages(mailbox_id,in_reply_to) WHERE in_reply_to<>'';
