import re
from datetime import datetime

import pytest


def test_create_ticket_success(api):
    res = api.create_ticket(title="  Login fails ", description="500 error", priority="HIGH")
    assert res["statusCode"] == 201
    t = res["json"]
    assert t["title"] == "Login fails"
    assert t["priority"] == "HIGH" and t["status"] == "OPEN"
    assert t["createdBy"] == "user-a"
    assert re.fullmatch(r"[0-9a-f]{24}", t["ticketId"])
    assert t["createdAt"].endswith("Z") and t["createdAt"] == t["updatedAt"]
    datetime.fromisoformat(t["createdAt"].replace("Z", "+00:00"))
    assert api.get_ticket(t["ticketId"])["json"] == t


def test_created_by_is_taken_from_jwt_not_body(api):
    assert api.create_ticket(createdBy="someone-else")["statusCode"] == 400
    assert api.create_ticket(user="user-b")["json"]["createdBy"] == "user-b"


def test_defaults(api):
    t = api.create_ticket()["json"]
    assert (t["priority"], t["status"]) == ("MEDIUM", "OPEN")


def test_update_ticket_success(api):
    t = api.create_ticket()["json"]
    res = api.patch_ticket(t["ticketId"], {"status": "IN_PROGRESS", "priority": "LOW", "title": "New"})
    assert res["statusCode"] == 200
    u = res["json"]
    assert (u["status"], u["priority"], u["title"]) == ("IN_PROGRESS", "LOW", "New")
    assert u["description"] == t["description"] and u["createdAt"] == t["createdAt"]
    assert u["updatedAt"] >= t["updatedAt"]
    # status index follows the update
    assert [x["ticketId"] for x in api.list_tickets(status="IN_PROGRESS")["json"]["items"]] == [t["ticketId"]]
    assert api.list_tickets(status="OPEN")["json"]["items"] == []


@pytest.mark.parametrize("body", [
    {}, {"title": ""}, {"title": 5}, {"description": "  "}, {"priority": "URGENT"}, {"status": "DONE"},
    {"createdBy": "x"}, {"ticketId": "x"}, {"createdAt": "x"},
])
def test_update_invalid_input(api, body):
    t = api.create_ticket()["json"]
    res = api.patch_ticket(t["ticketId"], body)
    assert res["statusCode"] == 400
    assert res["json"]["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("body", [
    {}, {"title": "x"}, {"description": "x"}, {"title": "x", "description": "y", "priority": "BAD"},
    {"title": "x", "description": "y", "status": "BAD"}, {"title": "t" * 201, "description": "y"},
    {"title": 1, "description": "y"}, "not json", "[1,2]",
])
def test_create_invalid_input(api, body):
    from tests.conftest import call
    from handlers import tickets
    res = call(tickets, "POST", "/tickets", body=body)
    assert res["statusCode"] == 400
    err = res["json"]["error"]
    assert err["code"] == "VALIDATION_ERROR" and err["requestId"] == "req-123" and err["message"]


def test_missing_body(api):
    from tests.conftest import call
    from handlers import tickets
    assert call(tickets, "POST", "/tickets")["statusCode"] == 400


def test_missing_ticket_returns_404_everywhere(api):
    for res in (api.get_ticket("nope"), api.patch_ticket("nope", {"title": "x"}), api.delete_ticket("nope")):
        assert res["statusCode"] == 404
        assert res["json"]["error"]["code"] == "NOT_FOUND"
        assert "nope" in res["json"]["error"]["message"]


def test_delete(api):
    t = api.create_ticket()["json"]
    res = api.delete_ticket(t["ticketId"])
    assert res["statusCode"] == 204 and res["body"] == ""
    assert api.get_ticket(t["ticketId"])["statusCode"] == 404
    assert api.delete_ticket(t["ticketId"])["statusCode"] == 404


def test_pagination(api):
    ids = [api.create_ticket(title=f"t{i}")["json"]["ticketId"] for i in range(5)]
    seen, token, pages = [], None, 0
    while True:
        q = {"limit": "2"}
        if token:
            q["nextToken"] = token
        body = api.list_tickets(**q)["json"]
        assert len(body["items"]) <= 2
        seen += [t["ticketId"] for t in body["items"]]
        pages += 1
        token = body.get("nextToken")
        if not token:
            break
    assert seen == list(reversed(ids))  # newest first, no gaps, no duplicates
    assert pages >= 3


@pytest.mark.parametrize("q", [{"limit": "0"}, {"limit": "101"}, {"limit": "abc"}, {"status": "NOPE"},
                               {"nextToken": "garbage"}])
def test_list_invalid_params(api, q):
    assert api.list_tickets(**q)["statusCode"] == 400


def test_status_filtering(api):
    a = api.create_ticket(status="OPEN")["json"]
    b = api.create_ticket(status="CLOSED")["json"]
    c = api.create_ticket(status="CLOSED")["json"]
    closed = api.list_tickets(status="CLOSED")["json"]["items"]
    assert {t["ticketId"] for t in closed} == {b["ticketId"], c["ticketId"]}
    assert [t["ticketId"] for t in api.list_tickets(status="OPEN")["json"]["items"]] == [a["ticketId"]]
    assert api.list_tickets(status="IN_PROGRESS")["json"]["items"] == []
    assert len(api.list_tickets()["json"]["items"]) == 3


def test_user_cannot_access_another_users_ticket(api):
    t = api.create_ticket(user="user-a")["json"]
    tid = t["ticketId"]
    assert api.get_ticket(tid, user="user-b")["statusCode"] == 404
    assert api.patch_ticket(tid, {"title": "hacked"}, user="user-b")["statusCode"] == 404
    assert api.delete_ticket(tid, user="user-b")["statusCode"] == 404
    assert api.list_tickets(user="user-b")["json"]["items"] == []
    assert api.list_tickets(user="user-b", status="OPEN")["json"]["items"] == []
    # untouched for the owner
    assert api.get_ticket(tid)["json"]["title"] == t["title"]


def test_next_token_cannot_be_used_by_another_user(api):
    for i in range(3):
        api.create_ticket(title=f"t{i}")
    token = api.list_tickets(limit="1")["json"]["nextToken"]
    assert api.list_tickets(user="user-b", nextToken=token)["statusCode"] == 400


def test_missing_identity_is_401(api):
    from tests.conftest import call
    from handlers import tickets
    res = call(tickets, "GET", "/tickets", user=None)
    assert res["statusCode"] == 401 and res["json"]["error"]["code"] == "UNAUTHORIZED"
