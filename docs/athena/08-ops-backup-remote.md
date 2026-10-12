# 08 — Ops, Backup, Remote Management

## Goal

The Windows trading box must stay usable when you are traveling: health visibility, restart IB Gateway / Athena services, and durable backups (local first, then S3-compatible blob).

## Remote ops agent

`apps/ops-agent` runs on the Windows box with a **tight allowlist** of actions, invoked only via Feishu/Telegram ops commands (see [05-human-control.md](05-human-control.md)).

| Action | Effect |
|--------|--------|
| `health` | IB connected?, orchestrator heartbeat, disk, last backup |
| `gateway_restart` | Restart IB Gateway service / process |
| `athena_restart` | Restart orchestrator (and dependents if needed) |
| `bots_restart` | Restart messaging workers |
| `backup_now` | Run backup pipeline |
| `logs_tail` | Last N lines of key logs (size-capped) |

**Not allowed remotely:** arbitrary shell, reading secrets files, changing firewall, disabling risk.

Pattern inspiration: DSH/claw-style “restart when I’m away” — keep the agent small and audited.

## Process supervision (Windows)

Prefer:

- Windows Service or NSSM/WinSW for Gateway, Postgres, orchestrator, bots, ops-agent  
- Or Docker Compose for DB/redis only; Gateway often better native on host  

Health: ops-agent probes localhost ports / heartbeats written by orchestrator every loop.

## Backup strategy

### 1. Local first (required)

| What | How |
|------|-----|
| Postgres dump or volume snapshot | Scheduled + on-demand |
| Settings versions + audit export | JSON/SQL included in dump |
| Config / `.env` | Encrypted archive separate from DB dump |
| Logs (optional rotate) | Not full SoR |

Destination on box: e.g. `D:\athena\backups\` with retention from Settings (`retain_local_days`).

### 2. Offsite: S3-compatible blob (MinIO local → remote later)

**MVP:** run **MinIO** on the Windows box (Docker or native). Same S3 API client as future AWS/R2 — only env changes.

After each successful local backup:

- Upload artifacts to the configured bucket/prefix  
- Credentials via env only; never commit  

#### Env variables (S3-compatible)

```bash
# Toggle
ATHENA_S3_SYNC_ENABLED=true

# Endpoint: local MinIO now; replace host later for real S3/R2 without code changes
ATHENA_S3_ENDPOINT=http://127.0.0.1:9100
ATHENA_S3_REGION=us-east-1
ATHENA_S3_BUCKET=athena-backups
ATHENA_S3_PREFIX=athena/windows-box/
ATHENA_S3_ACCESS_KEY_ID=minioadmin
ATHENA_S3_SECRET_ACCESS_KEY=minioadmin
# path-style required for MinIO; keep true until using virtual-hosted AWS
ATHENA_S3_FORCE_PATH_STYLE=true
# set true when endpoint is https with private CA, etc.
ATHENA_S3_TLS_VERIFY=true
```

When you provide a real remote endpoint later, change `ATHENA_S3_ENDPOINT` / keys / `ATHENA_S3_FORCE_PATH_STYLE` (often `false` on AWS). Client code must use the AWS SDK (or `boto3`/`minio` SDK) against these vars only.

#### Local MinIO sketch (`deploy/windows`)

- Container: `minio/minio server /data`, S3 API on host port 9100. The console stays inside the container and is not published on the host.  
- Create bucket `athena-backups` once  
- Persist MinIO data volume on the same disk as local backups if possible  

### 3. Verify

- Periodic restore test to a scratch DB (documented in ops checklist)  
- Alert if local backup missed or S3/MinIO sync failed  

## Connectivity for remote control

Recommended: **Tailscale** (or WireGuard) so:

- Bots on box stay outbound to Feishu/Telegram  
- control-web on Vercel calls private webhook on box  
- You can SSH for emergencies without exposing IB ports  

Do **not** publish IB Gateway ports to the public internet.

## Console buttons

The cockpit runs three workflows. Each one shows a floating task card, then a toast.

- **Health** checks Orchestrator, iTick, Store, the latest local backup, and Telegram.
- **Backup now** writes a local snapshot, prunes by `retain_local_days`, and syncs to S3 when that sync is on.
- **Halt trading** blocks new drafts on this account. **Resume** clears that halt. Privilege stays `all_orders`.

Disk, IB login, and a log tail are not part of that card.

## Alerting (ops)

CRITICAL → both Feishu and Telegram:

- Gateway down inside Athena slot > threshold  
- Backup failed  
- Disk low  
- Reject streak / daily loss halt  

## Disaster sketch

1. New Windows machine or rebuild  
2. Install Gateway + runtime deps  
3. Restore latest local or S3 backup  
4. Restore secrets  
5. `/ops` health → paper mode verify → live privilege only when ready  

## Open items

- [ ] Real remote S3/R2 endpoint (swap env; MinIO stays for local/dev)  
- Backup encryption passphrase storage  
- Whether Gateway runs as Docker or native installer on the 5090 box  
