import json
import os

import boto3
import pytest
from moto import mock_aws

REGION = "us-east-1"
BUCKET = "test-report-bucket"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION, "TABLE_NAME": "app-table", "REPORT_BUCKET": BUCKET,
        "PRESIGN_TTL_SECONDS": "300", "MAX_RECEIVE_COUNT": "3",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def aws(aws_env, monkeypatch):
    from common import clients

    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="app-table", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk", "gsi1pk", "gsi1sk")],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"}, {"AttributeName": "gsi1sk", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        sqs = boto3.client("sqs")
        dlq = sqs.create_queue(QueueName="dlq")["QueueUrl"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        q = sqs.create_queue(
            QueueName="reports",
            Attributes={"RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"})},
        )["QueueUrl"]
        monkeypatch.setenv("REPORT_QUEUE_URL", q)
        boto3.client("s3").create_bucket(Bucket=BUCKET)
        clients.clear_caches()
        yield {"queue": q, "dlq": dlq}
        clients.clear_caches()


def event(method, path, *, user="user-a", body=None, path_params=None, query=None, route=None):
    """Build an API Gateway HTTP API v2 event as the JWT authorizer would deliver it."""
    ev = {
        "routeKey": route or f"{method} {path}",
        "requestContext": {
            "requestId": "req-123",
            "authorizer": {"jwt": {"claims": {"sub": user}}} if user else {},
        },
        "pathParameters": path_params,
        "queryStringParameters": query,
    }
    if body is not None:
        ev["body"] = body if isinstance(body, str) else json.dumps(body)
    return ev


def call(handler, *args, **kw):
    res = handler.lambda_handler(event(*args, **kw), None)
    res["json"] = json.loads(res["body"]) if res["body"] else None
    return res


@pytest.fixture
def api(aws):
    from handlers import reports, tickets

    class Api:
        def create_ticket(self, user="user-a", **body):
            body.setdefault("title", "Printer broken")
            body.setdefault("description", "Paper jam")
            return call(tickets, "POST", "/tickets", user=user, body=body)

        def get_ticket(self, tid, user="user-a"):
            return call(tickets, "GET", "/tickets/{ticketId}", user=user, path_params={"ticketId": tid})

        def patch_ticket(self, tid, body, user="user-a"):
            return call(tickets, "PATCH", "/tickets/{ticketId}", user=user, body=body, path_params={"ticketId": tid})

        def delete_ticket(self, tid, user="user-a"):
            return call(tickets, "DELETE", "/tickets/{ticketId}", user=user, path_params={"ticketId": tid})

        def list_tickets(self, user="user-a", **query):
            return call(tickets, "GET", "/tickets", user=user, query=query or None)

        def create_report(self, user="user-a", body=None):
            return call(reports, "POST", "/reports", user=user, body=body)

        def get_report(self, rid, user="user-a"):
            return call(reports, "GET", "/reports/{reportId}", user=user, path_params={"reportId": rid})

    return Api()


def sqs_messages(url, n=10):
    res = boto3.client("sqs").receive_message(QueueUrl=url, MaxNumberOfMessages=n, WaitTimeSeconds=0,
                                              AttributeNames=["ApproximateReceiveCount"])
    return res.get("Messages", [])


def to_sqs_event(messages, receive_count=1):
    return {"Records": [
        {"messageId": m["MessageId"], "body": m["Body"],
         "attributes": {"ApproximateReceiveCount": str(receive_count)}} for m in messages
    ]}
