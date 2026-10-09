# Windows process sketch

Phase 1 does not install a Windows service.

The process to start later is:

```text
uv run athena-orchestrator
```

Suggested service name, when you install it: `AthenaOrchestrator`.

IB Gateway stays a separate process. This sketch does not start Gateway and does not register a service.
