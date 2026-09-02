import asyncio
import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from core.agent.orchestrator import AgentOrchestrator, RunPoolManager
from core.api.dependencies import get_db, resolve_entity_by_id_or_prefix
from core.api.rate_limiter import rate_limit
from core.api.serializers import result_to_out, run_to_out
from core.db.orm import Automation, Result, Run
from core.db.session import SessionLocal
from core.events.bus import event_bus
from core.events.sse import format_sse, format_sse_ping, sse_response
from core.events.types import OrbitEvent
from core.models.enums import RunStatus
from core.models.schemas import RunOut

logger = logging.getLogger("core.api.v1.runs")

router = APIRouter(tags=["Runs"])
orchestrator = AgentOrchestrator()


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str, db: Annotated[Session, Depends(get_db)]):
    """Retrieves detailed execution audit trail and results for a specific run."""
    run = resolve_entity_by_id_or_prefix(db, Run, run_id, "run")
    return run_to_out(run)


@router.get("/runs/{run_id}/stream")
async def stream_run_telemetry(run_id: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    """
    Streams live run telemetry, stage transitions, reasoning logs, and status updates via Server-Sent Events (SSE).
    """
    run = resolve_entity_by_id_or_prefix(db, Run, run_id, "run")
    canonical_run_id = run.id
    initial_payload = run_to_out(run)

    async def event_generator():
        # 1. Send initial snapshot immediately
        yield format_sse(data=initial_payload, event="snapshot")

        # If already completed or failed, close the stream cleanly
        if initial_payload.status in (RunStatus.verified, RunStatus.failed):
            yield format_sse(data=initial_payload, event="complete")
            return

        queue: asyncio.Queue[OrbitEvent | None] = asyncio.Queue()

        async def _on_event(evt: OrbitEvent):
            if evt.run_id == canonical_run_id:
                await queue.put(evt)

        event_bus.subscribe(_on_event)

        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=1.0)
                    if evt is None:
                        break

                    with SessionLocal() as poll_db:
                        current_run = poll_db.query(Run).filter(Run.id == canonical_run_id).first()
                        if current_run:
                            out = run_to_out(current_run)
                            event_name = "complete" if out.status in (RunStatus.verified, RunStatus.failed) else "update"
                            yield format_sse(data=out, event=event_name)

                            if out.status in (RunStatus.verified, RunStatus.failed):
                                break
                except asyncio.TimeoutError:
                    with SessionLocal() as poll_db:
                        current_run = poll_db.query(Run).filter(Run.id == canonical_run_id).first()
                        if current_run:
                            out = run_to_out(current_run)
                            if out.status in (RunStatus.verified, RunStatus.failed):
                                yield format_sse(data=out, event="complete")
                                break
                    yield format_sse_ping()
        finally:
            event_bus.unsubscribe(_on_event)

    return sse_response(event_generator())


@router.get("/runs/{run_id}/results/stream")
async def stream_run_results(run_id: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    """
    Streams extracted and validated data entities incrementally via Server-Sent Events (SSE).
    """
    run = resolve_entity_by_id_or_prefix(db, Run, run_id, "run")
    canonical_run_id = run.id

    async def event_generator():
        sent_ids = set()

        # 1. Stream existing records first
        with SessionLocal() as cur_db:
            existing_results = cur_db.query(Result).filter(Result.run_id == canonical_run_id).all()
            for res in existing_results:
                sent_ids.add(res.id)
                yield format_sse(data=result_to_out(res), event="record")

        # If already finished, terminate
        with SessionLocal() as cur_db:
            cur_run = cur_db.query(Run).filter(Run.id == canonical_run_id).first()
            if cur_run and cur_run.status in (RunStatus.verified, RunStatus.failed):
                yield format_sse(data={"total": len(sent_ids), "status": cur_run.status.value}, event="complete")
                return

        queue: asyncio.Queue[OrbitEvent | None] = asyncio.Queue()

        async def _on_event(evt: OrbitEvent):
            if evt.run_id == canonical_run_id:
                await queue.put(evt)

        event_bus.subscribe(_on_event)

        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=1.0)
                    if evt is None:
                        break

                    with SessionLocal() as cur_db:
                        new_results = (
                            cur_db.query(Result)
                            .filter(Result.run_id == canonical_run_id, ~Result.id.in_(sent_ids) if sent_ids else True)
                            .all()
                        )
                        for res in new_results:
                            sent_ids.add(res.id)
                            yield format_sse(data=result_to_out(res), event="record")

                        cur_run = cur_db.query(Run).filter(Run.id == canonical_run_id).first()
                        if cur_run and cur_run.status in (RunStatus.verified, RunStatus.failed):
                            yield format_sse(data={"total": len(sent_ids), "status": cur_run.status.value}, event="complete")
                            break
                except asyncio.TimeoutError:
                    with SessionLocal() as cur_db:
                        new_results = (
                            cur_db.query(Result)
                            .filter(Result.run_id == canonical_run_id, ~Result.id.in_(sent_ids) if sent_ids else True)
                            .all()
                        )
                        for res in new_results:
                            sent_ids.add(res.id)
                            yield format_sse(data=result_to_out(res), event="record")

                        cur_run = cur_db.query(Run).filter(Run.id == canonical_run_id).first()
                        if cur_run and cur_run.status in (RunStatus.verified, RunStatus.failed):
                            yield format_sse(data={"total": len(sent_ids), "status": cur_run.status.value}, event="complete")
                            break
                    yield format_sse_ping()
        finally:
            event_bus.unsubscribe(_on_event)

    return sse_response(event_generator())


