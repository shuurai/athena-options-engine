#!/usr/bin/env node
// scripts/check-changelog.mjs  ——  run: node scripts/check-changelog.mjs
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

const ROOT = process.cwd(), LOGS_DIR = join(ROOT, "logs"), CHANGELOG = join(ROOT, "CHANGELOG.md");
const MAX_WORDS = 20;
const problems = [];
const fail = (m) => problems.push(m);

// One CJK character = 1, one contiguous ASCII run = 1, punctuation and whitespace = 0.
// NB: do NOT strip whitespace before matching ASCII runs — that would fuse a whole
// English sentence into a single "word" and defeat the limit for space-separated text.
function wordCount(s) {
  const ascii = (s.match(/[A-Za-z0-9]+/g) || []).length;
  const cjk = (s.match(/[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]/gu) || []).length;
  return ascii + cjk;
}

const logFiles = readdirSync(LOGS_DIR).filter((f) => /^\d{4}-\d{2}-\d{2}\.md$/.test(f)).sort();
const entries = new Map();

for (const file of logFiles) {
  const yyyymmdd = file.replace(/\.md$/, "").replace(/-/g, "");
  const ns = [];
  readFileSync(join(LOGS_DIR, file), "utf8").split("\n").forEach((line, i) => {
    const h = line.match(/^##\s+(.*?)\s*$/);
    if (!h) return;
    const m = h[1].match(/^C-(\d{8})-(\d+)$/);          // heading holds the ID only
    if (!m) return fail(`${file}:${i + 1} 条目标题不合规 → "${h[1]}"`);
    const [, d, n] = m;
    const id = `c-${d}-${n}`;
    if (d !== yyyymmdd) fail(`${file}:${i + 1} 条目日期与文件名不一致`);
    if (entries.has(id)) return fail(`${file}:${i + 1} 条目 ID 重复 → ${id}`);
    entries.set(id, { file, line: i + 1 });
    ns.push(Number(n));
  });
  ns.sort((a, b) => a - b);
  ns.forEach((n, i) => { if (n !== i + 1) fail(`${file} 序号不连续：期望 ${i + 1}，实际 ${n}`); });
  if (!ns.length) fail(`${file} 没有任何条目`);
}

const rows = [];
readFileSync(CHANGELOG, "utf8").split("\n").forEach((line, i) => {
  const m = line.match(/^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$/);
  if (m && m[1] !== "日期") rows.push({ lineNo: i + 1, date: m[1], desc: m[2], cell: m[3] });
});
if (!rows.length) fail("CHANGELOG.md 表格里没有任何变更行");

const linked = new Set();
for (const r of rows) {
  const lm = r.cell.match(/\]\((logs\/(\d{4}-\d{2}-\d{2})\.md)#([a-z0-9-]+)\)/);
  if (!lm) { fail(`CHANGELOG.md:${r.lineNo} 链接格式不合规 → "${r.cell}"`); continue; }
  const [, rel, linkDate, anchor] = lm;
  if (!existsSync(join(ROOT, rel))) { fail(`CHANGELOG.md:${r.lineNo} 日志文件不存在 → ${rel}`); continue; }
  if (!entries.has(anchor)) { fail(`CHANGELOG.md:${r.lineNo} 锚点 #${anchor} 不存在于 ${rel}`); continue; }
  if (linked.has(anchor)) fail(`CHANGELOG.md:${r.lineNo} 条目 ${anchor} 被索引多次`);
  linked.add(anchor);
  if (r.date !== linkDate) fail(`CHANGELOG.md:${r.lineNo} 行日期与链接文件日期不一致`);
  if (anchor.split("-")[1] !== linkDate.replace(/-/g, "")) fail(`CHANGELOG.md:${r.lineNo} 行日期与条目 ID 日期不一致`);
  const wc = wordCount(r.desc);
  if (wc > MAX_WORDS) fail(`CHANGELOG.md:${r.lineNo} 描述 ${wc} word 超过 ${MAX_WORDS} → "${r.desc}"`);
}
for (const id of entries.keys()) if (!linked.has(id)) {
  const e = entries.get(id);
  fail(`日志条目 ${e.file}:${e.line} 的 ${id} 没有在 CHANGELOG.md 建索引`);
}

console.log(`logs/ 文件 ${logFiles.length} 个 ／ 条目 ${entries.size} 条 ／ 索引 ${rows.length} 行`);
if (problems.length) {
  console.error(`\n✗ 变更台账机检失败（${problems.length} 项）：`);
  for (const p of problems) console.error(`  · ${p}`);
  process.exit(1);
}
console.log("✓ 变更台账机检通过（索引↔日志双向闭合、ID 唯一、链接可达、描述合规）");
