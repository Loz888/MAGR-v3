from datetime import UTC, datetime, timedelta

import httpx
from pydantic import SecretStr

from mgar_v3.live import LiveProfile
from mgar_v3.routing.live import RouterAuthorization, RouterPermit, RouterProfile
from mgar_v3.routing.worker import FixtureRouter


def setup_router(engine):
    engine.settings = engine.settings.model_copy(
        update={"allow_paid": True, "deepseek_api_key": SecretStr("fixture-router-key")}
    )
    s = engine.settings
    root = LiveProfile(
        case_id="router-fixture-budget",
        project_id=s.project_id,
        repository=s.repository,
        issue_number=1,
        actor=s.local_actor,
        question="Quote documented symptoms",
        source_ref="a" * 40,
        model_id="fixture-chat",
        price_version="fixture-v1",
        input_micros_per_million=1000000,
        output_micros_per_million=1000000,
        context_tokens=32768,
        max_cost_micros=2000000,
        max_model_calls=3,
    )
    profile = RouterProfile(
        project_id=s.project_id,
        repository=s.repository,
        actor=s.local_actor,
        budget_profile_hash=root.profile_hash,
        model_id="fixture-chat",
        price_version="fixture-v1",
        input_micros_per_million=1000000,
        output_micros_per_million=1000000,
        context_tokens=32768,
    )
    auth = RouterAuthorization(
        profile_hash=profile.profile_hash,
        approved_by=s.local_actor,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        allow_paid=True,
    )
    return root, RouterPermit(profile, auth, root, engine.settings)


def success_response(request):
    import json

    snapshot = json.loads(json.loads(request.content)["messages"][1]["content"])
    proposals = FixtureRouter().propose({"snapshot": snapshot})
    return httpx.Response(
        200,
        json={
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [
                {"finish_reason": "stop", "message": {"content": proposals.model_dump_json()}}
            ],
        },
    )