@router.get("/runs/{run_id}/dossier")
def get_run_dossier(run_id: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    """Streams the generated and redacted PDF/HTML report dossier with RFC 7234 ETag caching."""
    run = resolve_entity_by_id_or_prefix(db, Run, run_id, "run")

    updated_ts = int(run.finished_at.timestamp() if run.finished_at else (run.started_at.timestamp() if run.started_at else 0))
    etag = f'W/"{run.id[:12]}-{updated_ts}"'

    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=86400, stale-while-revalidate=3600" if run.status in (RunStatus.verified, RunStatus.failed) else "no-cache",
    }

    if request.headers.get("if-none-match") == etag or request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=headers)

    pdf_path = os.path.join("exports", run.automation_id, run.id, "dossier.pdf")
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            return Response(content=f.read(), media_type="application/pdf", headers=headers)

    # Generate interactive HTML fallback if PDF file is not on local disk
    html_report = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Orbit Executive Briefing - Run {run.id[:12]}</title>
    <style>body{{font-family:system-ui;background:#090d16;color:#e2e8f0;padding:2rem;}}
    .badge{{background:#06b6d420;color:#06b6d4;padding:4px 8px;border-radius:4px;font-family:monospace;}}</style>
    </head>
    <body>
    <h2>🛰️ Orbit Report Dossier <span class="badge">Run: {run.id[:12]}</span></h2>
    <p>Extraction Status: <strong>{run.status.value}</strong> | Records: {run.extracted_count}</p>
    <hr style="border:1px solid #1e293b;margin:1.5rem 0;"/>
    <p>PII Entities Masked: <strong>Active (Document Compliance Redactor)</strong></p>
    </body></html>
    """
    return Response(content=html_report.encode("utf-8"), media_type="text/html", headers=headers)


@router.post("/runs/{run_id}/retry", response_model=RunOut, dependencies=[Depends(rate_limit("run"))])
async def retry_run(run_id: str, db: Annotated[Session, Depends(get_db)]):
    """Resumes and retries execution of an existing run from its last checkpoint in background."""
    run = resolve_entity_by_id_or_prefix(db, Run, run_id, "run")

    if RunPoolManager.is_run_active(run.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run '{run.id[:8]}' is currently executing in background. Cannot trigger concurrent retry.",
        )

    automation = db.query(Automation).filter(Automation.id == run.automation_id).first()
    if not automation:
        raise HTTPException(status_code=404, detail="Parent automation not found")

    run.status = RunStatus.retrieving if run.sources_found else RunStatus.discovering
    run.error, run.finished_at = None, None
    db.commit()
    db.refresh(run)

    async def _execute_retry(auto_id: str, r_id: str):
        with SessionLocal() as bg_db:
            bg_auto = bg_db.query(Automation).filter(Automation.id == auto_id).first()
            bg_run = bg_db.query(Run).filter(Run.id == r_id).first()
            if bg_auto and bg_run:
                try:
                    await orchestrator.execute_run(bg_db, bg_auto, run=bg_run, resume=True)
                except Exception as err:
                    logger.exception("Background execution error in run %s: %s", r_id, err)

    asyncio.create_task(_execute_retry(automation.id, run.id))
    return run_to_out(run)


@router.get("/automations/{automation_id}/runs", response_model=list[RunOut])
def list_automation_runs(automation_id: str, db: Annotated[Session, Depends(get_db)]):
    """Lists past execution history for a given automation."""
    automation = resolve_entity_by_id_or_prefix(db, Automation, automation_id, "automation")
    runs = db.query(Run).filter(Run.automation_id == automation.id).order_by(Run.started_at.desc()).all()
    return [run_to_out(r) for r in runs]
