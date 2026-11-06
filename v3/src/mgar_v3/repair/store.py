"""Engine-owned repair records. Network, compiler and CAS writes stay outside transactions."""

import hashlib
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from mgar_v3.domain import DomainError, TriggerInput, digest
from mgar_v3.repair.domain import RepairInput, Snapshot, publication_requests
from mgar_v3.repair.hidden import suite_for_run
from mgar_v3.repair.sandbox import local_lock
from mgar_v3.repair.workspace import fixture_snapshot, recount_diff, workspace_hash

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Connection, Engine, Row
    from mgar_v3.live import LivePermit


class Repairs:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def register_live(self, conn: "Connection", permit: "LivePermit", snapshot_hash: str) -> None:
        scope = permit.profile.repair
        assert scope is not None
        roots = sorted(scope.predecessor_budget_hashes)
        found = conn.execute(
            "SELECT profile_hash FROM app.authorization_budgets "
            "WHERE profile_hash=ANY(%s) FOR UPDATE",
            (roots,),
        ).fetchall()
        if len(found) != len(roots):
            raise DomainError(
                "R5_BUDGET_PREDECESSOR_MISSING", "Cumulative budget predecessors are unavailable"
            )
        budget_hash = digest(
            {
                "project": self.engine.settings.project_id,
                "repository": self.engine.settings.repository.lower(),
                "purpose": "r5-cumulative-v1",
            }
        )
        conn.execute(
            "INSERT INTO app.repair_budget_groups(budget_hash,predecessor_hashes,"
            "cumulative_limit_micros) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (budget_hash, Jsonb(roots), scope.cumulative_limit_micros),
        )
        group = conn.execute(
            "SELECT * FROM app.repair_budget_groups WHERE budget_hash=%s FOR UPDATE", (budget_hash,)
        ).fetchone()
        if (
            not group
            or group["predecessor_hashes"] != roots
            or group["cumulative_limit_micros"] != scope.cumulative_limit_micros
        ):
            raise DomainError(
                "R5_BUDGET_SCOPE_CHANGED", "R5 cannot reset its cumulative predecessors or limit"
            )
        conn.execute(
            "INSERT INTO app.repair_live_profiles(profile_hash,budget_hash,snapshot_hash,"
            "authorization_payload) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                permit.profile.profile_hash,
                budget_hash,
                snapshot_hash,
                Jsonb(permit.authorization.model_dump(mode="json")),
            ),
        )

    def reserve_budget(self, conn: "Connection", profile_hash: str, amount: int) -> None:
        # R4 child profiles consume their trial root. Check that root against every R5
        # cumulative group too, so an older grant cannot overspend after R5 starts.
        alias = conn.execute(
            "SELECT t.budget_hash FROM app.r4_trial_profiles p "
            "JOIN app.r4_trials t USING(trial_id) WHERE p.profile_hash=%s",
            (profile_hash,),
        ).fetchone()
        effective = alias["budget_hash"] if alias else profile_hash
        groups = conn.execute(
            "SELECT g.* FROM app.repair_budget_groups g "
            "WHERE g.predecessor_hashes @> %s OR EXISTS "
            "(SELECT 1 FROM app.repair_live_profiles p WHERE p.budget_hash=g.budget_hash "
            "AND p.profile_hash=%s) FOR UPDATE OF g",
            (Jsonb([effective]), profile_hash),
        ).fetchall()
        for group in groups:
            unknown = conn.execute(
                "SELECT 1 FROM app.usage_calls WHERE status='unknown' AND "
                "(profile_hash=ANY(%s) OR trial_budget_hash=ANY(%s) OR profile_hash IN "
                "(SELECT profile_hash FROM app.repair_live_profiles WHERE budget_hash=%s)) LIMIT 1",
                (group["predecessor_hashes"], group["predecessor_hashes"], group["budget_hash"]),
            ).fetchone()
            if unknown:
                raise DomainError(
                    "USAGE_UNKNOWN", "Cumulative R5 usage requires reconciliation before a new call"
                )
            total = conn.execute(
                "SELECT coalesce(sum(settled_micros+reserved_micros),0) AS used "
                "FROM app.authorization_budgets WHERE profile_hash=ANY(%s) OR profile_hash IN "
                "(SELECT profile_hash FROM app.repair_live_profiles WHERE budget_hash=%s)",
                (group["predecessor_hashes"], group["budget_hash"]),
            ).fetchone()
            assert total is not None
            if total["used"] + amount > group["cumulative_limit_micros"]:
                raise DomainError(
                    "BUDGET_EXHAUSTED",
                    "R5 cumulative budget including earlier stages is exhausted",
                    403,
                )

    def artifact(self, content: bytes) -> str:
        value = self.engine.artifacts.put(content)
        with self.engine.transaction() as conn:
            conn.execute(
                "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                (value, len(content)),
            )
        return value

    def prepare_create(
        self,
        request: RepairInput,
        permit: "LivePermit | None" = None,
        fixture: Snapshot | None = None,
    ) -> dict[str, Any]:
        from mgar_v3.repair.live import load_snapshot
        from mgar_v3.repair.source import scope_hash as snapshot_scope

        if request.execution_mode == "live":
            if permit is None or permit.profile.investigation_version != "r5-repair-v1":
                raise DomainError(
                    "R5_PROFILE_REQUIRED", "R5 needs its separate reviewed live profile", 403
                )
            snapshot = load_snapshot(permit)
        else:
            from mgar_v3.repair.source import verify_snapshot

            snapshot = fixture or fixture_snapshot(self.engine.settings.repository.lower())
            verify_snapshot(snapshot)
        source = snapshot.model_dump_json().encode()
        snapshot_hash = self.engine.artifacts.put(source)
        scope_hash = snapshot_scope(snapshot)
        event = TriggerInput(
            source="cli",
            external_event_id=request.client_request_id,
            project_id=request.project_id,
            repository=self.engine.settings.repository,
            issue_number=request.issue_number,
            actor=self.engine.settings.local_actor,
            body="/mgar investigate " + request.question,
            execution_mode=request.execution_mode,
        )
        return {
            "snapshot": snapshot,
            "source": source,
            "snapshot_hash": snapshot_hash,
            "scope_hash": scope_hash,
            "event": event,
        }

    def create(self, request: RepairInput, permit: "LivePermit | None" = None) -> "Row":
        prepared = self.prepare_create(request, permit)
        return self.commit_create(request, prepared, permit)

    def commit_create(
        self, request: RepairInput, prepared: dict[str, Any], permit: "LivePermit | None" = None
    ) -> "Row":
        source = prepared["source"]
        snapshot_hash, scope_hash, event = (
            prepared["snapshot_hash"],
            prepared["scope_hash"],
            prepared["event"],
        )
        with self.engine.transaction() as conn:
            receipt = self.engine.ingest(
                event, permit if request.execution_mode == "live" else None
            )
            assert receipt.run_id is not None
            if receipt.duplicate:
                old = conn.execute(
                    "SELECT * FROM app.repair_requests WHERE run_id=%s", (receipt.run_id,)
                ).fetchone()
                if not old or old["scope_hash"] != scope_hash:
                    raise DomainError(
                        "EVENT_ID_REUSED", "Repair scope differs from the old request"
                    )
                if permit is not None and request.execution_mode == "live":
                    original = self.engine._run(conn, receipt.run_id)
                    mandate = conn.execute(
                        "SELECT scope FROM app.mandates WHERE mandate_id=%s",
                        (original["mandate_id"],),
                    ).fetchone()
                    if (
                        not mandate
                        or mandate["scope"].get("profile_hash") != permit.profile.profile_hash
                    ):
                        raise DomainError(
                            "EVENT_ID_REUSED", "Repair request belongs to another model profile"
                        )
            else:
                run = self.engine._run(conn, receipt.run_id)
                if receipt.route != "created":
                    raise DomainError("RUN_BUSY", "Use a free Ticket for repair")
                conn.execute(
                    "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (snapshot_hash, len(source)),
                )
                conn.execute(
                    "INSERT INTO app.repair_requests(run_id,snapshot_hash,scope_hash) "
                    "VALUES (%s,%s,%s)",
                    (receipt.run_id, snapshot_hash, scope_hash),
                )
                if request.execution_mode == "live":
                    assert permit is not None and permit.profile.repair is not None
                    self.register_live(conn, permit, snapshot_hash)
                    conn.execute(
                        "UPDATE app.repair_requests SET deadline=now()+%s WHERE run_id=%s",
                        (timedelta(seconds=permit.profile.repair.deadline_seconds), receipt.run_id),
                    )
                conn.execute(
                    "UPDATE app.runs SET graph_version='r5-repair-v1' WHERE run_id=%s",
                    (receipt.run_id,),
                )
                mandate = conn.execute(
                    "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
                ).fetchone()
                assert mandate is not None
                conn.execute(
                    "UPDATE app.mandates SET scope=%s WHERE mandate_id=%s",
                    (
                        Jsonb(
                            {
                                **mandate["scope"],
                                "actions": ["draft_pr"],
                                "repair_scope_hash": scope_hash,
                            }
                        ),
                        run["mandate_id"],
                    ),
                )
            return receipt.model_dump(mode="json")

    def snapshot(self, run_id: UUID) -> Snapshot:
        from mgar_v3.repair.source import scope_hash, verify_snapshot

        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id)
            self.engine.routing.owned_ticket(run["ticket_id"])
            row = conn.execute(
                "SELECT * FROM app.repair_requests WHERE run_id=%s", (run_id,)
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Repair request was not found", 404)
        source = Snapshot.model_validate_json(self.engine.artifacts.read(row["snapshot_hash"]))
        if scope_hash(source) != row["scope_hash"]:
            raise DomainError("REPAIR_SOURCE_CHANGED", "Frozen repair snapshot changed")
        verify_snapshot(source)
        return source

    def step(self, run_id: UUID, epoch: int, revision: int, key: str) -> dict[str, Any] | None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Repair no longer owns the execution")
            row = conn.execute(
                "SELECT * FROM app.repair_steps WHERE run_id=%s "
                "AND input_revision=%s AND step_key=%s",
                (run_id, revision, key),
            ).fetchone()
            if row and row["status"] == "succeeded":
                return dict(row["result"])
            conn.execute(
                "INSERT INTO app.repair_steps(run_id,input_revision,step_key,"
                "execution_epoch,status) VALUES (%s,%s,%s,%s,'running') "
                "ON CONFLICT(run_id,input_revision,step_key) DO UPDATE SET "
                "execution_epoch=excluded.execution_epoch,status='running'",
                (run_id, revision, key, epoch),
            )
            return None

    def finish(
        self, run_id: UUID, epoch: int, revision: int, key: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Late repair result was rejected")
            updated = conn.execute(
                "UPDATE app.repair_steps SET status='succeeded',result=%s "
                "WHERE run_id=%s AND input_revision=%s AND step_key=%s AND execution_epoch=%s "
                "RETURNING step_key",
                (Jsonb(result), run_id, revision, key, epoch),
            ).fetchone()
            if not updated:
                raise DomainError("FENCED", "Repair step belongs to an old epoch")
            self.engine._event(
                conn, run_id, "repair_step_completed", step_key=key, input_revision=revision
            )
        return result

    def remaining(self, run_id: UUID, epoch: int) -> float:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Repair is no longer running")
            row = conn.execute(
                "SELECT extract(epoch FROM deadline-now()) AS seconds "
                "FROM app.repair_requests WHERE run_id=%s",
                (run_id,),
            ).fetchone()
            assert row is not None
            return max(0.0, float(row["seconds"]))

    def waiting(self, run_id: UUID, epoch: int, reason: str) -> None:
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Repair is no longer running")
            conn.execute(
                "UPDATE app.runs SET status='waiting_input',reason_code=%s WHERE run_id=%s",
                (reason, run_id),
            )
            self.engine._event(conn, run_id, "repair_waiting_input", reason_code=reason)

    def candidate(
        self,
        run_id: UUID,
        epoch: int,
        revision: int,
        attempt: int,
        diff: str,
        files: dict[str, str],
        public: dict[str, Any],
        hidden: dict[str, Any],
    ) -> "Row":
        from mgar_v3.repair.source import candidate_tree, working_files

        snapshot = self.snapshot(run_id)
        work = working_files(snapshot, files)
        if public["workspace_hash"] != workspace_hash(work) or hidden[
            "workspace_hash"
        ] != workspace_hash({**work, "zz_mgar_hidden_test.go": suite_for_run(self.engine, run_id)}):
            raise DomainError(
                "TEST_PROOF_CHANGED", "Receipts belong to a different tested workspace"
            )
        diff_hash = self.artifact(diff.encode())
        files_hash = self.artifact(json.dumps(files, sort_keys=True).encode())
        verified = bool(public["passed"] and hidden["passed"])
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running":
                raise DomainError("FENCED", "Repair candidate arrived late")
            row = conn.execute(
                "INSERT INTO app.repair_candidates(run_id,input_revision,attempt,"
                "diff_hash,files_hash,workspace_hash,tree_sha,public_receipt,"
                "hidden_receipt,verified) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(run_id,input_revision,attempt) DO UPDATE "
                "SET verified=app.repair_candidates.verified RETURNING *",
                (
                    run_id,
                    revision,
                    attempt,
                    diff_hash,
                    files_hash,
                    workspace_hash(files),
                    candidate_tree(snapshot, files),
                    Jsonb(public),
                    Jsonb(hidden),
                    verified,
                ),
            ).fetchone()
            assert row is not None
            if row["diff_hash"] != diff_hash or row["files_hash"] != files_hash:
                raise DomainError("CANDIDATE_CHANGED", "Stored attempt cannot change its patch")
        if self.engine.commerce.binding(run_id):
            from mgar_v3.commerce.results import record_candidate

            record_candidate(self.engine.commerce, run_id, epoch, row)
        return row

    def propose(self, run_id: UUID, epoch: int, revision: int, candidate: "Row") -> UUID:
        from mgar_v3.repair.source import candidate_tree

        snapshot = self.snapshot(run_id)
        diff = self.engine.artifacts.read(candidate["diff_hash"]).decode()
        files = json.loads(self.engine.artifacts.read(candidate["files_hash"]))
        if (
            not candidate["verified"]
            or workspace_hash(files) != candidate["workspace_hash"]
            or candidate_tree(snapshot, files) != candidate["tree_sha"]
        ):
            raise DomainError(
                "UNVERIFIED_PATCH", "Candidate lacks matching protected test evidence"
            )
        commerce_result = (
            self.engine.commerce.require_result(run_id, candidate["diff_hash"])
            if self.engine.commerce.binding(run_id)
            else None
        )
        body_calls = []
        if commerce_result:
            from mgar_v3.repair.body import body_diff

            with self.engine.transaction() as conn:
                recorded_bodies = conn.execute(
                    "SELECT * FROM app.usage_calls WHERE run_id=%s "
                    "AND status='known' AND result ? 'function_body'",
                    (run_id,),
                ).fetchall()
            for call in recorded_bodies:
                try:
                    if body_diff(snapshot, call["result"]["function_body"]) == diff:
                        body_calls.append(call)
                except DomainError:
                    continue
        with self.engine.transaction() as conn:
            run = self.engine._run(conn, run_id, epoch)
            if run["status"] != "running" or run["thread_revision"] != revision:
                raise DomainError("STALE_INPUT", "Repair input changed before proposal")
            scope = conn.execute(
                "SELECT scope FROM app.mandates WHERE mandate_id=%s", (run["mandate_id"],)
            ).fetchone()
            if scope and scope["scope"].get("read_only"):
                raise DomainError(
                    "READ_ONLY_RUN", "Read-only investigations cannot propose repairs", 403
                )
            key = f"{run['ticket_id']}:draft:r{revision}:g{run['proposal_generation']}"
            old = conn.execute(
                "SELECT action_id FROM app.actions WHERE dedupe_key=%s", (key,)
            ).fetchone()
            if old:
                return UUID(str(old["action_id"]))
            self.engine.routing.require_primary(run_id, run["thread_id"])
            target = f"github:{snapshot.repository}:draft:{snapshot.base_branch}"
            grant = conn.execute(
                "SELECT * FROM app.target_write_grants WHERE target=%s FOR UPDATE", (target,)
            ).fetchone()
            if grant and grant["active"] and grant["run_id"] != run_id:
                raise DomainError("TARGET_BUSY", "Another Run owns this repair target")
            token = grant["fencing_token"] + 1 if grant else 1
            conn.execute(
                "INSERT INTO app.target_write_grants(target,run_id,execution_epoch,"
                "fencing_token) VALUES (%s,%s,%s,%s) ON CONFLICT(target) DO UPDATE "
                "SET run_id=excluded.run_id,execution_epoch=excluded.execution_epoch,"
                "fencing_token=excluded.fencing_token,active=true",
                (target, run_id, epoch, token),
            )
            action_id = uuid4()
            payload = {
                "repository": snapshot.repository,
                "issue_number": int(run["external_key"].rsplit(":", 1)[1]),
                "base_sha": snapshot.base_sha,
                "base_tree": snapshot.base_tree,
                "base_branch": snapshot.base_branch,
                "source_origin": snapshot.origin,
                "draft": True,
                "branch": "mgar/r5-" + action_id.hex,
                "title": "Fix paid order revenue calculation",
                "body": f"Validated bounded repair.\n\n{diff}\n\n<!-- mgar-action:{action_id} -->",
                "diff": diff,
                "diff_hash": candidate["diff_hash"],
                "workspace_hash": candidate["workspace_hash"],
                "tree_sha": candidate["tree_sha"],
                "files": [
                    {"path": p, "content": files[p]}
                    for p in snapshot.editable_paths
                    if files[p] != snapshot.files[p]
                ],
                "public_receipt": candidate["public_receipt"],
                "hidden_receipt": candidate["hidden_receipt"],
                "test_hash": digest(
                    {"public": candidate["public_receipt"], "hidden": candidate["hidden_receipt"]}
                ),
            }
            if commerce_result:
                current_result = self.engine.commerce.require_result(run_id, candidate["diff_hash"])
                payload["business_result_hash"] = current_result["result_hash"]
                payload["spec_hash"] = current_result["spec_hash"]
            if run["execution_mode"] == "live":
                recorded = conn.execute(
                    "SELECT call_id,run_id,step_key,profile_hash,result->>'diff' AS raw_diff "
                    "FROM app.usage_calls WHERE profile_hash="
                    "(SELECT scope->>'profile_hash' FROM app.mandates WHERE mandate_id=%s) "
                    "AND status='known' AND result ? 'diff' ORDER BY call_id",
                    (run["mandate_id"],),
                ).fetchall()
                calls = []
                for call in recorded:
                    try:
                        normalized = recount_diff(call["raw_diff"])
                    except DomainError:
                        continue
                    if normalized == diff:
                        calls.append(call)
                manual = conn.execute(
                    "SELECT diff_hash FROM app.repair_manual_inputs "
                    "WHERE run_id=%s AND input_revision=%s",
                    (run_id, revision),
                ).fetchone()
                if (
                    not calls
                    and not body_calls
                    and (not manual or manual["diff_hash"] != candidate["diff_hash"])
                ):
                    raise DomainError(
                        "PATCH_PROVENANCE_REQUIRED",
                        "Live patch needs a recorded model result or user edit",
                    )
                payload["model_receipts"] = [
                    {
                        "call_id": str(c["call_id"]),
                        "source_run_id": str(c["run_id"]),
                        "step_key": c["step_key"],
                        "profile_hash": c["profile_hash"],
                        "transformation": "hunk_counts_v1" if c["raw_diff"] != diff else "none",
                        "raw_diff_hash": hashlib.sha256(c["raw_diff"].encode()).hexdigest(),
                        "normalized_diff_hash": hashlib.sha256(diff.encode()).hexdigest(),
                    }
                    for c in calls
                ]
                payload["model_receipts"].extend(
                    {
                        "call_id": str(c["call_id"]),
                        "source_run_id": str(c["run_id"]),
                        "step_key": c["step_key"],
                        "profile_hash": c["profile_hash"],
                        "transformation": "function_body_v1",
                        "function_body_hash": hashlib.sha256(
                            c["result"]["function_body"].encode()
                        ).hexdigest(),
                        "normalized_diff_hash": candidate["diff_hash"],
                    }
                    for c in body_calls
                )
                payload["manual_input_hash"] = manual["diff_hash"] if manual else None
            payload_hash = digest(
                {
                    "type": "draft_pr",
                    "target": target,
                    "payload": payload,
                    "base_revision": revision,
                }
            )
            conn.execute(
                "INSERT INTO app.actions(action_id,run_id,action_type,target,payload,"
                "payload_hash,base_revision,execution_epoch,fencing_token,dedupe_key,status,"
                "expires_at,reply_kind) "
                "VALUES (%s,%s,'draft_pr',%s,%s,%s,%s,%s,%s,%s,'proposed',now()+%s,'result')",
                (
                    action_id,
                    run_id,
                    target,
                    Jsonb(payload),
                    payload_hash,
                    revision,
                    epoch,
                    token,
                    key,
                    timedelta(seconds=self.engine.settings.approval_ttl_seconds),
                ),
            )
            conn.execute("UPDATE app.runs SET status='waiting_approval' WHERE run_id=%s", (run_id,))
            self.engine._event(
                conn,
                run_id,
                "repair_approval_requested",
                action_id=str(action_id),
                payload_hash=payload_hash,
            )
            return action_id

    def renew_review(self, action_id: UUID, payload_hash: str, actor: str) -> "Row":
        """Record one fresh human review, never extend or mutate the original proposal."""
        if actor.lower() not in {a.lower() for a in self.engine.settings.allowed_actors}:
            raise DomainError("ACTOR_DENIED", "Actor cannot renew this review", 403)
        self.engine.commerce.prepare_action(action_id)
        with self.engine.transaction() as conn:
            action, run = self.engine._action(conn, action_id)
            if action["action_type"] != "draft_pr" or action["payload_hash"] != payload_hash:
                raise DomainError("APPROVAL_HASH_MISMATCH", "Review needs the exact Draft PR hash")
            if (
                action["status"] != "proposed"
                or conn.execute(
                    "SELECT 1 FROM app.outbox WHERE action_id=%s UNION ALL "
                    "SELECT 1 FROM app.repair_publication_steps WHERE action_id=%s UNION ALL "
                    "SELECT 1 FROM app.approvals WHERE action_id=%s",
                    (action_id, action_id, action_id),
                ).fetchone()
            ):
                raise DomainError(
                    "REVIEW_RENEWAL_DENIED", "Only an entirely unsent proposal can renew"
                )
            # Revalidate every other fence and protected receipt for this fresh review.
            self.engine._validate_action(conn, {**action, "unexpired": True}, run)
            old = conn.execute(
                "SELECT *,expires_at>now() AS unexpired FROM app.action_review_renewals "
                "WHERE action_id=%s",
                (action_id,),
            ).fetchone()
            if old:
                if not old["unexpired"]:
                    raise DomainError(
                        "STALE_ACTION", "Renewed review expired; re-propose explicitly"
                    )
                return old
            if action["unexpired"]:
                return action
            renewed = conn.execute(
                "INSERT INTO app.action_review_renewals(action_id,payload_hash,actor,expires_at) "
                "VALUES (%s,%s,%s,now()+%s) RETURNING *,expires_at>now() AS unexpired",
                (
                    action_id,
                    payload_hash,
                    actor,
                    timedelta(seconds=self.engine.settings.approval_ttl_seconds),
                ),
            ).fetchone()
            self.engine._event(
                conn,
                run["run_id"],
                "repair_review_renewed",
                action_id=str(action_id),
                payload_hash=payload_hash,
                actor=actor,
            )
            assert renewed is not None
            return renewed

    def validate_action(self, action: "Row", run: "Row") -> None:
        from mgar_v3.repair.source import candidate_tree, working_files

        payload = action["payload"]
        snapshot = self.snapshot(run["run_id"])
        if (
            payload.get("draft") is not True
            or payload.get("source_origin") != snapshot.origin
            or payload.get("base_sha") != snapshot.base_sha
            or payload.get("base_tree") != snapshot.base_tree
            or payload.get("base_branch") != snapshot.base_branch
            or payload.get("repository") != snapshot.repository
        ):
            raise DomainError("REPAIR_SCOPE_DENIED", "Draft differs from the frozen source scope")
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM app.repair_candidates WHERE run_id=%s "
                "AND input_revision=%s AND diff_hash=%s AND verified",
                (run["run_id"], action["base_revision"], payload["diff_hash"]),
            ).fetchone()
        if not row or payload["test_hash"] != digest(
            {"public": row["public_receipt"], "hidden": row["hidden_receipt"]}
        ):
            raise DomainError("TEST_PROOF_CHANGED", "Draft lacks matching protected test evidence")
        files = json.loads(self.engine.artifacts.read(row["files_hash"]))
        work = working_files(snapshot, files)
        if (
            payload["public_receipt"] != row["public_receipt"]
            or payload["hidden_receipt"] != row["hidden_receipt"]
            or row["public_receipt"]["workspace_hash"] != workspace_hash(work)
            or row["hidden_receipt"]["workspace_hash"]
            != workspace_hash(
                {**work, "zz_mgar_hidden_test.go": suite_for_run(self.engine, run["run_id"])}
            )
        ):
            raise DomainError(
                "TEST_PROOF_CHANGED", "Receipt content or independent test suite changed"
            )
        if (
            candidate_tree(snapshot, files) != payload["tree_sha"]
            or workspace_hash(files) != payload["workspace_hash"]
        ):
            raise DomainError("TEST_PROOF_CHANGED", "Tested tree changed")
        expected = [
            {"path": p, "content": files[p]}
            for p in snapshot.editable_paths
            if files[p] != snapshot.files[p]
        ]
        if (
            payload.get("files") != expected
            or payload["diff"] != self.engine.artifacts.read(row["diff_hash"]).decode()
        ):
            raise DomainError("PATCH_CHANGED", "Published file bytes differ from the tested tree")
        lock = local_lock()
        for proof in (row["public_receipt"], row["hidden_receipt"]):
            if not proof["passed"] or proof["image_id"] != lock["image_id"]:
                raise DomainError("TEST_PROOF_CHANGED", "Sandbox identity changed after testing")
            self.engine.artifacts.read(proof["log_hash"])

    def decision(
        self, action_id: UUID, payload_hash: str, actor: str, decision: str, reason: str
    ) -> "Row":
        if actor.lower() != self.engine.settings.local_actor.lower():
            raise DomainError("ACTOR_DENIED", "Actor cannot decide this repair", 403)
        with self.engine.transaction() as conn:
            action, run = self.engine._action(conn, action_id)
            if action["action_type"] != "draft_pr":
                raise DomainError(
                    "ACTION_TYPE_DENIED", "Repair decisions require a Draft PR action"
                )
            if action["payload_hash"] != payload_hash or action["status"] != "proposed":
                raise DomainError("STALE_ACTION", "Proposal changed or is no longer pending")
            self.engine._validate_action(conn, action, run)
            conn.execute(
                "INSERT INTO app.action_decisions(decision_id,action_id,actor,decision,"
                "payload_hash,reason) VALUES (%s,%s,%s,%s,%s,%s)",
                (uuid4(), action_id, actor, decision, payload_hash, reason),
            )
            conn.execute(
                "UPDATE app.actions SET status='cancelled',reason_code=%s WHERE action_id=%s",
                (decision.upper(), action_id),
            )
            conn.execute(
                "UPDATE app.runs SET status='waiting_input',reason_code=%s WHERE run_id=%s",
                (decision.upper(), run["run_id"]),
            )
            self.engine._event(
                conn, run["run_id"], "proposal_" + decision, action_id=str(action_id)
            )
            return {**action, "status": "cancelled"}

    def revise(self, action_id: UUID, payload_hash: str, diff: str, actor: str) -> None:
        with self.engine.transaction() as conn:
            _, existing = self.engine._action(conn, action_id)
        permit = None
        if existing["execution_mode"] == "live":
            from mgar_v3.repair.live import repair_permit

            permit = repair_permit(self.engine.settings)
        content_hash = self.artifact(diff.encode())
        with self.engine.transaction() as conn:
            _, run = self.engine._action(conn, action_id)
            self.decision(action_id, payload_hash, actor, "edit", "Retest the user-edited patch")
            self.engine.follow_up(
                run["run_id"],
                "edit:" + str(uuid4()),
                "Retest the edited patch before a new approval.",
                permit,
            )
            current = self.engine._run(conn, run["run_id"])
            conn.execute(
                "UPDATE app.runs SET status='queued',execution_epoch=execution_epoch+1,"
                "reason_code='REPAIR_EDIT_RECEIVED' WHERE run_id=%s",
                (run["run_id"],),
            )
            conn.execute(
                "INSERT INTO app.repair_manual_inputs(run_id,input_revision,diff_hash) "
                "VALUES (%s,%s,%s)",
                (run["run_id"], current["thread_revision"], content_hash),
            )

    def publication_step(
        self, action_id: UUID, lease: UUID, ordinal: int, kind: str, request: dict[str, Any]
    ) -> "Row | None":
        self.engine.commerce.prepare_action(action_id)
        with self.engine.transaction() as conn:
            action, run = self.engine._action(conn, action_id)
            if (
                action["status"] != "dispatching"
                or action["lease_token"] != lease
                or run["execution_epoch"] != action["execution_epoch"]
                or run["status"] != "waiting_approval"
            ):
                raise DomainError("FENCED", "Publication step no longer owns this execution")
            expected = publication_requests(action)
            if ordinal not in range(3) or (kind, request) != (
                expected[ordinal][0],
                expected[ordinal][2],
            ):
                raise DomainError(
                    "PUBLICATION_SCOPE_DENIED", "Step differs from the approved action"
                )
            self.engine._validate_action(conn, action, run)
            old = conn.execute(
                "SELECT * FROM app.repair_publication_steps WHERE action_id=%s AND ordinal=%s",
                (action_id, ordinal),
            ).fetchone()
            if old:
                if old["request_hash"] != digest(request):
                    raise DomainError("STEP_CHANGED", "Publication step request changed")
                if old["status"] != "succeeded":
                    raise DomainError("OUTCOME_UNKNOWN", "Unconfirmed step cannot be resent")
                return old
            conn.execute(
                "INSERT INTO app.repair_publication_steps(action_id,ordinal,kind,"
                "request_hash,status) VALUES (%s,%s,%s,%s,'dispatching')",
                (action_id, ordinal, kind, digest(request)),
            )
            return None

    def completed_step(self, action_id: UUID, ordinal: int, request: dict[str, Any]) -> bool:
        with self.engine.transaction() as conn:
            row = conn.execute(
                "SELECT status,request_hash FROM app.repair_publication_steps "
                "WHERE action_id=%s AND ordinal=%s",
                (action_id, ordinal),
            ).fetchone()
            if row and row["request_hash"] != digest(request):
                raise DomainError("STEP_CHANGED", "A completed publication request cannot change")
            return bool(row and row["status"] == "succeeded")

    def continue_publication(self, action_id: UUID, payload_hash: str, actor: str) -> "Row":
        if actor.lower() != self.engine.settings.local_actor.lower():
            raise DomainError("ACTOR_DENIED", "Actor cannot continue publication", 403)
        self.engine.commerce.prepare_action(action_id)
        with self.engine.transaction() as conn:
            action, run = self.engine._action(conn, action_id)
            if action["action_type"] != "draft_pr" or action["payload_hash"] != payload_hash:
                raise DomainError(
                    "STALE_ACTION", "Continue requires the unchanged Draft PR payload"
                )
            self.engine._validate_action(conn, action, run)
            if action["status"] == "ready":
                return action
            if action["status"] != "outcome_unknown":
                raise DomainError("NOT_UNKNOWN", "Only reconciled partial actions can continue")
            steps = conn.execute(
                "SELECT * FROM app.repair_publication_steps WHERE action_id=%s ORDER BY ordinal",
                (action_id,),
            ).fetchall()
            if (
                any(s["status"] != "succeeded" for s in steps)
                or len(steps) >= 3
                or [s["ordinal"] for s in steps] != list(range(len(steps)))
            ):
                raise DomainError(
                    "OUTCOME_UNKNOWN", "Every previously issued step must be positively reconciled"
                )
            conn.execute(
                "UPDATE app.actions SET status='ready',reason_code=NULL WHERE action_id=%s",
                (action_id,),
            )
            conn.execute(
                "UPDATE app.outbox SET status='ready',next_retry_at=now() WHERE action_id=%s",
                (action_id,),
            )
            self.engine._event(
                conn,
                run["run_id"],
                "draft_unissued_steps_authorized",
                actor=actor,
                payload_hash=payload_hash,
                next_ordinal=len(steps),
            )
            return {**action, "status": "ready"}

    def publication_receipt(
        self, action_id: UUID, lease: UUID, ordinal: int, receipt: dict[str, Any]
    ) -> None:
        with self.engine.transaction() as conn:
            action, _ = self.engine._action(conn, action_id)
            if action["status"] != "dispatching" or action["lease_token"] != lease:
                raise DomainError("FENCED", "Publication receipt lease changed")
            conn.execute(
                "UPDATE app.repair_publication_steps SET status='succeeded',receipt=%s "
                "WHERE action_id=%s AND ordinal=%s AND status='dispatching'",
                (Jsonb(receipt), action_id, ordinal),
            )
            self.engine._event(conn, action["run_id"], "draft_step_committed", ordinal=ordinal)

    def detail(self, run_id: UUID) -> dict[str, Any]:
        self.snapshot(run_id)
        with self.engine.transaction() as conn:
            return {
                "publication_steps": conn.execute(
                    (
                        "SELECT s.* FROM app.repair_publication_steps s JOIN app.actions a "
                        "USING(action_id) WHERE a.run_id=%s ORDER BY action_id,"
                        "ordinal"
                    ),
                    (run_id,),
                ).fetchall(),
                "candidates": conn.execute(
                    "SELECT * FROM app.repair_candidates "
                    "WHERE run_id=%s ORDER BY input_revision,attempt",
                    (run_id,),
                ).fetchall(),
                "steps": conn.execute(
                    "SELECT * FROM app.repair_steps WHERE run_id=%s "
                    "ORDER BY input_revision,step_key",
                    (run_id,),
                ).fetchall(),
            }

    def reconciled(
        self, action_id: UUID, receipts: list[dict[str, Any]] | dict[int, dict[str, Any]]
    ) -> None:
        """Record exact read-only findings; this method never schedules a remote write."""
        with self.engine.transaction() as conn:
            action, _ = self.engine._action(conn, action_id)
            for ordinal, receipt in (
                receipts.items() if isinstance(receipts, dict) else enumerate(receipts)
            ):
                row = conn.execute(
                    "UPDATE app.repair_publication_steps SET status='succeeded',receipt=%s "
                    "WHERE action_id=%s AND ordinal=%s "
                    "AND status IN ('dispatching','outcome_unknown') RETURNING ordinal",
                    (Jsonb(receipt), action_id, ordinal),
                ).fetchone()
                if row:
                    self.engine._event(
                        conn, action["run_id"], "draft_step_reconciled", ordinal=ordinal
                    )

    def artifact_bytes(self, run_id: UUID, content_hash: str) -> bytes:
        detail = self.detail(run_id)
        allowed = set()
        for row in detail["candidates"]:
            allowed.update(
                [
                    row["diff_hash"],
                    row["files_hash"],
                    row["public_receipt"]["log_hash"],
                    row["hidden_receipt"]["log_hash"],
                ]
            )
        for row in detail["steps"]:
            if row["result"] and row["result"].get("log_hash"):
                allowed.add(row["result"]["log_hash"])
        if content_hash not in allowed:
            raise DomainError("NOT_FOUND", "Artifact is outside this repair", 404)
        return self.engine.artifacts.read(content_hash)
