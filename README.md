# shop

Empty starter project, set up for testing integrations with the
[FetchSandbox](https://fetchsandbox.com) MCP server.

## FetchSandbox MCP connectivity check

Every tool the `Fetchsandbox` MCP server exposes was called once from a
Claude Code session, and each one answered:

| Tool | Call made | Result |
| --- | --- | --- |
| `guide` | route "Stripe webhook delivered twice" | matched `stripe` / `accept_payment` / `webhook_retries` (0.95) |
| `list_workflows` | `spec_slug: stripe` | 29 workflows, 16 failure scenarios |
| `quickrun` | `stripe` `accept_payment` + `webhook_retries` | pass, 6/6 steps, returned sandbox + receipt |
| `run_workflow` | `customers_create` on that sandbox | pass, 2/2 steps |
| `verify_behavior` | `webhook_duplicate_side_effect` | async job; confirmed (buggy reference handler charged twice, fixed one charged once) |
| `get_job` | polled the `verify_behavior` job | `running` → `done` after ~75s |
| `validate_integration` | `providers: [stripe]`, then `inspect` state | session created, 19 probes listed, twin state read |
| `coach` | start a Stripe integration session | session opened, asked first discovery question |

Note: these calls only check that the MCP server is reachable and its tools work.
They do not test any application code, because this project has none yet.

## Layout

```
src/   # application code goes here
```
