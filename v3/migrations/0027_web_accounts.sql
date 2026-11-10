-- Local instance administration: one owner, server-side sessions and encrypted settings.
CREATE TABLE app.web_accounts (
    user_id uuid PRIMARY KEY,
    singleton boolean NOT NULL DEFAULT true UNIQUE CHECK (singleton),
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.web_sessions (
    token_hash char(64) PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES app.web_accounts(user_id) ON DELETE CASCADE,
    csrf_token text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX web_sessions_expiry ON app.web_sessions(expires_at);
CREATE TABLE app.web_login_attempts (
    bucket char(64) PRIMARY KEY,
    attempts integer NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE app.web_analysis_settings (
    revision bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    encrypted_payload text NOT NULL,
    updated_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION app.web_settings_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Web configuration history is append-only';
END;
$$;
CREATE TRIGGER web_analysis_settings_append_only
    BEFORE UPDATE OR DELETE ON app.web_analysis_settings
    FOR EACH ROW EXECUTE FUNCTION app.web_settings_immutable();
