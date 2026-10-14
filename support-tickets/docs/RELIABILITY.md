# Reliability, failure handling and recovery

## Flow and states
`POST /reports` -> record `QUEUED` -> SQS -> worker sets `PROCESSING` -> CSV in S3 -> `COMPLETED`; on failure `FAILED`.

## Duplicate delivery (SQS is at-least-once)
* `reportId` is the idempotency key; one report record per id, one S3 object at the deterministic key `reports/<sub>/<reportId>.csv`.
* The worker skips jobs whose report is `COMPLETED`. If two deliveries race, the S3 write overwrites the same key with equivalent content and every status write carries `status <> COMPLETED`, so a late duplicate can never clobber a finished report (test: `test_duplicate_delivery_is_idempotent`, `test_concurrent_duplicate_cannot_overwrite_completed`).
* A client that calls `POST /reports` twice gets two reports by design (no client idempotency key; see limitations).

## Retries and DLQ
* The Lambda event source mapping uses `ReportBatchItemFailures`: only failed messages return to the queue.
* Queue visibility timeout 180 s (6x the 30 s worker timeout); `maxReceiveCount` = 3 (parameter), then the message moves to the DLQ (14-day retention). A CloudWatch alarm fires when the DLQ is non-empty.
* On the final attempt (`ApproximateReceiveCount >= MAX_RECEIVE_COUNT`) the worker writes `FAILED` + a short error type (no internals) and re-raises so the message reaches the DLQ. Earlier attempts leave the report `PROCESSING`.

## Failure between "save record" and "publish to SQS"
The API writes the record first, then publishes (a transactional outbox would need DynamoDB Streams; see improvements).
1. Publish raises (SQS outage, throttling): the API marks the report `FAILED` ("Failed to enqueue") and returns **503**. The client simply requests a new report.
2. The process dies/times out between the two steps (or the `FAILED` write also fails): the record stays `QUEUED` with no message. These are discoverable via GSI1 and fixed by
   `python scripts/requeue_stale_reports.py --minutes 15` (re-publishes; idempotent worker makes this safe). It could be run from a scheduled Lambda; left manual to keep scope small.

## Investigating and recovering
| Symptom | Investigate | Recover |
|---|---|---|
| Messages in DLQ / alarm | CloudWatch Logs Insights on the worker log group: `fields @timestamp, reportId, message \| filter level="ERROR"`; read the DLQ message (`aws sqs receive-message --queue-url <dlq>`) | Fix the cause, then redrive: `aws sqs start-message-move-task --source-arn <dlq-arn> --destination-arn <queue-arn>`. The worker accepts `FAILED` reports, so the same report completes. |
| Report stuck `QUEUED` | Is there a message? (queue attributes); stale query above | `requeue_stale_reports.py` |
| Report stuck `PROCESSING` (worker timed out/crashed) | SQS redelivers automatically after the visibility timeout; if retries are exhausted it is in the DLQ | redrive DLQ or `requeue_stale_reports.py --minutes 15` |
| Customer-visible `FAILED` | `errorMessage` + `reportId` in logs | `requeue_stale_reports.py --include-failed` or customer re-requests |

Correlate with `requestId` (API Gateway request id, also returned as `X-Request-Id` and in every error body) and `reportId` in the structured logs.

## Which ticket state does a report contain?
**Processing time.** The worker queries tickets when it runs, not when the report was requested (typically seconds apart, but retries/redrives can widen the gap). Trade-off: no need to snapshot tickets in the job message (SQS 256 KB limit) or store a copy. A point-in-time report would require snapshotting at request time. The report record stores `createdAt` and `completedAt`, so consumers can see the window.
