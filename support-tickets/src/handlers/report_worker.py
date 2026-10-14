"""SQS-triggered worker. Uses partial batch responses so one bad message doesn't retry the batch."""
import json

from common.logging_utils import get_logger
from services import report_service

log = get_logger()


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            job = json.loads(record["body"])
            receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
            report_service.process_job(job, receive_count)
        except Exception:
            log.error("job failed, returning to queue", extra={"messageId": message_id})
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
