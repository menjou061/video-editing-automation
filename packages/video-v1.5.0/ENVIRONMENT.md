# Windows runtime environment contract

The v1.5.0 production worker and queue run on the Windows rendering machine.
This source ZIP is not a macOS or Linux deployment package; Linux is
unsupported. Deploy and configure the worker only in the Windows environment
that has access to the Windows Jianying installation, source materials, draft
root, and required services.

The `JY_MAC_DRAFT_ROOT` and `JY_MAC_REVIEW_STAGE` settings below describe the
separate human-review handoff: the first is the reviewer's Mac-side path and the
second is the corresponding Windows-accessible staging path. They do not move
the worker or production queue to macOS. A reviewed draft must still pass the
configured human-acceptance gate before distribution.

The release has no embedded credentials or host paths. A deployment may set:

| Variable | Purpose |
| --- | --- |
| `JY_PIPE_ROOT` | pipeline working root |
| `JY_WORK_ROOT` | task work directory |
| `JY_SKILL_ROOT` | deployed orchestrator/editor skill root |
| `JY_DRAFT_ROOT` | local Jianying drafts root |
| `JY_NAS_SHARE` | NAS share used by the distributor |
| `JY_NAS_USER` | NAS account name |
| `NAS_PASSWORD` | NAS password, process environment only |
| `JY_NAS_TEST_ROOT` | NAS test-output root |
| `JY_NAS_DISTRIBUTION_ROOT` | grouped distribution parent root |
| `JY_MAC_DRAFT_ROOT` | Mac review draft root |
| `JY_MAC_REVIEW_STAGE` | Windows-accessible review staging directory paired with the Mac mount; separate from final distribution |
| `JY_PRODUCT_IDENTITY_FILE` | Operator-owned JSON map of confirmed single-SKU source directories, with optional record exceptions; not packaged |
| `JY_MATERIAL_INDEX_FILE` | Optional reviewed material-index cache; when set, only approved same-SKU sources may enter the contract |
| `JY_EVAL_MODE` | `observe` until two real observations qualify; then `enforce` for new tasks |
| `JY_EVAL_OBSERVATIONS` | Hash-bound two-task observation receipt for enforcement |
| `LARK_BASE_TOKEN` | Feishu base token, process environment only |
| `LARK_TABLE_ID` | Feishu table id |

Missing credentials or deployment roots must stop the queue and yield a
bounded failure; do not add fallbacks containing real host values.

`JY_PRODUCT_IDENTITY_FILE` can bind one confirmed single-SKU source directory
for all tasks using it; `records` may override an exception:

```json
{"sources":{"<exact-UNC-from-table>":{"product":"<table-product>","category":"<category>","sku":"<sku>","material_dir":"<exact-UNC-from-table>","human_confirmed":true}},"records":{}}
```

The map stays on the production host. Missing or changed identity prevents
dispatch without writing a business failure to Feishu. The review staging
root and Mac root must refer to the same shared files through their respective
OS paths; successful staging is still `AWAITING_MAC_REVIEW`, not acceptance.
