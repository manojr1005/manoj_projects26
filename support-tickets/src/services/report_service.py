"""Report business logic: request, status lookup and the worker's job processing."""
import csv
import io
import uuid
from typing import Optional

from common import config
from common.errors import ServiceUnavailableError
from common.logging_utils import get_logger
from repositories import report_repository as reports
from repositories import ticket_repository as tickets
from services.ticket_service import now_utc, validate_status_filter

log = get_logger()

CSV_COLUMNS = ("ticketId", "title", "description", "priority", "status", "createdBy", "createdAt", "updatedAt")


def request_report(user_id: str, data: dict, request_id: str) -> dict:
    from common.errors import ValidationError

    unknown = set(data) - {"status"}
    if unknown:
        raise ValidationError(f"Unknown fields: {sorted(unknown)}")
    status_filter = validate_status_filter(data.get("status"))
    report_id = str(uuid.uuid4())
    now = now_utc()
    item = reports.create(user_id, report_id, status_filter, now)  # 1) durable record (QUEUED)
    try:
        reports.publish_job(user_id, report_id, status_filter)  # 2) enqueue
    except Exception:
        log.exception("failed to enqueue report job", extra={"requestId": request_id, "reportId": report_id})
        # Best effort: surface the failure instead of leaving a report QUEUED forever. If even this
        # write fails (or the process dies between 1 and 2) the record stays QUEUED and is picked up
        # by scripts/requeue_stale_reports.py (see docs/RELIABILITY.md).
        try:
            reports.set_status(user_id, report_id, "FAILED", now_utc(),
                               extra={"errorMessage": "Failed to enqueue report job"})
        except Exception:
            log.exception("failed to mark report FAILED", extra={"reportId": report_id})
        raise ServiceUnavailableError("Could not queue the report, please retry")
    log.info("report queued", extra={"requestId": request_id, "reportId": report_id})
    return reports.public(item)


def get_report(user_id: str, report_id: str) -> dict:
    item = reports.get(user_id, report_id)
    result = reports.public(item)
    if item["status"] == "COMPLETED":
        result["downloadUrl"] = reports.presign(user_id, report_id)
        result["downloadUrlExpiresInSeconds"] = config.presign_ttl_seconds()
    return result


def _csv_cell(value) -> str:
    """Neutralise spreadsheet formula injection (=, +, -, @ prefixes)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def build_csv(rows) -> (bytes, int):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    count = 0
    for t in rows:
        writer.writerow([_csv_cell(t.get(c)) for c in CSV_COLUMNS])
        count += 1
    return buf.getvalue().encode("utf-8"), count


def process_job(job: dict, receive_count: int) -> str:
    """Idempotent job handler. Returns an outcome string (for logs/tests).

    Safe to run any number of times for the same reportId: COMPLETED reports are skipped, the S3 key
    is deterministic (overwrite, never a second object) and the final write cannot clobber COMPLETED.
    Raising makes SQS retry; after maxReceiveCount the message lands in the DLQ.
    """
    report_id, user_id = job["reportId"], job["userId"]
    status_filter: Optional[str] = job.get("statusFilter")
    ctx = {"reportId": report_id, "receiveCount": receive_count}

    from common.errors import NotFoundError

    try:
        item = reports.get(user_id, report_id)
    except NotFoundError:
        log.warning("report record missing, dropping job", extra=ctx)
        return "dropped"
    if item["status"] == "COMPLETED":
        log.info("duplicate delivery ignored, report already completed", extra=ctx)
        return "duplicate"

    try:
        if not reports.set_status(user_id, report_id, "PROCESSING", now_utc()):
            log.info("report completed concurrently, skipping", extra=ctx)
            return "duplicate"
        # Contents reflect ticket state at PROCESSING time (documented trade-off).
        data, count = build_csv(tickets.iter_all(user_id, status_filter))
        reports.put_csv(user_id, report_id, data)
        done = now_utc()
        reports.set_status(user_id, report_id, "COMPLETED", done,
                           extra={"ticketCount": count, "completedAt": done})
        log.info("report completed", extra={**ctx, "ticketCount": count})
        return "completed"
    except Exception as exc:
        log.exception("report processing failed", extra=ctx)
        if receive_count >= config.max_receive_count():
            reports.set_status(user_id, report_id, "FAILED", now_utc(),
                               extra={"errorMessage": f"Processing failed: {type(exc).__name__}"})
        raise
