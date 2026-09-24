"use strict";

/* Scan log: what each scan found, what the curator kept or rejected and why,
   source health, and sources discovery keeps bumping into. */

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const kids = (...xs) => xs.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false);
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, credentials: "same-origin", ...opts });
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  return res.json();
}
const fmt = (iso) => (iso ? new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Europe/London" }).format(new Date(iso)) : "");

const DECISIONS = [
  ["post", "Posted"], ["coverage", "Folded into a story"], ["rejected", "Rejected"],
  ["skipped", "Skipped"], ["kept", "Kept, not yet clustered"], ["pending", "Pending"],
];

let me = {};
const root = document.getElementById("runs");

function totals(stats) {
  const t = { found: 0, new: 0, kept: 0, rejected: 0, posts: 0, follow_up_posts: 0, coverage: 0, sources_failed: 0 };
  for (const [key, v] of Object.entries(stats || {})) {
    if (key === "llm" || typeof v !== "object" || v === null) continue;
    for (const k of Object.keys(t)) t[k] += Number(v[k] || 0);
  }
  return t;
}

async function renderRuns() {
  const runs = await api("/api/runs?limit=30");
  const wrap = h("div", { class: "scroll-x" });
  const table = h("table", { class: "runs-table" },
    h("thead", {}, h("tr", {}, ["Started", "Trigger", "Status", "Found", "New", "Kept", "Rejected", "Posted", "Folded", "LLM calls", "Searches"].map((c) => h("th", { scope: "col" }, c)))));
  const body = h("tbody", {});
  for (const r of runs) {
    const t = totals(r.stats);
    const llm = (r.stats && r.stats.llm) || {};
    const row = h("tr", { class: "pick", tabindex: "0", "aria-selected": "false" },
      h("td", {}, fmt(r.started_at)),
      h("td", {}, r.trigger),
      h("td", {}, r.status === "error" ? h("span", { class: "status-error", title: r.error || "" }, "Failed") : r.status === "ok" ? "OK" : "Running"),
      h("td", {}, t.found), h("td", {}, t.new), h("td", {}, t.kept), h("td", {}, t.rejected),
      h("td", {}, t.posts + t.follow_up_posts), h("td", {}, t.coverage),
      h("td", {}, llm.calls || 0), h("td", {}, llm.web_searches || 0));
    const open = () => {
      body.querySelectorAll("tr").forEach((x) => x.setAttribute("aria-selected", "false"));
      row.setAttribute("aria-selected", "true");
      showRun(r);
    };
    row.addEventListener("click", open);
    row.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
    body.append(row);
  }
  table.append(body);
  wrap.append(table);
  return h("section", {},
    h("h2", { class: "section-title" }, "Scans"),
    runs.length ? wrap : h("p", { class: "notice" }, "No scans yet. They run at 04:00 and 13:00 UK time."),
    h("div", { id: "run-detail" }));
}

async function showRun(run) {
  const detail = document.getElementById("run-detail");
  detail.replaceChildren(h("p", { class: "notice" }, "Loading…"));
  const rows = await api(`/api/runs/${run.id}/candidates`);
  const counts = Object.fromEntries(DECISIONS.map(([d]) => [d, rows.filter((r) => r.decision === d).length]));
  if (!rows.length) {
    detail.replaceChildren(h("h2", { class: "section-title" }, `Scan of ${fmt(run.started_at)}`),
      h("p", { class: "notice" }, run.error ? `This scan failed: ${run.error}` : "This scan found nothing new. Everything it saw had already been seen."));
    return;
  }
  let current = counts.rejected ? "rejected" : (DECISIONS.find(([d]) => counts[d]) || ["post"])[0];
  const list = h("div", {});
  const tabs = h("div", { class: "tabs" });
  const draw = () => {
    tabs.replaceChildren(...DECISIONS.filter(([d]) => counts[d]).map(([d, label]) =>
      h("button", { class: "btn quiet", "aria-pressed": String(d === current), onClick: () => { current = d; draw(); } }, `${label} (${counts[d]})`)));
    const shown = rows.filter((r) => r.decision === current);
    list.replaceChildren(h("div", { class: "scroll-x" }, h("table", { class: "runs-table" },
      h("thead", {}, h("tr", {}, ["Item", "Source", "Found via", "Why"].map((c) => h("th", { scope: "col" }, c)))),
      h("tbody", {}, shown.map((r) => h("tr", {},
        h("td", {}, h("a", { href: safeUrl(r.url), target: "_blank", rel: "noopener noreferrer" }, r.title)),
        h("td", {}, r.source_name || ""),
        h("td", {}, r.found_via),
        h("td", {}, r.reason || "")))))));
  };
  draw();
  detail.replaceChildren(...kids(
    h("h2", { class: "section-title" }, `Scan of ${fmt(run.started_at)}`),
    run.error ? h("p", { class: "notice" }, `This scan failed: ${run.error}`) : null,
    tabs, list));
}

async function init() {
  me = await api("/api/me").catch(() => ({}));
  const meEl = document.getElementById("me");
  meEl.append(h("a", { href: "/" }, "Feed"), " ", h("a", { href: "/sources" }, "Sources"), " ",
    me.email ? h("a", { href: "/logout" }, "Sign out") : h("a", { href: "/login?next=/runs" }, "Sign in"));
  if (me.admin) {
    meEl.append(h("button", { class: "btn", onClick: async (e) => {
      try { await api("/api/scan", { method: "POST" }); e.target.textContent = "Scan queued"; e.target.disabled = true; }
      catch (err) { alert(err.message); }
    } }, "Scan now"));
  }
  try {
    root.replaceChildren(await renderRuns());
  } catch (err) {
    root.replaceChildren(h("p", { class: "notice" }, `Couldn't load the scan log. ${err.message}`));
  }
}

init();
