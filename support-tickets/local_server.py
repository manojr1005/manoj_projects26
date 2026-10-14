#!/usr/bin/env python3
"""
Local dev server — wraps the Lambda handlers behind a real HTTP server.
Uses moto to mock DynamoDB, SQS and S3, and runs the report worker in the background.

Usage:
    python local_server.py          # starts on http://localhost:8080
    python local_server.py --port 9090

All endpoints are open (no JWT required locally — user-id defaults to "local-user").
Pass  X-User-Id: <id>  header to simulate different users.

Stop with Ctrl-C.
"""
import argparse
import base64
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

# ── moto must be activated before importing our code ──────────────────────────
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TABLE_NAME", "local-table")
os.environ.setdefault("REPORT_BUCKET", "local-reports")
os.environ.setdefault("PRESIGN_TTL_SECONDS", "300")
os.environ.setdefault("MAX_RECEIVE_COUNT", "3")

import boto3
from moto import mock_aws

# patch() keeps moto active for the lifetime of the process
_mock = mock_aws()
_mock.start()

# ── create AWS resources ───────────────────────────────────────────────────────
_ddb = boto3.client("dynamodb", region_name="us-east-1")
_ddb.create_table(
    TableName="local-table",
    BillingMode="PAY_PER_REQUEST",
    AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk", "gsi1pk", "gsi1sk")],
    KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
    GlobalSecondaryIndexes=[{
        "IndexName": "GSI1",
        "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"}, {"AttributeName": "gsi1sk", "KeyType": "RANGE"}],
        "Projection": {"ProjectionType": "ALL"},
    }],
)
_sqs = boto3.client("sqs", region_name="us-east-1")
_queue_url = _sqs.create_queue(QueueName="local-reports")["QueueUrl"]
os.environ["REPORT_QUEUE_URL"] = _queue_url
boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="local-reports")

# ── import our app code now that env vars are set ─────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from common import clients
clients.clear_caches()

from handlers import report_worker, reports, tickets

# ── background worker: polls SQS every 2 s ────────────────────────────────────
def _worker_loop():
    while True:
        try:
            msgs = _sqs.receive_message(
                QueueUrl=_queue_url, MaxNumberOfMessages=5, WaitTimeSeconds=1,
                AttributeNames=["ApproximateReceiveCount"],
            ).get("Messages", [])
            if msgs:
                event = {"Records": [
                    {"messageId": m["MessageId"], "body": m["Body"],
                     "attributes": {"ApproximateReceiveCount": m["Attributes"].get("ApproximateReceiveCount", "1")}}
                    for m in msgs
                ]}
                result = report_worker.lambda_handler(event, None)
                # delete successfully processed messages
                ok = {f["itemIdentifier"] for f in result.get("batchItemFailures", [])}
                for m in msgs:
                    if m["MessageId"] not in ok:
                        _sqs.delete_message(QueueUrl=_queue_url, ReceiptHandle=m["ReceiptHandle"])
        except Exception as e:
            print(f"[worker] error: {e}", flush=True)
        time.sleep(1)

# ── HTTP→Lambda bridge ────────────────────────────────────────────────────────
ROUTES = {
    ("POST",   "/tickets"):              (tickets,  "POST /tickets"),
    ("GET",    "/tickets"):              (tickets,  "GET /tickets"),
    ("GET",    "/tickets/{ticketId}"):   (tickets,  "GET /tickets/{ticketId}"),
    ("PATCH",  "/tickets/{ticketId}"):   (tickets,  "PATCH /tickets/{ticketId}"),
    ("DELETE", "/tickets/{ticketId}"):   (tickets,  "DELETE /tickets/{ticketId}"),
    ("POST",   "/reports"):              (reports,  "POST /reports"),
    ("GET",    "/reports/{reportId}"):   (reports,  "GET /reports/{reportId}"),
}


def _match(method, path):
    """Return (handler_module, routeKey, path_params) or None."""
    for (m, template), (mod, route_key) in ROUTES.items():
        if m != method:
            continue
        parts_t = template.split("/")
        parts_p = path.split("/")
        if len(parts_t) != len(parts_p):
            continue
        params = {}
        ok = True
        for t, p in zip(parts_t, parts_p):
            if t.startswith("{") and t.endswith("}"):
                params[t[1:-1]] = p
            elif t != p:
                ok = False
                break
        if ok:
            return mod, route_key, params
    return None


def _build_event(method, path, headers, body_bytes, query_string, user_id, route_key, path_params):
    qs = {}
    if query_string:
        for part in query_string.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                qs[k] = v
    event = {
        "routeKey": route_key,
        "requestContext": {
            "requestId": str(uuid.uuid4())[:8],
            "authorizer": {"jwt": {"claims": {"sub": user_id}}},
        },
        "pathParameters": path_params or None,
        "queryStringParameters": qs or None,
        "isBase64Encoded": False,
    }
    if body_bytes:
        event["body"] = body_bytes.decode("utf-8", errors="replace")
    return event


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path} → {args[1] if len(args) > 1 else ''}", flush=True)

    def _handle(self):
        parsed = self.path.split("?", 1)
        path, qs = parsed[0], parsed[1] if len(parsed) > 1 else ""
        headers = {k.lower(): v for k, v in self.headers.items()}
        length = int(headers.get("content-length", 0))
        body = self.rfile.read(length) if length else b""
        user_id = headers.get("x-user-id", "local-user")

        match = _match(self.command, path)
        if not match:
            self._send(404, {"error": {"code": "NOT_FOUND", "message": f"No route: {self.command} {path}"}})
            return

        mod, route_key, path_params = match
        event = _build_event(self.command, path, headers, body, qs, user_id, route_key, path_params)
        result = mod.lambda_handler(event, None)
        status = result.get("statusCode", 200)
        resp_body = result.get("body", "")
        content_type = result.get("headers", {}).get("Content-Type", "application/json")
        self._send_raw(status, resp_body, content_type)

    def _send(self, status, data):
        self._send_raw(status, json.dumps(data), "application/json")

    def _send_raw(self, status, body, content_type):
        encoded = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):    self._handle()
    def do_POST(self):   self._handle()
    def do_PATCH(self):  self._handle()
    def do_DELETE(self): self._handle()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    threading.Thread(target=_worker_loop, daemon=True).start()
    print("[worker] background report worker started", flush=True)

    print(f"""
╔══════════════════════════════════════════════════════════╗
║  Support Tickets — local dev server                      ║
║  http://localhost:{args.port:<5}                                   ║
║                                                          ║
║  All requests default to user-id: local-user             ║
║  Pass  X-User-Id: alice  to simulate different users     ║
║                                                          ║
║  Endpoints:                                              ║
║    POST   /tickets                                       ║
║    GET    /tickets?status=OPEN&limit=5                   ║
║    GET    /tickets/{{ticketId}}                            ║
║    PATCH  /tickets/{{ticketId}}                            ║
║    DELETE /tickets/{{ticketId}}                            ║
║    POST   /reports          (body: {{"status":"OPEN"}})    ║
║    GET    /reports/{{reportId}}                            ║
║                                                          ║
║  Report worker runs in the background automatically.     ║
║  Stop with Ctrl-C                                        ║
╚══════════════════════════════════════════════════════════╝
""", flush=True)
    server = HTTPServer(("", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] stopped")
