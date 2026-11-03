-- A fresh explicit human review may renew an unsent, otherwise unchanged proposal.
-- Original Action payload and expiry remain immutable. This is never automatic.
CREATE TABLE app.action_review_renewals (
 action_id uuid PRIMARY KEY REFERENCES app.actions,
 payload_hash text NOT NULL CHECK(length(payload_hash)=64),
 actor text NOT NULL,
 reviewed_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL CHECK(expires_at>reviewed_at)
);
CREATE FUNCTION app.protect_review_renewal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Review renewal is immutable'; END $$;
CREATE TRIGGER review_renewal_immutable BEFORE UPDATE OR DELETE
 ON app.action_review_renewals FOR EACH ROW EXECUTE FUNCTION app.protect_review_renewal();
