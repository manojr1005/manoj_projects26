import csv
import io
import json
from unittest.mock import patch

import boto3
import pytest

from tests.conftest import BUCKET, sqs_messages, to_sqs_event


def run_worker(aws, receive_count=1):
    from handlers import report_worker

    msgs = sqs_messages(aws["queue"])
    return report_worker.lambda_handler(to_sqs_event(msgs, receive_count), None), msgs


def read_csv(user, rid):
    body = boto3.client("s3").get_object(Bucket=BUCKET, Key=f"reports/{user}/{rid}.csv")["Body"].read().decode()
    return list(csv.DictReader(io.StringIO(body)))


def test_request_returns_202_and_queues_job(api, aws):
    res = api.create_report()
    assert res["statusCode"] == 202
    r = res["json"]
    assert r["status"] == "QUEUED" and r["reportId"]
    msgs = sqs_messages(aws["queue"])
    assert len(msgs) == 1
    assert json.loads(msgs[0]["Body"]) == {"reportId": r["reportId"], "userId": "user-a", "statusFilter": None}


def test_invalid_report_filter(api):
    assert api.create_report(body={"status": "NOPE"})["statusCode"] == 400
    assert api.create_report(body={"x": 1})["statusCode"] == 400


def test_successful_report_generation_with_filter(api, aws):
    api.create_ticket(title="a", status="OPEN")
    api.create_ticket(title="b", status="CLOSED")
    api.create_ticket(user="user-b", title="other user", status="CLOSED")
    rid = api.create_report(body={"status": "CLOSED"})["json"]["reportId"]
    out, _ = run_worker(aws)
    assert out == {"batchItemFailures": []}
    r = api.get_report(rid)["json"]
    assert r["status"] == "COMPLETED" and r["ticketCount"] == 1 and "downloadUrl" in r
    assert "X-Amz-Signature" in r["downloadUrl"] and "X-Amz-Expires=300" in r["downloadUrl"]
    rows = read_csv("user-a", rid)
    assert [x["title"] for x in rows] == ["b"] and rows[0]["createdBy"] == "user-a"


def test_report_without_filter_contains_all_own_tickets_only(api, aws):
    api.create_ticket(title="a", status="OPEN")
    api.create_ticket(title="b", status="CLOSED")
    api.create_ticket(user="user-b", title="x")
    rid = api.create_report()["json"]["reportId"]
    run_worker(aws)
    assert sorted(x["title"] for x in read_csv("user-a", rid)) == ["a", "b"]


def test_empty_report_is_header_only(api, aws):
    rid = api.create_report()["json"]["reportId"]
    run_worker(aws)
    assert api.get_report(rid)["json"]["ticketCount"] == 0 and read_csv("user-a", rid) == []


def test_csv_formula_injection_neutralised(api, aws):
    api.create_ticket(title="=HYPERLINK(\"http://evil\")", description="+1")
    rid = api.create_report()["json"]["reportId"]
    run_worker(aws)
    row = read_csv("user-a", rid)[0]
    assert row["title"].startswith("'=") and row["description"].startswith("'+")


def test_user_cannot_access_another_users_report(api, aws):
    rid = api.create_report(user="user-a")["json"]["reportId"]
    run_worker(aws)
    assert api.get_report(rid, user="user-b")["statusCode"] == 404
    assert api.get_report(rid, user="user-a")["statusCode"] == 200
    assert api.get_report("does-not-exist")["json"]["error"]["code"] == "NOT_FOUND"


def test_not_ready_report_has_no_download_link(api):
    rid = api.create_report()["json"]["reportId"]
    r = api.get_report(rid)["json"]
    assert r["status"] == "QUEUED" and "downloadUrl" not in r


def test_duplicate_delivery_is_idempotent(api, aws):
    api.create_ticket()
    rid = api.create_report()["json"]["reportId"]
    msgs = sqs_messages(aws["queue"])
    event = to_sqs_event(msgs)
    from handlers import report_worker
    from repositories import report_repository

    with patch.object(report_repository, "put_csv", wraps=report_repository.put_csv) as put:
        assert report_worker.lambda_handler(event, None) == {"batchItemFailures": []}
        first = api.get_report(rid)["json"]
        assert report_worker.lambda_handler(event, None) == {"batchItemFailures": []}  # redelivery
        assert put.call_count == 1  # second delivery did no work
    second = api.get_report(rid)["json"]
    assert first["completedAt"] == second["completedAt"] and second["status"] == "COMPLETED"
    keys = boto3.client("s3").list_objects_v2(Bucket=BUCKET).get("KeyCount")
    assert keys == 1
    items = boto3.client("dynamodb").scan(TableName="app-table")["Items"]  # test-only scan
    assert len([i for i in items if i["sk"]["S"].startswith("REPORT#")]) == 1


