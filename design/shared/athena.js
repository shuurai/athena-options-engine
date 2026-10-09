/* Athena design ideation — shared interaction layer.
   Toasts, modals, slide-in panels, loading, sparkline charts, 6-month fixtures. */
(function () {
  "use strict";

  /* ---------- styles injected once ---------- */
  const css = `
  .ath-toasts { position: fixed; right: 16px; bottom: 16px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; max-width: 360px; }
  .ath-toast {
    background: #10141a; border: 1px solid #333d49; color: #f2f4f7;
    padding: 11px 14px; font: 12.5px "IBM Plex Sans", sans-serif;
    display: flex; gap: 10px; align-items: flex-start;
    clip-path: polygon(6px 0, 100% 0, 100% calc(100% - 6px), calc(100% - 6px) 100%, 0 100%, 0 6px);
    box-shadow: 0 12px 40px rgba(0,0,0,.5);
    animation: athToastIn .22s cubic-bezier(.2,.9,.3,1.2);
  }
  .ath-toast.out { animation: athToastOut .18s ease forwards; }
  .ath-toast .t-ico { width: 8px; height: 8px; margin-top: 4px; flex-shrink: 0; }
  .ath-toast.ok .t-ico { background: #16c784; }
  .ath-toast.err .t-ico { background: #ea3943; }
  .ath-toast.info .t-ico { background: #6b96f2; }
  .ath-toast.warn .t-ico { background: #f7931a; }
  .ath-toast b { font-weight: 700; }
  .ath-toast .t-sub { color: #9aa5b1; font-size: 11.5px; margin-top: 2px; }
  @keyframes athToastIn { from { opacity: 0; transform: translateX(24px); } to { opacity: 1; transform: none; } }
  @keyframes athToastOut { to { opacity: 0; transform: translateY(8px); } }

  .ath-overlay {
    position: fixed; inset: 0; z-index: 9000;
    background: rgba(4,6,9,.66); backdrop-filter: blur(3px);
    display: none; align-items: center; justify-content: center;
    animation: athFade .16s ease;
  }
  .ath-overlay.open { display: flex; }
  @keyframes athFade { from { opacity: 0; } to { opacity: 1; } }
  .ath-modal {
    width: min(520px, calc(100vw - 32px));
    background: #10141a; border: 1px solid #333d49; color: #f2f4f7;
    clip-path: polygon(10px 0, 100% 0, 100% calc(100% - 10px), calc(100% - 10px) 100%, 0 100%, 0 10px);
    animation: athPop .2s cubic-bezier(.2,.9,.3,1.15);
    max-height: 84vh; display: flex; flex-direction: column;
  }
  @keyframes athPop { from { opacity: 0; transform: scale(.96) translateY(8px); } to { opacity: 1; transform: none; } }
  .ath-modal .m-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid #232a33; }
  .ath-modal .m-head b { font-size: 14px; letter-spacing: .02em; }
  .ath-modal .m-x { background: none; border: 1px solid #333d49; color: #9aa5b1; width: 26px; height: 26px; cursor: pointer; font-family: "IBM Plex Mono", monospace; }
  .ath-modal .m-x:hover { color: #fff; border-color: #6b96f2; }
  .ath-modal .m-body { padding: 16px; overflow-y: auto; font-size: 13px; color: #c3cad4; line-height: 1.55; }
  .ath-modal .m-foot { padding: 12px 16px; border-top: 1px solid #232a33; display: flex; gap: 8px; justify-content: flex-end; }

  .ath-panel {
    position: fixed; top: 0; right: 0; bottom: 0; z-index: 9001;
    width: min(460px, 94vw);
    background: #0d1117; border-left: 1px solid #333d49; color: #f2f4f7;
    transform: translateX(102%); transition: transform .24s cubic-bezier(.2,.9,.25,1);
    display: flex; flex-direction: column;
    box-shadow: -24px 0 60px rgba(0,0,0,.45);
  }
  .ath-panel.open { transform: none; }
  .ath-panel .p-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid #232a33; }
  .ath-panel .p-head b { font-size: 13px; letter-spacing: .04em; text-transform: uppercase; font-family: "IBM Plex Mono", monospace; }
  .ath-panel .p-body { padding: 16px; overflow-y: auto; flex: 1; font-size: 12.5px; color: #c3cad4; }

  .ath-skel { position: relative; overflow: hidden; background: #161c24; min-height: 12px; }
  .ath-skel::after {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.06), transparent);
    animation: athShimmer 1.1s infinite;
  }
  @keyframes athShimmer { from { transform: translateX(-100%); } to { transform: translateX(100%); } }

  .ath-spin { width: 14px; height: 14px; border: 2px solid #333d49; border-top-color: #6b96f2; animation: athSpin .7s linear infinite; display: inline-block; }
  @keyframes athSpin { to { transform: rotate(360deg); } }

  .ath-fade-in { animation: athFade .25s ease; }
  `;
  const styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  /* ---------- toast ---------- */
  let toastRoot = null;
  function ensureToastRoot() {
    if (!toastRoot) {
      toastRoot = document.createElement("div");
      toastRoot.className = "ath-toasts";
      document.body.appendChild(toastRoot);
    }
    return toastRoot;
  }
  function toast(title, sub, kind) {
    const root = ensureToastRoot();
    const el = document.createElement("div");
    el.className = "ath-toast " + (kind || "info");
    el.innerHTML =
      '<span class="t-ico"></span><div><b></b><div class="t-sub"></div></div>';
    el.querySelector("b").textContent = title;
    const s = el.querySelector(".t-sub");
    if (sub) s.textContent = sub; else s.remove();
    root.appendChild(el);
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 200);
    }, 3400);
  }

  /* ---------- overlay / modal / panel ---------- */
  let overlay = null;
  function ensureOverlay() {
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.className = "ath-overlay";
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeAll();
      });
      document.body.appendChild(overlay);
    }
    return overlay;
  }
  function closeAll() {
    document.querySelectorAll(".ath-modal").forEach((m) => m.remove());
    document.querySelectorAll(".ath-panel").forEach((p) => p.classList.remove("open"));
    if (overlay) overlay.classList.remove("open");
    setTimeout(() => document.querySelectorAll(".ath-panel").forEach((p) => p.remove()), 260);
  }
  function modal(opts) {
    ensureOverlay().classList.add("open");
    const m = document.createElement("div");
    m.className = "ath-modal";
    m.innerHTML =
      '<div class="m-head"><b></b><button class="m-x" aria-label="close">×</button></div>' +
      '<div class="m-body"></div>' +
      (opts.footer ? '<div class="m-foot"></div>' : "");
    m.querySelector(".m-head b").textContent = opts.title || "Dialog";
    const body = m.querySelector(".m-body");
    if (typeof opts.body === "string") body.innerHTML = opts.body;
    else if (opts.body instanceof Node) body.appendChild(opts.body);
    m.querySelector(".m-x").addEventListener("click", closeAll);
    if (opts.footer) {
      const foot = m.querySelector(".m-foot");
      opts.footer.forEach((b) => foot.appendChild(b));
    }
    overlay.appendChild(m);
    return m;
  }
  function panel(opts) {
    ensureOverlay().classList.add("open");
    const p = document.createElement("div");
    p.className = "ath-panel";
    p.innerHTML =
      '<div class="p-head"><b></b><button class="m-x" aria-label="close">×</button></div>' +
      '<div class="p-body"></div>';
    p.querySelector(".p-head b").textContent = opts.title || "Panel";
    const body = p.querySelector(".p-body");
    if (typeof opts.body === "string") body.innerHTML = opts.body;
    else if (opts.body instanceof Node) body.appendChild(opts.body);
    p.querySelector(".m-x").addEventListener("click", closeAll);
    document.body.appendChild(p);
    requestAnimationFrame(() => p.classList.add("open"));
    return p;
  }
  function confirmAction(title, bodyHtml, onConfirm, confirmLabel) {
    const ok = document.createElement("button");
    ok.className = "btn primary";
    ok.textContent = confirmLabel || "Confirm";
    const cancel = document.createElement("button");
    cancel.className = "btn ghost";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", closeAll);
    ok.addEventListener("click", () => {
      ok.disabled = true;
      ok.innerHTML = '<span class="ath-spin"></span>';
      setTimeout(() => {
        closeAll();
        if (onConfirm) onConfirm();
      }, 650);
    });
    modal({ title, body: bodyHtml, footer: [cancel, ok] });
  }

  /* ---------- action buttons (data-action) ---------- */
  const ACTION_COPY = {
    approve: ["Approved", "Draft queued for execution in Athena slot", "ok"],
    reject: ["Rejected", "Draft discarded · reason logged", "err"],
    amend: ["Amend", "Opening control link…", "info"],
    halt: ["Trading halted", "All new drafts blocked until resume", "warn"],
    resume: ["Resumed", "Trading gate restored", "ok"],
    save: ["Saved", "settings_v43 staged · audit written", "ok"],
    ops: ["Ops command sent", "ops-agent will report back", "info"],
    backup: ["Backup started", "Local snapshot → MinIO sync", "info"],
    link: ["Opening", "Loading surface…", "info"],
  };
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const act = btn.getAttribute("data-action");
    const copy = ACTION_COPY[act] || ["Done", "", "info"];
    if (act === "halt" || act === "approve") {
      confirmAction(
        act === "halt" ? "Halt all trading?" : "Approve this draft?",
        act === "halt"
          ? "Blocks every new order until you resume. Existing positions keep syncing."
          : "Order will submit to IB in the next Athena slot window.",
        () => toast(copy[0], copy[1], copy[2]),
        act === "halt" ? "Halt" : "Approve"
      );
      return;
    }
    toast(copy[0], copy[1], copy[2]);
  });

  /* ---------- loading helper ---------- */
  function simulateLoading(container, done, ms) {
    container.classList.add("ath-fade-in");
    container.innerHTML =
      '<div style="display:flex;align-items:center;gap:10px;padding:24px;color:#9aa5b1;font-size:12px">' +
      '<span class="ath-spin"></span> Loading…</div>';
    setTimeout(() => {
      if (done) done();
    }, ms || 700);
  }

  /* ---------- charts (inline SVG) ---------- */
  function linePath(values, w, h, pad) {
    const min = Math.min.apply(null, values);
    const max = Math.max.apply(null, values);
    const span = max - min || 1;
    const step = (w - pad * 2) / (values.length - 1);
    return values
      .map((v, i) => {
        const x = pad + i * step;
        const y = h - pad - ((v - min) / span) * (h - pad * 2);
        return (i === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1);
      })
      .join(" ");
  }
  function sparkline(el, values, opts) {
    opts = opts || {};
    const w = opts.w || 260;
    const h = opts.h || 64;
    const color = opts.color || "#6b96f2";
    const fill = opts.fill !== false;
    const d = linePath(values, w, h, 6);
    const area =
      d + "L" + (w - 6) + "," + (h - 6) + "L6," + (h - 6) + "Z";
    el.innerHTML =
      '<svg viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' +
      (fill ? '<path d="' + area + '" fill="' + color + '22"></path>' : "") +
      '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.6"></path>' +
      "</svg>";
  }
  function bars(el, values, opts) {
    opts = opts || {};
    const w = opts.w || 260;
    const h = opts.h || 72;
    const max = Math.max.apply(null, values.map((v) => Math.abs(v))) || 1;
    const n = values.length;
    const bw = (w - 8) / n;
    const mid = h / 2;
    let rects = "";
    values.forEach((v, i) => {
      const bh = (Math.abs(v) / max) * (h / 2 - 6);
      const x = 4 + i * bw;
      const y = v >= 0 ? mid - bh : mid;
      const c = v >= 0 ? "#16c784" : "#ea3943";
      rects += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + (bw * 0.66).toFixed(1) + '" height="' + Math.max(bh, 1).toFixed(1) + '" fill="' + c + '"></rect>';
    });
    el.innerHTML =
      '<svg viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' +
      '<line x1="0" y1="' + mid + '" x2="' + w + '" y2="' + mid + '" stroke="#232a33" stroke-width="1"></line>' +
      rects + "</svg>";
  }

  /* ---------- 6-month fixtures ---------- */
  function seedSeries(seed, n, base, vol, drift) {
    let s = seed;
    const out = [];
    let v = base;
    for (let i = 0; i < n; i++) {
      s = (s * 9301 + 49297) % 233280;
      const r = s / 233280 - 0.5;
      v = Math.max(1, v + r * vol + drift);
      out.push(Math.round(v * 100) / 100);
    }
    return out;
  }
  const MONTHS = ["May", "Jun", "Jul", "Aug", "Sep", "Oct"];
  const DATA = {
    months: MONTHS,
    // weekly NLV points (26 weeks)
    nlvWeekly: seedSeries(7, 26, 198000, 4200, 1050),
    // monthly premium collected USD
    premiumMonthly: [1240, 1580, 990, 1760, 2140, 1842],
    // monthly realized pnl USD (can be negative)
    pnlMonthly: [1240, 1620, -380, 1490, 2310, 960],
    // weekly IVR on NVDA
    ivrWeekly: seedSeries(21, 26, 45, 14, 1.1).map((v) => Math.min(96, Math.max(8, Math.round(v)))),
    // trades per month
    tradesMonthly: [6, 9, 7, 11, 13, 8],
    winRateMonthly: [83, 78, 71, 82, 85, 88],
  };

  function historyRows(n) {
    const tactics = ["OPEN_CSP", "OPEN_CC", "OPEN_STRANGLE", "ROLL", "CLOSE", "HOLD"];
    const syms = ["NVDA", "QCOM", "T", "F", "INTC", "DRAM"];
    const rows = [];
    let s = 42;
    for (let i = 0; i < n; i++) {
      s = (s * 9301 + 49297) % 233280;
      const r = s / 233280;
      const t = tactics[Math.floor(r * tactics.length)];
      const sym = syms[Math.floor(r * syms.length)];
      const pnl = Math.round((r - 0.28) * 900);
      const d = new Date(2026, 9, 8);
      d.setDate(d.getDate() - i * 3 - Math.floor(r * 2));
      rows.push({
        date: d.toISOString().slice(0, 10),
        sym,
        tactic: t,
        phase: ["P1", "P2", "P3", "P4", "P5"][Math.floor(r * 5)],
        pnl,
        status: r > 0.12 ? "filled" : "rejected",
      });
    }
    return rows;
  }

  /* ---------- clipboard ---------- */
  function copy(text, label) {
    const done = () => toast("Copied", (label || "") + " " + text, "ok");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(done);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      ta.remove();
      done();
    }
  }
  document.addEventListener("click", (e) => {
    const c = e.target.closest("[data-copy]");
    if (!c) return;
    e.preventDefault();
    copy(c.getAttribute("data-copy"), c.getAttribute("data-copy-label") || "");
  });

  const NAV_BADGES = {
    Drafts: { n: "2", title: "2 drafts awaiting Approve" },
    News: { n: "1", title: "1 wash cluster to review", warn: true },
    Settings: { n: "1", title: "Telegram not linked", warn: true },
  };
  document.querySelectorAll(".nav a").forEach((link) => {
    const spec = NAV_BADGES[link.textContent.trim()];
    if (!spec || link.querySelector(".nav-badge")) return;
    const badge = document.createElement("span");
    badge.className = "nav-badge" + (spec.warn ? " warn" : "");
    badge.textContent = spec.n;
    badge.title = spec.title;
    link.appendChild(badge);
  });

  window.Athena = {
    toast,
    modal,
    panel,
    confirmAction,
    simulateLoading,
    sparkline,
    bars,
    copy,
    DATA,
    historyRows,
    closeAll,
  };
})();
