-- Additional observations attest an unchanged, explicitly reviewed proposal.
-- Original business results, Action payloads and expired evidence remain immutable.
CREATE TABLE app.commerce_review_checks (
 check_id uuid PRIMARY KEY,
 action_id uuid NOT NULL REFERENCES app.actions,
 payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
 result_id uuid NOT NULL REFERENCES app.business_results,
 certificate_hash text NOT NULL REFERENCES app.artifacts,
 status text NOT NULL CHECK(status IN ('passed','failed')),
 actor text NOT NULL,
 case_revision bigint NOT NULL,
 input_revision bigint NOT NULL,
 execution_epoch bigint NOT NULL,
 fresh_until timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX commerce_review_latest ON app.commerce_review_checks(action_id,created_at DESC);
CREATE TRIGGER immutable_commerce_review_checks BEFORE UPDATE OR DELETE
 ON app.commerce_review_checks FOR EACH ROW EXECUTE FUNCTION app.immutable_business_result();
