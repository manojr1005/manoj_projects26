# DynamoDB design

Single table `AppTable` (on-demand billing, PITR on, encrypted). Attributes `pk`, `sk` (table key) and `gsi1pk`, `gsi1sk` (GSI1, `ALL` projection).

| Entity | pk | sk | gsi1pk | gsi1sk |
|---|---|---|---|---|
| Ticket | `USER#<sub>` | `TICKET#<ticketId>` | `USER#<sub>#STATUS#<status>` | `TICKET#<ticketId>` |
| Report | `USER#<sub>` | `REPORT#<reportId>` | `REPORT_STATUS#<QUEUED\|PROCESSING\|FAILED>` (removed when COMPLETED) | `<updated-at ISO>` |

`<sub>` is the Cognito `sub` claim from the validated JWT. `ticketId` = 12 hex chars of millisecond timestamp + 12 random hex chars, so it sorts by creation time.

## Access patterns

| # | Pattern | Operation |
|---|---|---|
| 1 | Create ticket | `PutItem` (`attribute_not_exists(pk)`) |
| 2 | Get ticket (own) | `GetItem pk=USER#sub, sk=TICKET#id` |
| 3 | List my tickets, newest first, paginated | `Query pk=USER#sub AND begins_with(sk,'TICKET#')`, `ScanIndexForward=false`, `Limit`, `ExclusiveStartKey` |
| 4 | List my tickets by status | `Query GSI1 gsi1pk=USER#sub#STATUS#<s>`, same paging |
| 5 | Update / delete ticket | `UpdateItem`/`DeleteItem` with `attribute_exists(pk)` (-> 404); a status change also rewrites `gsi1pk` |
| 6 | Create / get report (own) | `PutItem` / `GetItem pk=USER#sub, sk=REPORT#id` |
| 7 | Worker: all tickets of a user (+ status) | same queries as 3/4, paged until exhausted |
| 8 | Operations: stale reports | `Query GSI1 gsi1pk=REPORT_STATUS#QUEUED AND gsi1sk < cutoff` |

No pattern uses `Scan` (the only Scan is in tests, to count rows).

## Why this design
* **Isolation by construction**: every key contains the caller's `sub`, so another user's `ticketId`/`reportId` is simply "not found" (404, which also avoids revealing that it exists).
* `nextToken` is the base64 `LastEvaluatedKey`; it is rejected (400) unless its keys belong to the caller.
* The status GSI is partitioned per user+status, so there are no hot partitions. Report-status partitions are small because COMPLETED reports leave the index.
* Pagination caveat: DynamoDB can return a `nextToken` when the last page is exactly `limit` items; the next page is then empty.
