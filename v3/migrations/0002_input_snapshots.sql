ALTER TABLE app.runs ADD COLUMN input_revision bigint;
UPDATE app.runs r SET input_revision = h.revision
    FROM app.tickets t JOIN app.threads h USING(thread_id)
    WHERE r.ticket_id=t.ticket_id;
ALTER TABLE app.runs ALTER COLUMN input_revision SET NOT NULL;
ALTER TABLE app.runs ADD CHECK (input_revision > 0);
