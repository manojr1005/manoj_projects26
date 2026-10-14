"""Configuration, read from environment variables only (set by template.yaml)."""
import os


def table_name() -> str:
    return os.environ["TABLE_NAME"]


def queue_url() -> str:
    return os.environ["REPORT_QUEUE_URL"]


def bucket_name() -> str:
    return os.environ["REPORT_BUCKET"]


def presign_ttl_seconds() -> int:
    return int(os.environ.get("PRESIGN_TTL_SECONDS", "300"))


def max_receive_count() -> int:
    """Must match the redrive policy maxReceiveCount of the report queue."""
    return int(os.environ.get("MAX_RECEIVE_COUNT", "3"))
