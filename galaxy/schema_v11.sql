-- galaxy_memory.db schema v11 (Phase 8): token lifetime and rotation, and request sessions and seals. Additive.
-- Existing tokens get a 30-day expiry from their creation time, so no live token becomes immortal on upgrade.
ALTER TABLE api_tokens ADD COLUMN expires_at REAL;
ALTER TABLE api_tokens ADD COLUMN rotated_to TEXT;
UPDATE api_tokens SET expires_at = created_at + 2592000 WHERE expires_at IS NULL;
ALTER TABLE android_requests ADD COLUMN token_id TEXT;
ALTER TABLE android_requests ADD COLUMN signature TEXT;
CREATE INDEX IF NOT EXISTS idx_api_tokens_live ON api_tokens(user_id, revoked_at);
