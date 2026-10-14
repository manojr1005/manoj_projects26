"""HTTP layer for /tickets: parse the request, call the service, shape the response."""
from common import http
from services import ticket_service as svc


def create(event, rid):
    ticket = svc.create_ticket(http.user_id(event), http.json_body(event))
    return http.response(201, ticket, rid)


def get(event, rid):
    return http.response(200, svc.get_ticket(http.user_id(event), event["pathParameters"]["ticketId"]), rid)


def list_(event, rid):
    q = event.get("queryStringParameters") or {}
    result = svc.list_tickets(http.user_id(event), q.get("status"), q.get("limit"), q.get("nextToken"))
    return http.response(200, result, rid)


def patch(event, rid):
    ticket = svc.update_ticket(
        http.user_id(event), event["pathParameters"]["ticketId"], http.json_body(event)
    )
    return http.response(200, ticket, rid)


def delete(event, rid):
    svc.delete_ticket(http.user_id(event), event["pathParameters"]["ticketId"])
    return http.response(204, None, rid)


ROUTES = {
    "POST /tickets": create,
    "GET /tickets": list_,
    "GET /tickets/{ticketId}": get,
    "PATCH /tickets/{ticketId}": patch,
    "DELETE /tickets/{ticketId}": delete,
}


def lambda_handler(event, context):
    return http.dispatch(event, ROUTES)
