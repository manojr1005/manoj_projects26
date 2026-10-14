"""Ticket business rules: validation, id/timestamp generation. No HTTP, no boto3 here."""
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from common.errors import ValidationError
from repositories import ticket_repository as repo

PRIORITIES = ("LOW", "MEDIUM", "HIGH")
STATUSES = ("OPEN", "IN_PROGRESS", "CLOSED")
MAX_TITLE, MAX_DESCRIPTION = 200, 5000
UPDATABLE = ("title", "description", "priority", "status")
DEFAULT_LIMIT, MAX_LIMIT = 20, 100


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_ticket_id() -> str:
    """Time-sortable (ms timestamp prefix) + random suffix: newest-first listing, no collisions."""
    return f"{int(time.time() * 1000):012x}{secrets.token_hex(6)}"


def _text(data: dict, field: str, max_len: int, required: bool):
    if field not in data:
        if required:
            raise ValidationError(f"'{field}' is required")
        return None
    value = data[field]
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"'{field}' must be a non-empty string")
    if len(value) > max_len:
        raise ValidationError(f"'{field}' must be at most {max_len} characters")
    return value.strip()


def _enum(data: dict, field: str, allowed: tuple, default=None):
    if field not in data:
        return default
    if data[field] not in allowed:
        raise ValidationError(f"'{field}' must be one of {list(allowed)}")
    return data[field]


def validate_status_filter(status: Optional[str]) -> Optional[str]:
    if status is not None and status not in STATUSES:
        raise ValidationError(f"'status' must be one of {list(STATUSES)}")
    return status


def create_ticket(user_id: str, data: dict) -> dict:
    allowed = {"title", "description", "priority", "status"}
    unknown = set(data) - allowed
    if unknown:  # also rejects a client supplied createdBy / ticketId / timestamps
        raise ValidationError(f"Unknown or read-only fields: {sorted(unknown)}")
    now = now_utc()
    ticket = {
        "ticketId": new_ticket_id(),
        "title": _text(data, "title", MAX_TITLE, True),
        "description": _text(data, "description", MAX_DESCRIPTION, True),
        "priority": _enum(data, "priority", PRIORITIES, "MEDIUM"),
        "status": _enum(data, "status", STATUSES, "OPEN"),
        "createdBy": user_id,
        "createdAt": now,
        "updatedAt": now,
    }
    repo.put(ticket, user_id)
    return ticket


def get_ticket(user_id: str, ticket_id: str) -> dict:
    return repo.get(user_id, ticket_id)


def update_ticket(user_id: str, ticket_id: str, data: dict) -> dict:
    unknown = set(data) - set(UPDATABLE)
    if unknown:
        raise ValidationError(f"Unknown or read-only fields: {sorted(unknown)}")
    if not data:
        raise ValidationError(f"Provide at least one of {list(UPDATABLE)}")
    fields = {}
    for f, mx in (("title", MAX_TITLE), ("description", MAX_DESCRIPTION)):
        v = _text(data, f, mx, False)
        if v is not None:
            fields[f] = v
    for f, allowed in (("priority", PRIORITIES), ("status", STATUSES)):
        v = _enum(data, f, allowed)
        if v is not None:
            fields[f] = v
    return repo.update(user_id, ticket_id, fields, now_utc())


def delete_ticket(user_id: str, ticket_id: str) -> None:
    repo.delete(user_id, ticket_id)


def list_tickets(user_id: str, status: Optional[str], limit_raw: Optional[str], token: Optional[str]):
    validate_status_filter(status)
    limit = DEFAULT_LIMIT
    if limit_raw is not None:
        try:
            limit = int(limit_raw)
        except ValueError:
            raise ValidationError("'limit' must be an integer")
        if not 1 <= limit <= MAX_LIMIT:
            raise ValidationError(f"'limit' must be between 1 and {MAX_LIMIT}")
    tickets, next_token = repo.list_page(user_id, status, limit, token)
    result = {"items": tickets}
    if next_token:
        result["nextToken"] = next_token
    return result
