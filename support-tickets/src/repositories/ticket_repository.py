"""DynamoDB access for tickets. No Scan is used anywhere."""
import base64
import json
from typing import Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from common import clients
from common.errors import NotFoundError, ValidationError
from repositories import keys

PUBLIC_FIELDS = (
    "ticketId", "title", "description", "priority", "status", "createdBy", "createdAt", "updatedAt",
)


def _public(item: dict) -> dict:
    return {k: item[k] for k in PUBLIC_FIELDS if k in item}


def encode_token(last_key: Optional[dict]) -> Optional[str]:
    if not last_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def decode_token(token: str, user_id: str) -> dict:
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode()))
        if not isinstance(key, dict) or key.get("pk") != keys.user_pk(user_id):
            raise ValueError
        if "gsi1pk" in key and not str(key["gsi1pk"]).startswith(keys.user_pk(user_id) + "#"):
            raise ValueError
        return key
    except (ValueError, TypeError):
        raise ValidationError("Invalid nextToken")


def put(ticket: dict, user_id: str) -> None:
    item = dict(ticket)
    item["pk"] = keys.user_pk(user_id)
    item["sk"] = keys.ticket_sk(ticket["ticketId"])
    item["gsi1pk"] = keys.ticket_status_gsi_pk(user_id, ticket["status"])
    item["gsi1sk"] = item["sk"]
    clients.table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")


def get(user_id: str, ticket_id: str) -> dict:
    res = clients.table().get_item(
        Key={"pk": keys.user_pk(user_id), "sk": keys.ticket_sk(ticket_id)}
    )
    item = res.get("Item")
    if not item:
        raise NotFoundError(f"Ticket {ticket_id} not found")
    return _public(item)


def update(user_id: str, ticket_id: str, fields: dict, updated_at: str) -> dict:
    names, values, sets = {}, {":updatedAt": updated_at}, ["#updatedAt = :updatedAt"]
    names["#updatedAt"] = "updatedAt"
    for i, (name, value) in enumerate(fields.items()):
        names[f"#f{i}"] = name
        values[f":v{i}"] = value
        sets.append(f"#f{i} = :v{i}")
    if "status" in fields:  # keep the status index in sync
        names["#g"] = "gsi1pk"
        values[":g"] = keys.ticket_status_gsi_pk(user_id, fields["status"])
        sets.append("#g = :g")
    try:
        res = clients.table().update_item(
            Key={"pk": keys.user_pk(user_id), "sk": keys.ticket_sk(ticket_id)},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise NotFoundError(f"Ticket {ticket_id} not found")
        raise
    return _public(res["Attributes"])


def delete(user_id: str, ticket_id: str) -> None:
    try:
        clients.table().delete_item(
            Key={"pk": keys.user_pk(user_id), "sk": keys.ticket_sk(ticket_id)},
            ConditionExpression="attribute_exists(pk)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise NotFoundError(f"Ticket {ticket_id} not found")
        raise


def list_page(user_id: str, status: Optional[str], limit: int, token: Optional[str]):
    """One page of tickets, newest first. Returns (tickets, next_token)."""
    kwargs = {"Limit": limit, "ScanIndexForward": False}
    if status:
        kwargs["IndexName"] = "GSI1"
        kwargs["KeyConditionExpression"] = Key("gsi1pk").eq(
            keys.ticket_status_gsi_pk(user_id, status)
        )
    else:
        kwargs["KeyConditionExpression"] = Key("pk").eq(keys.user_pk(user_id)) & Key("sk").begins_with(
            "TICKET#"
        )
    if token:
        kwargs["ExclusiveStartKey"] = decode_token(token, user_id)
    res = clients.table().query(**kwargs)
    return [_public(i) for i in res["Items"]], encode_token(res.get("LastEvaluatedKey"))


def iter_all(user_id: str, status: Optional[str]):
    """Yield every ticket of a user (used by the report worker), paging through Query."""
    token = None
    while True:
        items, token = list_page(user_id, status, 100, token)
        yield from items
        if not token:
            return
