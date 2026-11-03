-- Final evaluation has its own envelope; ordinary/R4 authorization limits stay intact.
CREATE TABLE app.benchmark_budgets (
 budget_hash text PRIMARY KEY,
 limit_micros bigint NOT NULL CHECK(limit_micros BETWEEN 1 AND 2000000),
 call_limit integer NOT NULL CHECK(call_limit BETWEEN 1 AND 270),
 used_calls integer NOT NULL DEFAULT 0 CHECK(used_calls BETWEEN 0 AND call_limit),
 reserved_micros bigint NOT NULL DEFAULT 0 CHECK(reserved_micros>=0),
 settled_micros bigint NOT NULL DEFAULT 0 CHECK(settled_micros>=0),
 CHECK(reserved_micros+settled_micros<=limit_micros)
);
INSERT INTO app.benchmark_budgets
 SELECT b.profile_hash,b.limit_micros,b.call_limit,b.used_calls,b.reserved_micros,b.settled_micros
 FROM app.authorization_budgets b JOIN app.benchmark_grants g ON g.budget_hash=b.profile_hash;
ALTER TABLE app.benchmark_grants DROP CONSTRAINT benchmark_grants_budget_hash_fkey;
ALTER TABLE app.benchmark_grants ADD CONSTRAINT benchmark_grants_budget_hash_fkey
 FOREIGN KEY(budget_hash) REFERENCES app.benchmark_budgets;
ALTER TABLE app.usage_calls ADD COLUMN final_budget_hash text REFERENCES app.benchmark_budgets;
UPDATE app.usage_calls c SET final_budget_hash=c.trial_budget_hash,trial_budget_hash=NULL
 WHERE EXISTS(SELECT 1 FROM app.benchmark_grants g WHERE g.budget_hash=c.trial_budget_hash);
ALTER TABLE app.usage_calls ADD CONSTRAINT one_group_per_call
 CHECK(trial_budget_hash IS NULL OR final_budget_hash IS NULL);
CREATE FUNCTION app.protect_benchmark_budget() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Benchmark budgets cannot be deleted'; END IF;
 IF ROW(NEW.budget_hash,NEW.limit_micros,NEW.call_limit) IS DISTINCT FROM
    ROW(OLD.budget_hash,OLD.limit_micros,OLD.call_limit) THEN
   RAISE EXCEPTION 'Benchmark limits are immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER benchmark_budget_immutable BEFORE UPDATE OR DELETE ON app.benchmark_budgets
 FOR EACH ROW EXECUTE FUNCTION app.protect_benchmark_budget();
