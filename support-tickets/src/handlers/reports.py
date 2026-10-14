"""HTTP layer for /reports."""
from common import http
from services import report_service as svc


def create(event, rid):
    raw = event.get("body")
    data = http.json_body(event) if raw else {}  # body is optional (no filter)
    return http.response(202, svc.request_report(http.user_id(event), data, rid), rid)


def get(event, rid):
    return http.response(200, svc.get_report(http.user_id(event), event["pathParameters"]["reportId"]), rid)


ROUTES = {"POST /reports": create, "GET /reports/{reportId}": get}


def lambda_handler(event, context):
    return http.dispatch(event, ROUTES)
