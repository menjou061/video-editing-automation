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
| `JY_PIPE_ROOT` | deployed package/code root; Python tools and policies resolve from here |
| `JY_WORK_ROOT` | task work directory, shared by JyPoll, JyRun, and the Python worker |
| `JY_STATE_ROOT` | JyPoll/JyRun lock and processed-state directory |
| `JY_LOG_ROOT` | JyPoll/JyRun log directory |
| `JY_CONFIG_ROOT` | external machine configuration directory; defaults to `<JY_PIPE_ROOT>\config` |
| `JY_ENV_CONFIG_PATH` | optional external JSON profile with an `environment` object of `JY_*` and `LARK_*` settings |
| `JY_NAS_SECRET_FILE` | optional path to the Windows CurrentUser-DPAPI encrypted NAS password |
| `JY_SKILL_ROOT` | deployed orchestrator/editor skill root |
| `JY_DRAFT_ROOT` | local Jianying drafts root |
| `JY_DRAFT_QC_SCRIPT` | machine-supplied independent `draft_visual_qc.py`; missing helper blocks generation |
| `JY_VOICE_CATALOG_ROOT` | optional externally provisioned voice catalog; only licensed catalogs may be used |
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

Both scheduled entrypoints load the external runtime profile at process start.
JyPoll and JyRun must use the same Windows task identity that encrypted
`nas-password.dpapi`; the password is decrypted into that process only and is
never written to the ZIP or log. Do not put `NAS_PASSWORD` in `runtime.json`.
Keep external config ACLs restricted to the renderer service account and
operators. `JY_PIPE_ROOT` selects code; `JY_WORK_ROOT` selects task data, so
changing the data root does not redirect policy or tool lookup.

The package deliberately does not contain `draft_visual_qc.py` because its
source and redistribution permission have not been established. Until the
renderer supplies the independently maintained helper, package preflight must
report `DRAFT_VISUAL_QC_MISSING` and production generation remains blocked.

`JY_PRODUCT_IDENTITY_FILE` can bind one confirmed single-SKU source directory
for all tasks using it; `records` may override an exception:

```json
{"sources":{"<exact-UNC-from-table>":{"product":"<table-product>","category":"<category>","sku":"<sku>","material_dir":"<exact-UNC-from-table>","human_confirmed":true}},"records":{}}
```

The map stays on the production host. Missing or changed identity prevents
dispatch without writing a business failure to Feishu. The review staging
root and Mac root must refer to the same shared files through their respective
OS paths; successful staging is still `AWAITING_MAC_REVIEW`, not acceptance.
