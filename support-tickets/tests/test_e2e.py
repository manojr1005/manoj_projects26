"""End-to-end flow: ticket -> report request -> queue -> worker -> status -> CSV download.

Runs fully in-process against moto (no AWS needed):   pytest tests/test_e2e.py
Against a deployed stack:                              API_URL=... ID_TOKEN=... pytest -m live tests/test_e2e.py
"""
import csv
import io
import os
import time

import boto3
import pytest

from tests.conftest import sqs_messages, to_sqs_event


def test_e2e_in_process(api, aws):
    import requests  # presigned URL is fetched through the real HTTP stack (moto intercepts it)
    from handlers import report_worker

    t1 = api.create_ticket(title="Broken login", status="OPEN")["json"]
    api.create_ticket(title="Slow page", status="CLOSED")
    rid = api.create_report(body={"status": "OPEN"})["json"]["reportId"]
    assert api.get_report(rid)["json"]["status"] == "QUEUED"

    report_worker.lambda_handler(to_sqs_event(sqs_messages(aws["queue"])), None)

    status = api.get_report(rid)["json"]
    assert status["status"] == "COMPLETED"
    body = requests.get(status["downloadUrl"], timeout=10)
    assert body.status_code == 200
    rows = list(csv.DictReader(io.StringIO(body.text)))
    assert [r["ticketId"] for r in rows] == [t1["ticketId"]]


@pytest.mark.live
@pytest.mark.skipif(not (os.getenv("API_URL") and os.getenv("ID_TOKEN")), reason="needs API_URL and ID_TOKEN")
def test_e2e_live():
    import requests

    base, h = os.environ["API_URL"].rstrip("/"), {"Authorization": f"Bearer {os.environ['ID_TOKEN']}"}
    t = requests.post(f"{base}/tickets", json={"title": "e2e", "description": "live test"}, headers=h, timeout=15)
    assert t.status_code == 201
    r = requests.post(f"{base}/reports", json={"status": "OPEN"}, headers=h, timeout=15)
    assert r.status_code == 202
    rid = r.json()["reportId"]
    for _ in range(30):
        s = requests.get(f"{base}/reports/{rid}", headers=h, timeout=15).json()
        if s["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(2)
    assert s["status"] == "COMPLETED", s
    csv_text = requests.get(s["downloadUrl"], timeout=15).text
    assert t.json()["ticketId"] in csv_text
    assert requests.delete(f"{base}/tickets/{t.json()['ticketId']}", headers=h, timeout=15).status_code == 204
