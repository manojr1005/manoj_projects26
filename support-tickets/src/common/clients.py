"""Lazily created AWS clients (cached per container; tests clear the cache)."""
from functools import lru_cache

import boto3
from botocore.config import Config

from common import config

_CFG = Config(retries={"max_attempts": 3, "mode": "standard"})


@lru_cache(maxsize=None)
def table():
    return boto3.resource("dynamodb", config=_CFG).Table(config.table_name())


@lru_cache(maxsize=None)
def sqs():
    return boto3.client("sqs", config=_CFG)


@lru_cache(maxsize=None)
def s3():
    # SigV4 + regional endpoint so presigned URLs work in every region.
    return boto3.client("s3", config=Config(signature_version="s3v4", retries=_CFG.retries))


def clear_caches():
    table.cache_clear()
    sqs.cache_clear()
    s3.cache_clear()
