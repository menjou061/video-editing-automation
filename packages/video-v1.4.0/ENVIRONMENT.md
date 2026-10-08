# Runtime environment contract

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
| `LARK_BASE_TOKEN` | Feishu base token, process environment only |
| `LARK_TABLE_ID` | Feishu table id |

Missing credentials or deployment roots must stop the queue and yield a
bounded failure; do not add fallbacks containing real host values.
