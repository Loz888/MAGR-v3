from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI
from fastapi.encoders import jsonable_encoder

from mgar_v3.coordination.engine import Engine
from mgar_v3.rag.domain import ACLUpdate, MemoryConfirmation, Search, Upload


def register(app: FastAPI, authority: Any, authenticated: Any) -> None:
    routes = APIRouter(prefix="/api/v3", dependencies=[Depends(authenticated)])
    Service = Annotated[Engine, Depends(authority)]

    @routes.get("/knowledge/documents")
    def documents(service: Service) -> Any:
        return jsonable_encoder(service.knowledge.documents())

    @routes.post("/knowledge/documents", status_code=202)
    def upload(body: Upload, service: Service) -> Any:
        return jsonable_encoder(service.knowledge.upload(body))

    @routes.patch("/knowledge/documents/{document_id}/acl")
    def acl(document_id: UUID, body: ACLUpdate, service: Service) -> Any:
        return jsonable_encoder(service.knowledge.acl(document_id, body))

    @routes.get("/knowledge/jobs")
    def jobs(service: Service) -> Any:
        return jsonable_encoder(service.knowledge.jobs())

    @routes.get("/knowledge/status")
    def status(service: Service) -> Any:
        return {
            "active_snapshot_id": str(service.knowledge.active() or ""),
            "mode": service.settings.rag_mode,
        }

    @routes.post("/knowledge/search")
    def search(body: Search, service: Service) -> Any:
        return service.knowledge.search(body)

    @routes.get("/knowledge/chunks/{chunk_id}")
    def read(chunk_id: UUID, snapshot_id: UUID, service: Service) -> Any:
        return jsonable_encoder(service.knowledge.read(chunk_id, snapshot_id))

    @routes.get("/runs/{run_id}/resolution")
    def resolution(run_id: UUID, service: Service) -> Any:
        return jsonable_encoder(service.tools.resolution(run_id))

    @routes.get("/context/{kind}/{object_id}")
    def context(kind: str, object_id: UUID, service: Service, max_hops: int = 2) -> Any:
        return service.context.recall(kind, object_id, max_hops=max_hops)

    @routes.get("/context/{kind}/{object_id}/page")
    def page(
        kind: str,
        object_id: UUID,
        service: Service,
        cursor: str | None = None,
        max_bytes: int = 2048,
    ) -> Any:
        return service.context.page(kind, object_id, cursor=cursor, max_bytes=max_bytes)

    @routes.get("/memories")
    def memories(service: Service) -> Any:
        return jsonable_encoder(service.context.memories())

    @routes.get("/tickets/{ticket_id}/memory-preview")
    def preview(ticket_id: UUID, service: Service) -> Any:
        return service.context.preview_memory(ticket_id)

    @routes.post("/tickets/{ticket_id}/memory")
    def confirm(ticket_id: UUID, body: MemoryConfirmation, service: Service) -> Any:
        return jsonable_encoder(
            service.context.confirm(ticket_id, body.summary_hash, body.report_hash)
        )

    @routes.delete("/memories/{memory_id}")
    def revoke(memory_id: UUID, service: Service) -> dict[str, bool]:
        service.context.revoke(memory_id)
        return {"revoked": True}

    app.include_router(routes)