def test_concurrent_duplicate_cannot_overwrite_completed(api, aws):
    """A late duplicate that already passed the 'is it completed?' check must not clobber COMPLETED."""
    from repositories import report_repository as repo

    api.create_ticket()
    rid = api.create_report()["json"]["reportId"]
    run_worker(aws)
    assert repo.set_status("user-a", rid, "PROCESSING", "2030-01-01T00:00:00Z") is False
    assert api.get_report(rid)["json"]["status"] == "COMPLETED"


def test_worker_failure_is_retried_then_marked_failed_and_recoverable(api, aws):
    from repositories import report_repository

    api.create_ticket()
    rid = api.create_report()["json"]["reportId"]
    msgs = sqs_messages(aws["queue"])

    with patch.object(report_repository, "put_csv", side_effect=RuntimeError("s3 down")):
        from handlers import report_worker

        # attempt 1 & 2: reported as batch item failure (SQS will redeliver), status not yet FAILED
        for attempt in (1, 2):
            out = report_worker.lambda_handler(to_sqs_event(msgs, attempt), None)
            assert out["batchItemFailures"] == [{"itemIdentifier": msgs[0]["MessageId"]}]
            assert api.get_report(rid)["json"]["status"] == "PROCESSING"
        # final attempt: FAILED is recorded (message then goes to the DLQ via the redrive policy)
        out = report_worker.lambda_handler(to_sqs_event(msgs, 3), None)
        assert len(out["batchItemFailures"]) == 1
    failed = api.get_report(rid)["json"]
    assert failed["status"] == "FAILED" and "RuntimeError" in failed["errorMessage"]
    assert "s3 down" not in failed["errorMessage"]  # no internal details leak to clients

    # recovery: after the fault is fixed, redriving the DLQ message completes the SAME report
    out = report_worker.lambda_handler(to_sqs_event(msgs, 1), None)
    assert out == {"batchItemFailures": []}
    done = api.get_report(rid)["json"]
    assert done["status"] == "COMPLETED" and "errorMessage" not in done


def test_queue_redrive_policy_sends_to_dlq_after_max_receives(aws):
    sqs = boto3.client("sqs")
    sqs.send_message(QueueUrl=aws["queue"], MessageBody="{}")
    for _ in range(3):
        m = sqs.receive_message(QueueUrl=aws["queue"], VisibilityTimeout=0)
        assert m.get("Messages")
    sqs.receive_message(QueueUrl=aws["queue"], VisibilityTimeout=0)
    assert sqs.receive_message(QueueUrl=aws["dlq"]).get("Messages")


def test_enqueue_failure_marks_report_failed_and_returns_503(api, aws):
    from repositories import report_repository

    with patch.object(report_repository, "publish_job", side_effect=RuntimeError("sqs down")):
        res = api.create_report()
    assert res["statusCode"] == 503 and res["json"]["error"]["code"] == "SERVICE_UNAVAILABLE"
    items = [i for i in boto3.client("dynamodb").scan(TableName="app-table")["Items"] if i["sk"]["S"].startswith("REPORT#")]
    assert [i["status"]["S"] for i in items] == ["FAILED"]


def test_crash_between_save_and_publish_leaves_stale_queued_report_that_is_findable(api, aws):
    """Simulates the process dying after the record is saved but before SQS publish: the record stays
    QUEUED and the recovery query (GSI, no scan) finds it."""
    from repositories import report_repository as repo

    repo.create("user-a", "r-stale", None, "2020-01-01T00:00:00.000Z")
    stale = repo.list_by_status("QUEUED", "2021-01-01T00:00:00.000Z")
    assert [s["reportId"] for s in stale] == ["r-stale"]
    # re-enqueue (what scripts/requeue_stale_reports.py does) and process normally
    repo.publish_job("user-a", "r-stale", None)
    run_worker(aws)
    assert api.get_report("r-stale")["json"]["status"] == "COMPLETED"
    assert repo.list_by_status("QUEUED", "2099-01-01T00:00:00.000Z") == []


def test_poison_message_is_returned_for_retry(aws):
    from handlers import report_worker

    out = report_worker.lambda_handler({"Records": [{"messageId": "m1", "body": "not json"}]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_logs_are_structured_json_with_correlation_ids(api, aws):
    import logging

    from common.logging_utils import JsonFormatter, get_logger

    lines = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(JsonFormatter().format(record))

    h = Collect()
    get_logger().addHandler(h)
    try:
        rid = api.create_report()["json"]["reportId"]
    finally:
        get_logger().removeHandler(h)
    parsed = [json.loads(line) for line in lines]
    assert any(p.get("reportId") == rid and p.get("requestId") == "req-123" for p in parsed)
    assert "Bearer" not in "".join(lines) and "Authorization" not in "".join(lines)
