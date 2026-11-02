CREATE TABLE app.authorization_budgets (
    profile_hash text PRIMARY KEY,
    limit_micros bigint NOT NULL CHECK (limit_micros > 0),
    reserved_micros bigint NOT NULL DEFAULT 0 CHECK (reserved_micros >= 0),
    settled_micros bigint NOT NULL DEFAULT 0 CHECK (settled_micros >= 0),
    used_calls integer NOT NULL DEFAULT 0 CHECK (used_calls BETWEEN 0 AND 1),
    CHECK (reserved_micros+settled_micros <= limit_micros)
);
ALTER TABLE app.usage_calls ADD COLUMN profile_hash text REFERENCES app.authorization_budgets;
ALTER TABLE app.usage_calls ADD COLUMN usage jsonb;
