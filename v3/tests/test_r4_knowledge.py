import base64
import io
from uuid import UUID, uuid4

import psycopg
import pytest
from pypdf import PdfWriter

from mgar_v3.domain import DomainError
from mgar_v3.rag.domain import ACLUpdate, Search, Upload
from mgar_v3.rag.text import parse, terms


def upload(
    engine,
    text="# 订单统计\n报表统计事件行，业务订单按 order_id 去重。",
    uri="kb://test/orders",
    version="v1",
):
    job = engine.knowledge.upload(
        Upload(
            title="测试资料",
            source_uri=uri,
            source_version=version,
            media_type="text/markdown",
            content_base64=base64.b64encode(text.encode()).decode(),
        )
    )
    return engine.knowledge.index(job["job_id"])


def test_hybrid_search_read_cache_and_exact_filter(engine):
    snapshot = upload(engine)
    result = engine.knowledge.search(Search(query="订单统计 order_id", snapshot_id=snapshot))
    assert result["hits"] and result["embedding_mode"] == "fixture"
    assert result["vector_ranking"] and result["bm25_ranking"]
    hit = result["hits"][0]
    assert hit["read"] is False and hit["locator"]["start_line"] == 1
    read = engine.knowledge.read(UUID(hit["chunk_id"]), snapshot)
    assert "去重" in read["text"]
    cached = engine.knowledge.search(Search(query="订单统计 order_id", snapshot_id=snapshot))
    assert cached["cache_state"] == "hit"
    assert (
        engine.knowledge.search(
            Search(query="订单统计", snapshot_id=snapshot, source_versions=["other"])
        )["hits"]
        == []
    )
    with engine.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.evidence").fetchone()["n"] == 0


def test_failed_ingestion_keeps_prior_snapshot_and_raw_version(engine, monkeypatch):
    old = upload(engine)
    job = engine.knowledge.upload(
        Upload(
            title="bad",
            source_uri="kb://bad",
            source_version="v1",
            media_type="text/plain",
            content_base64=base64.b64encode(b"\xff").decode(),
        )
    )
    with pytest.raises(DomainError, match="parsed"):
        engine.knowledge.index(job["job_id"])
    assert engine.knowledge.active() == old
    assert engine.knowledge.jobs()[0]["status"] == "failed"
    assert engine.knowledge.search(Search(query="订单"))["hits"]
    with pytest.raises(DomainError) as exc:
        upload(engine, "changed")
    assert exc.value.code == "SOURCE_VERSION_REUSED"


def test_snapshot_immutable_and_versions_are_fixed(engine):
    old = upload(engine, version="old")
    new = upload(engine, "# 统计新版\n当前业务口径按唯一订单统计。", version="new")
    assert old != new
    assert (
        engine.knowledge.search(Search(query="统计", snapshot_id=old))["hits"][0]["source_version"]
        == "old"
    )
    assert (
        engine.knowledge.search(Search(query="统计", snapshot_id=new))["hits"][0]["source_version"]
        == "new"
    )
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute("UPDATE knowledge.snapshots SET config='{}' WHERE snapshot_id=%s", (old,))
    with pytest.raises(psycopg.Error), engine.transaction() as conn:
        conn.execute("DELETE FROM knowledge.snapshot_chunks WHERE snapshot_id=%s", (old,))


def test_acl_revocation_rechecks_cache_and_old_snapshot(engine):
    snapshot = upload(engine)
    result = engine.knowledge.search(Search(query="订单统计"))
    from uuid import UUID

    document = UUID(result["hits"][0]["document_id"])
    chunk = UUID(result["hits"][0]["chunk_id"])
    engine.knowledge.acl(document, ACLUpdate(revoked=True))
    assert not engine.knowledge.search(Search(query="订单统计", snapshot_id=snapshot))["hits"]
    with pytest.raises(DomainError):
        engine.knowledge.read(chunk, snapshot)
    engine.knowledge.acl(document, ACLUpdate())
    assert engine.knowledge.read(chunk, snapshot)["text"]
    with pytest.raises(DomainError):
        engine.knowledge.eligible(snapshot, "outsider")


