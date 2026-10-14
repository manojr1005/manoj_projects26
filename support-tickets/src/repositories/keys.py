"""DynamoDB key builders - the single place that knows the key schema (see docs/DYNAMODB.md)."""


def user_pk(user_id: str) -> str:
    return f"USER#{user_id}"


def ticket_sk(ticket_id: str) -> str:
    return f"TICKET#{ticket_id}"


def report_sk(report_id: str) -> str:
    return f"REPORT#{report_id}"


def ticket_status_gsi_pk(user_id: str, status: str) -> str:
    return f"USER#{user_id}#STATUS#{status}"


def report_status_gsi_pk(status: str) -> str:
    return f"REPORT_STATUS#{status}"
