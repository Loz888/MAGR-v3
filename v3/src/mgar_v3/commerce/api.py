from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from mgar_v3.commerce.domain import CaseInput, CaseMessage, RepairRequest


def register(app: FastAPI, authority: Any, authenticated: Any) -> None:
    deps = [Depends(authenticated)]
    service_dependency = Depends(authority)

    def response(value: Any, status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(value), status_code=status_code, headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/v3/commerce/templates", dependencies=deps)
    def templates(service: Any = service_dependency) -> JSONResponse:
        return response(service.commerce.templates())

    @app.post("/api/v3/commerce/cases", status_code=202, dependencies=deps)
    def create(body: CaseInput, service: Any = service_dependency) -> JSONResponse:
        return response(service.commerce.create(body), 202)

    @app.get("/api/v3/commerce/cases", dependencies=deps)
    def cases(service: Any = service_dependency) -> JSONResponse:
        return response(service.commerce.list_cases())

    @app.get("/api/v3/commerce/cases/{case_id}", dependencies=deps)
    def detail(case_id: UUID, service: Any = service_dependency) -> JSONResponse:
        return response(service.commerce.detail(case_id))

    @app.get("/api/v3/commerce/cases/{case_id}/result", dependencies=deps)
    def result(case_id: UUID, service: Any = service_dependency) -> JSONResponse:
        return response(service.commerce.result_view(case_id))

    @app.get("/api/v3/commerce/cases/{case_id}/artifacts/{content_hash}", dependencies=deps)
    def artifact(case_id: UUID, content_hash: str, service: Any = service_dependency) -> Response:
        return Response(
            service.commerce.artifact_bytes(case_id, content_hash),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/v3/commerce/cases/{case_id}/messages", dependencies=deps)
    def message(
        case_id: UUID, body: CaseMessage, service: Any = service_dependency
    ) -> JSONResponse:
        return response(service.commerce.message(case_id, body), 202)

    @app.post("/api/v3/commerce/cases/{case_id}/repair", dependencies=deps)
    def repair(
        case_id: UUID, body: RepairRequest, service: Any = service_dependency
    ) -> JSONResponse:
        return response(service.commerce.start_repair(case_id, body), 202)