def test_overlapping_ingestions_use_compare_and_swap(engine):
    def body(uri):
        return Upload(
            title="x",
            source_uri=uri,
            source_version="v1",
            media_type="text/plain",
            content_base64=base64.b64encode("并发索引资料".encode()).decode(),
        )

    first = engine.knowledge.upload(body("kb://one"))
    second = engine.knowledge.upload(body("kb://two"))
    snapshot = engine.knowledge.index(first["job_id"])
    with pytest.raises(DomainError) as exc:
        engine.knowledge.index(second["job_id"])
    assert exc.value.code == "SNAPSHOT_CONFLICT"
    assert engine.knowledge.active() == snapshot
    retry = engine.knowledge.upload(body("kb://two"))
    assert retry["job_id"] != second["job_id"]
    engine.knowledge.index(retry["job_id"])
    assert len(engine.knowledge.search(Search(query="资料"))["hits"]) == 2


def test_redis_outage_falls_back(engine):
    upload(engine)
    from pydantic import SecretStr

    engine.settings.redis_url = SecretStr("redis://127.0.0.1:1/0")
    result = engine.knowledge.search(Search(query="订单"))
    assert result["cache_state"] == "unavailable" and result["hits"]


def test_parser_locators_scanned_pdf_and_bounds():
    assert "order_id" in terms("订单按 order_id 去重")
    chunks = parse(b"def example():\n    return 1\n", "text/x-python")
    assert chunks[0].locator == {"start_line": 1, "end_line": 2, "symbol": "example"}
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(DomainError) as exc:
        parse(stream.getvalue(), "application/pdf")
    assert exc.value.code == "SCANNED_PDF_UNSUPPORTED"
    for data in (b"a" * 5000, b"\x00", b"\xff"):
        with pytest.raises(DomainError):
            parse(data, "text/plain")


def test_ingestion_crash_lease_is_recovered(engine):
    job = engine.knowledge.upload(
        Upload(
            title="x",
            source_uri="kb://one",
            source_version="v1",
            media_type="text/plain",
            content_base64=base64.b64encode(b"recovery").decode(),
        )
    )
    with engine.transaction() as conn:
        conn.execute(
            "UPDATE knowledge.ingestion_jobs SET status='indexing',lease_token=%s WHERE job_id=%s",
            (uuid4(), job["job_id"]),
        )
    engine.knowledge.recover()
    assert engine.knowledge.index(job["job_id"]) == engine.knowledge.active()


def test_redis_tampering_and_model_dimension_mismatch_do_not_leak(engine):
    import json

    from redis import Redis

    from mgar_v3.domain import digest
    from mgar_v3.rag.embedding import normalize

    snapshot = upload(engine)
    request = Search(query="订单统计", snapshot_id=snapshot)
    original = engine.knowledge.search(request)
    snap, _, scope = engine.knowledge.eligible(snapshot, "owner")
    key = "mgar:rag:" + digest(
        {
            "request": digest(request.model_dump(mode="json")),
            "snapshot": str(snapshot),
            "scope": scope,
            "config": snap["config_hash"],
        }
    )
    with Redis.from_url(engine.settings.redis_url.get_secret_value()) as cache:
        envelope = json.loads(cache.get(key))
        envelope["value"]["hits"][0]["snippet"] = "poisoned privileged source"
        cache.setex(key, 60, json.dumps(envelope))
    restored = engine.knowledge.search(request)
    assert restored["hits"][0]["snippet"] == original["hits"][0]["snippet"]
    with pytest.raises(DomainError):
        normalize([1, 2], 300)


def test_pdf_actual_text_and_page_locator_are_read():
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 200 Td (Order count uses distinct order_id.) Tj ET")
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    chunks = parse(output.getvalue(), "application/pdf")
    assert "distinct order_id" in chunks[0].text and chunks[0].locator["page"] == 1
