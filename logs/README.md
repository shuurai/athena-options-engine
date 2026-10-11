# logs/ — development change ledger conventions

This directory is the **development / engineering change audit**, not a task list and not the
assistant's working memory. Keep the three separate.

## Structure

```
CHANGELOG.md                  # index: one row per change (date | <=20 words | link)
logs/
  README.md                   # this file: conventions + template
  YYYY-MM-DD.md               # detail: one file per day, several entries inside
scripts/check-changelog.mjs   # machine check (index <-> log closure)
```

One file per day; several entries per day go in the same file.

## Two rules that matter

1. **An entry heading holds only the ID.** `## C-20261010-1`, with the human title on the next line.
   If the heading contains Chinese text, the rendered anchor slug changes with the renderer and every
   link from `CHANGELOG.md` breaks. An ASCII ID anchor is stable forever.
2. **Word count** = one CJK character counts 1, one ASCII *token* counts 1, punctuation and
   whitespace count 0. A token is a maximal `[A-Za-z0-9]+` run, so `archiveTenant` is one word and
   `Add review doc` is three. (Count tokens **before** stripping whitespace — stripping first fuses a
   whole English sentence into a single word and silently disables the 20-word limit. That bug was
   found and fixed in `C-20261010-2`.)

## Entry template

```markdown
## C-YYYYMMDD-N

**One-line title**

| 项 | 内容 |
|---|---|
| 状态 | ✅ 完成并验证 / 🚧 进行中 / ⛔ 已回滚 / 🔁 修正 C-YYYYMMDD-N |
| 范围 | files touched (exact names; nothing else) |
| 动机 | why (request id / defect) |
| 变更 | ① … ② … ③ … |
| 影响 | interfaces / data / compatibility; and what was **not** changed |
| 验证 | command + observed result (not "should be fine") |
| 回滚 | how to revert |
| 关联 | commit hash / doc section / migration |
```

Hard rules:

- Entries are **append-only, never rewritten**. If something was wrong, add a new entry with
  `状态: 🔁 修正 C-…` and keep the history.
- Same-day sequence numbers restart at 1 each day.
- A `|` inside a table cell must be escaped as `\|`.
- After any change: run `node scripts/check-changelog.mjs`.
