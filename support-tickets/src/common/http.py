"""HTTP helpers for API Gateway HTTP API (payload format 2.0)."""
import base64
import json
from decimal import Decimal

from common.errors import AppError, UnauthorizedError, ValidationError
from common.logging_utils import get_logger

log = get_logger()


def _json_default(o):
    if isinstance(o, Decimal):  # DynamoDB numbers
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Not JSON serializable: {type(o).__name__}")


def response(status: int, body=None, request_id: str = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if request_id:
        headers["X-Request-Id"] = request_id
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, default=_json_default) if body is not None else "",
    }


def error_response(err: AppError, request_id: str) -> dict:
    payload = {"error": {"code": err.code, "message": err.message, "requestId": request_id}}
    if err.details:
        payload["error"]["details"] = err.details
    return response(err.status, payload, request_id)


def request_id(event: dict) -> str:
    return event.get("requestContext", {}).get("requestId", "unknown")


def user_id(event: dict) -> str:
    """Identity comes ONLY from the JWT claims validated by the API Gateway authorizer."""
    claims = (
        event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    )
    sub = claims.get("sub")
    if not sub:
        raise UnauthorizedError("Missing authenticated identity")
    return sub


def json_body(event: dict) -> dict:
    raw = event.get("body")
    if not raw:
        raise ValidationError("Request body is required")
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValidationError("Request body must be valid JSON")
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object")
    return data


def dispatch(event: dict, routes: dict):
    """Run the handler for event['routeKey'], mapping errors to the standard body."""
    rid = request_id(event)
    route = event.get("routeKey")
    try:
        handler = routes.get(route)
        if handler is None:
            from common.errors import NotFoundError

            raise NotFoundError("Route not found")
        return handler(event, rid)
    except AppError as err:
        if err.status >= 500:
            log.error("request failed", extra={"requestId": rid, "route": route, "code": err.code})
        else:
            log.info("request rejected", extra={"requestId": rid, "route": route, "code": err.code})
        return error_response(err, rid)
    except Exception:  # never leak internals
        log.exception("unhandled error", extra={"requestId": rid, "route": route})
        return response(
            500,
            {"error": {"code": "INTERNAL_ERROR", "message": "Internal server error", "requestId": rid}},
            rid,
        )
