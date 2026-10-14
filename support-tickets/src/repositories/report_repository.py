"""DynamoDB, SQS and S3 access for reports."""
import json
from typing import Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from common import clients, config
from common.errors import NotFoundError
from repositories import keys

PUBLIC_FIELDS = (
    "reportId", "status", "statusFilter", "createdAt", "updatedAt", "completedAt",
    "ticketCount", "errorMessage",
)
# Reports that are still in flight/failed are indexed (GSI1) so a recovery job can find
# stale ones without a Scan. COMPLETED reports are removed from the index.
_INDEXED = {"QUEUED", "PROCESSING", "FAILED"}


def _public(item: dict) -> dict:
    return {k: item[k] for k in PUBLIC_FIELDS if k in item}


def s3_key(user_id: str, report_id: str) -> str:
    return f"reports/{user_id}/{report_id}.csv"  # deterministic -> idempotent writes


def create(user_id: str, report_id: str, status_filter: Optional[str], now: str) -> dict:
    item = {
        "pk": keys.user_pk(user_id),
        "sk": keys.report_sk(report_id),
        "gsi1pk": keys.report_status_gsi_pk("QUEUED"),
        "gsi1sk": now,
        "reportId": report_id,
        "userId": user_id,
        "status": "QUEUED",
        "createdAt": now,
        "updatedAt": now,
    }
    if status_filter:
        item["statusFilter"] = status_filter
    clients.table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    return item


def get(user_id: str, report_id: str) -> dict:
    """Raw item. Scoped by the user's partition key, so another user's id simply isn't found."""
    res = clients.table().get_item(
        Key={"pk": keys.user_pk(user_id), "sk": keys.report_sk(report_id)}
    )
    if "Item" not in res:
        raise NotFoundError(f"Report {report_id} not found")
    return res["Item"]


def public(item: dict) -> dict:
    return _public(item)


def set_status(user_id: str, report_id: str, status: str, now: str, *, extra: Optional[dict] = None,
               not_completed: bool = True) -> bool:
    """Move a report to `status`. Never overwrites COMPLETED. Returns False if it was COMPLETED."""
    extra = extra or {}
    names = {"#s": "status", "#u": "updatedAt", "#g": "gsi1pk", "#gs": "gsi1sk"}
    values = {":s": status, ":u": now}
    sets = ["#s = :s", "#u = :u"]
    removes = []
    if status in _INDEXED:
        values[":g"] = keys.report_status_gsi_pk(status)
        values[":gs"] = now
        sets += ["#g = :g", "#gs = :gs"]
    else:
        removes += ["#g", "#gs"]
    for i, (k, v) in enumerate(extra.items()):
        names[f"#e{i}"] = k
        values[f":e{i}"] = v
        sets.append(f"#e{i} = :e{i}")
    if status != "FAILED":
        names["#err"] = "errorMessage"
        removes.append("#err")
    expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    try:
        clients.table().update_item(
            Key={"pk": keys.user_pk(user_id), "sk": keys.report_sk(report_id)},
            UpdateExpression=expr,
            ConditionExpression="attribute_exists(pk) AND #s <> :done",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={**values, ":done": "COMPLETED"},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def list_by_status(status: str, created_before: str):
    """Recovery helper: reports in `status` older than `created_before` (uses GSI1, no Scan)."""
    res = clients.table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("gsi1pk").eq(keys.report_status_gsi_pk(status))
        & Key("gsi1sk").lt(created_before),
    )
    return res["Items"]


def publish_job(user_id: str, report_id: str, status_filter: Optional[str]) -> None:
    body = {"reportId": report_id, "userId": user_id, "statusFilter": status_filter}
    clients.sqs().send_message(QueueUrl=config.queue_url(), MessageBody=json.dumps(body))


def put_csv(user_id: str, report_id: str, data: bytes) -> str:
    key = s3_key(user_id, report_id)
    clients.s3().put_object(
        Bucket=config.bucket_name(), Key=key, Body=data, ContentType="text/csv",
        ServerSideEncryption="AES256",
    )
    return key


def presign(user_id: str, report_id: str) -> str:
    return clients.s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": config.bucket_name(),
            "Key": s3_key(user_id, report_id),
            "ResponseContentDisposition": f'attachment; filename="tickets-{report_id}.csv"',
        },
        ExpiresIn=config.presign_ttl_seconds(),
    )
