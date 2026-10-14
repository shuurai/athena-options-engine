# Security

## Reporting a vulnerability

Please open a private GitHub security advisory on this repository, or email the
maintainers if advisories are not enabled yet. Do not file a public issue for
active vulnerabilities.

Include steps to reproduce, affected versions, and impact. We will acknowledge
receipt and work on a fix.

## Secrets and trading credentials

Never commit:

- `.env` or any file with live tokens
- Interactive Brokers account ids that are not fixtures
- DeepSeek, iTick, Telegram, Feishu, or cloud credentials
- Gateway passwords or signing secrets

Copy [`.env.example`](.env.example) to `.env` locally and fill values there.
Athena loads secrets from the environment and account Settings — not from the
repository.

## Trading risk

Athena can send real broker orders when configured for paper or live Interactive
Brokers sessions. Review risk limits, privilege, and Approve gates before
enabling trading. Past performance of any strategy docs is not a guarantee of
results.
