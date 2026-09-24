"use strict";

/* News feed front end. No framework, no build step.
   The URL is the source of truth for filters: ?t=ai&t=-legislation&q=agents&sort=top */

const state = {
  include: [],
  exclude: [],
  q: "",
  sort: "new",
  me: { email: null },
  meta: { timezone: "Europe/London", similar: [] },
  posts: [],
  next: null,
  loading: false,
  unknown: [],
  collapsed: new Set(),
};

// ---- tiny DOM helper ---------------------------------------------------
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
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
const $ = (sel) => document.querySelector(sel);
// Native append() prints null as "null", so drop empty children first.
const kids = (...xs) => xs.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false);
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  return res.json();
}

// ---- dates -------------------------------------------------------------
function tz() { return state.meta.timezone || "Europe/London"; }
function dayKey(date) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz(), year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function dayPosterName(key) {
  const [y, m, d] = key.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d, 12));
  return `${WEEKDAYS[date.getUTCDay()]} ${d} ${MONTHS[m - 1]}`;
}
function relativeDay(key) {
  const today = dayKey(new Date());
  const yesterday = dayKey(new Date(Date.now() - 864e5));
  if (key === today) return "Today";
  if (key === yesterday) return "Yesterday";
  return "";
}
function clock(iso) {
  return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: tz() }).format(new Date(iso));
}
function shortDate(iso) {
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: tz() }).format(new Date(iso));
}

// ---- URL state ---------------------------------------------------------
function readUrl() {
  const p = new URLSearchParams(location.search);
  state.include = [];
  state.exclude = [];
  for (const raw of p.getAll("t")) {
    for (const v of raw.split(",")) {
      const t = v.trim().toLowerCase();
      if (!t) continue;
      if (t.startsWith("-")) state.exclude.push(t.slice(1));
      else state.include.push(t.replace(/^\+/, ""));
    }
  }
  state.q = p.get("q") || "";
  state.sort = p.get("sort") === "top" ? "top" : "new";
}
function currentQuery() {
  const p = new URLSearchParams();
  state.include.forEach((t) => p.append("t", t));
  state.exclude.forEach((t) => p.append("t", `-${t}`));
  if (state.q) p.set("q", state.q);
  if (state.sort === "top") p.set("sort", "top");
  return p;
}
function commit() {
  const qs = currentQuery().toString();
  history.pushState(null, "", qs ? `/?${qs}` : "/");
  renderFilters();
  load(true);
}
window.addEventListener("popstate", () => { readUrl(); renderFilters(); load(true); });

function includeTag(slug) {
  state.exclude = state.exclude.filter((t) => t !== slug);
  if (!state.include.includes(slug)) state.include.push(slug);
  commit();
}
function excludeTag(slug) {
  state.include = state.include.filter((t) => t !== slug);
  if (!state.exclude.includes(slug)) state.exclude.push(slug);
  commit();
}

// ---- filters row -------------------------------------------------------
function renderFilters() {
  const box = $("#filters");
  box.replaceChildren();
  for (const slug of state.include) {
    box.append(h("span", { class: "chip" },
      h("button", { class: "chip-label", title: "Hide this tag instead", onClick: () => excludeTag(slug) }, `#${slug}`),
      h("button", { "aria-label": `Remove #${slug} filter`, onClick: () => { state.include = state.include.filter((t) => t !== slug); commit(); } }, "×")));
  }
  for (const slug of state.exclude) {
    box.append(h("span", { class: "chip excluded" },
      h("button", { class: "chip-label", title: "Show only this tag instead", onClick: () => includeTag(slug) }, `#${slug}`),
      h("button", { "aria-label": `Stop hiding #${slug}`, onClick: () => { state.exclude = state.exclude.filter((t) => t !== slug); commit(); } }, "×")));
  }
  if (state.q) {
    box.append(h("span", { class: "chip query" },
      h("span", { class: "chip-label" }, `“${state.q}”`),
      h("button", { "aria-label": "Clear search", onClick: () => { state.q = ""; commit(); } }, "×")));
  }
  box.append(h("button", {
    class: "sort-toggle",
    onClick: () => { state.sort = state.sort === "top" ? "new" : "top"; commit(); },
  }, state.sort === "top" ? "Sorted by importance" : "Sorted by newest"));
}

// ---- search box with tag typeahead -------------------------------------
const search = {
  input: null, list: null, options: [], index: -1, timer: null,
  init() {
    this.input = $("#q");
    this.list = $("#suggest");
    this.input.addEventListener("input", () => this.schedule());
    this.input.addEventListener("focus", () => this.schedule());
    this.input.addEventListener("keydown", (e) => this.key(e));
    document.addEventListener("click", (e) => { if (!e.target.closest(".search")) this.close(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
        e.preventDefault();
        this.input.focus();
      }
    });
  },
  schedule() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.refresh(), 120);
  },
  async refresh() {
    const raw = this.input.value.trim();
    if (!raw) return this.close();
    const negate = raw.startsWith("-");
    const term = raw.replace(/^[-#+]+/, "");
    let tags = [];
    try { tags = term ? await api(`/api/tags?prefix=${encodeURIComponent(term)}`) : []; } catch (_) { tags = []; }
    this.options = [];
    if (!negate && !raw.startsWith("#")) this.options.push({ kind: "search", text: raw });
    for (const t of tags) {
      if (state.include.includes(t.slug) || state.exclude.includes(t.slug)) continue;
      this.options.push({ kind: "tag", tag: t, negate });
    }
    this.index = this.options.length ? 0 : -1;
    this.draw(term);
  },
  draw(term) {
    this.list.replaceChildren();
    this.options.forEach((opt, i) => {
      const li = h("li", { role: "option", id: `opt-${i}`, "aria-selected": String(i === this.index) });
      if (opt.kind === "search") {
        li.append(h("span", {}, "Search posts for ", h("strong", {}, `“${opt.text}”`)));
      } else {
        li.append(...kids(
          h("span", {}, opt.negate ? "Hide " : "", h("strong", {}, `#${opt.tag.slug}`)),
          opt.tag.label.toLowerCase() !== opt.tag.slug.replace(/-/g, " ") ? h("span", { class: "hint" }, opt.tag.label) : null,
          h("span", { class: "kind" }, `${opt.tag.type}, ${opt.tag.uses} ${opt.tag.uses === 1 ? "post" : "posts"}`),
          opt.negate ? null : h("button", {
            class: "not", title: `Hide posts tagged #${opt.tag.slug}`,
            onClick: (e) => { e.stopPropagation(); this.pick({ ...opt, negate: true }); },
          }, "Hide"),
        ));
      }
      li.addEventListener("mousedown", (e) => { e.preventDefault(); this.pick(opt); });
      this.list.append(li);
    });
    if (!this.options.length) {
      this.list.append(h("li", { class: "hint", role: "option", "aria-disabled": "true" },
        term ? `No tag matches “${term}”. Only existing tags can be used as filters.` : "Type a tag name."));
    }
    this.list.hidden = false;
    this.input.setAttribute("aria-expanded", "true");
    if (this.index >= 0) this.input.setAttribute("aria-activedescendant", `opt-${this.index}`);
    else this.input.removeAttribute("aria-activedescendant");
  },
  key(e) {
    if (this.list.hidden && e.key === "Enter") { this.refresh().then(() => this.pick(this.options[this.index])); e.preventDefault(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!this.options.length) return;
      this.index = (this.index + (e.key === "ArrowDown" ? 1 : -1) + this.options.length) % this.options.length;
      [...this.list.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === this.index)));
      this.input.setAttribute("aria-activedescendant", `opt-${this.index}`);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const opt = this.options[this.index];
      if (opt) this.pick(e.shiftKey && opt.kind === "tag" ? { ...opt, negate: true } : opt);
    } else if (e.key === "Escape") {
      this.close();
    }
  },
  pick(opt) {
    if (!opt) return;
    this.input.value = "";
    this.close();
    if (opt.kind === "search") { state.q = opt.text; commit(); }
    else if (opt.negate) excludeTag(opt.tag.slug);
    else includeTag(opt.tag.slug);
  },
  close() {
    this.list.hidden = true;
    this.input.setAttribute("aria-expanded", "false");
    this.input.removeAttribute("aria-activedescendant");
  },
};

// ---- feed --------------------------------------------------------------
async function load(reset) {
  if (state.loading) return;
  state.loading = true;
  const qs = currentQuery();
  if (!reset && state.next) qs.set("before", state.next);
  try {
    const data = await api(`/api/feed?${qs.toString()}`);
    state.posts = reset ? data.posts : state.posts.concat(data.posts);
    state.next = data.next_before;
    state.unknown = data.unknown_tags || [];
    renderFeed();
  } catch (err) {
    $("#feed").replaceChildren(h("p", { class: "notice" }, `Couldn't load the feed. ${err.message}`));
  } finally {
    state.loading = false;
  }
}

function groupByDay(posts) {
  const groups = new Map();
  for (const p of posts) {
    const key = p.day || dayKey(new Date(p.feed_at));
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  }
  return groups;
}

function renderFeed() {
  const feed = $("#feed");
  feed.replaceChildren();
  if (state.unknown.length) {
    feed.append(h("p", { class: "notice" },
      `There is no tag called ${state.unknown.map((t) => `#${t}`).join(", ")}, so it was ignored.`));
  }
  const filtered = state.include.length || state.exclude.length || state.q;
  if (!state.posts.length) {
    feed.append(filtered
      ? h("div", { class: "empty" }, h("h2", {}, "Nothing matches"),
          h("p", {}, "No posts match these filters. Remove one to see more."),
          h("button", { class: "btn quiet", onClick: () => { state.include = []; state.exclude = []; state.q = ""; commit(); } }, "Clear filters"))
      : h("div", { class: "empty" }, h("h2", {}, "No posts yet"),
          h("p", {}, "Scans run at 04:00 and 13:00 UK time. Admins can start one now from the ",
            h("a", { href: "/runs" }, "scan log"), ".")));
    return;
  }
  for (const [key, posts] of groupByDay(state.posts)) {
    const open = !state.collapsed.has(key);
    const listId = `day-${key}`;
    const rel = relativeDay(key);
    const poster = h("button", {
      class: "day-poster", "aria-expanded": String(open), "aria-controls": listId,
      onClick: () => {
        if (state.collapsed.has(key)) state.collapsed.delete(key); else state.collapsed.add(key);
        const nowOpen = !state.collapsed.has(key);
        poster.setAttribute("aria-expanded", String(nowOpen));
        document.getElementById(listId).hidden = !nowOpen;
      },
    },
      h("span", { class: "day-name" }, dayPosterName(key)),
      h("span", { class: "day-meta" },
        rel ? h("span", {}, rel) : null,
        h("span", {}, `${posts.length} ${posts.length === 1 ? "post" : "posts"}`),
        h("span", { class: "chev", "aria-hidden": "true" }, "▾")));
    const list = h("ol", { class: "day-posts", id: listId, hidden: !open }, posts.map(renderPost));
    feed.append(h("section", { class: "day", "aria-label": dayPosterName(key) }, h("h2", { class: "visually-hidden" }, dayPosterName(key)), poster, list));
  }
  if (state.next) {
    feed.append(h("button", { class: "btn quiet more", onClick: () => load(false) }, "Show older posts"));
  }
}

const STAGE_TEXT = { proposed: "Proposed", passed: "Passed", in_force: "In force", enforcement: "Enforcement", guidance: "Guidance" };
const TYPE_TEXT = { official: "Official", podcast: "Podcast", video: "Video", paper: "Paper", repo: "Repo", discussion: "Discussion", legislation: "Legal text" };

function renderTags(p) {
  return h("ul", { class: "tags", "aria-label": "Tags" }, p.tags.map((t) => h("li", {},
    h("button", { class: `tag t-${t.type}`, title: `Show only #${t.slug}`, onClick: () => includeTag(t.slug) }, t.slug))));
}

function renderPost(p) {
  const canWrite = Boolean(state.me.email);
  const li = h("li", { class: `post${p.importance >= 4 ? " major" : ""}${p.flagged ? " flagged" : ""}`, id: `post-${p.id}` });
  const slot = h("div", { class: "panel-slot" });
  let openPanel = null;
  const toggle = (name, build) => {
    if (openPanel === name) { slot.replaceChildren(); openPanel = null; return; }
    openPanel = name;
    slot.replaceChildren(build());
  };

  const byline = h("div", { class: "byline" },
    h("span", { class: "source" }, p.source_name || "Unknown source"),
    h("time", { datetime: p.feed_at }, clock(p.feed_at)),
    TYPE_TEXT[p.content_type] ? h("span", { class: "badge" }, TYPE_TEXT[p.content_type]) : null,
    p.legislation_stage ? h("span", { class: "badge" },
      `${STAGE_TEXT[p.legislation_stage] || p.legislation_stage}${p.jurisdiction ? ` (${p.jurisdiction.toUpperCase()})` : ""}`) : null,
    p.importance >= 4 ? h("span", { class: "badge" }, "Big") : null);

  const voteBtn = h("button", {
    class: "act vote", "aria-pressed": String(p.voted), disabled: !canWrite,
    title: canWrite ? "Upvote" : "Sign in to vote",
    onClick: async () => {
      try {
        const r = await api(`/api/posts/${p.id}/vote`, { method: "POST" });
        p.voted = r.voted; p.votes = r.votes;
        voteBtn.setAttribute("aria-pressed", String(r.voted));
        voteBtn.lastChild.textContent = String(r.votes);
      } catch (err) { alert(err.message); }
    },
  }, h("span", { class: "arrow", "aria-hidden": "true" }, "▲"), h("span", { class: "visually-hidden" }, "Upvote, "), h("span", {}, String(p.votes)));

  const commentBtn = h("button", {
    class: "act", "aria-expanded": "false",
    onClick: () => { toggle("comments", () => commentsPanel(p, commentBtn)); commentBtn.setAttribute("aria-expanded", String(openPanel === "comments")); },
  }, p.comment_count ? `${p.comment_count} ${p.comment_count === 1 ? "comment" : "comments"}` : "Comment");

  const coverageBtn = p.coverage_count ? h("button", {
    class: "act", "aria-expanded": "false",
    onClick: () => { toggle("coverage", () => coveragePanel(p)); coverageBtn.setAttribute("aria-expanded", String(openPanel === "coverage")); },
  }, `${p.coverage_count} more ${p.coverage_count === 1 ? "source" : "sources"}`) : null;

  const tagsHolder = h("div", {}, renderTags(p));
  const tagBtn = h("button", {
    class: "act", disabled: !canWrite, title: canWrite ? "Add a tag" : "Sign in to add tags",
    onClick: () => toggle("tag", () => tagPanel(p, tagsHolder)),
  }, "Add tag");

  const flagBtn = h("button", {
    class: "act flag", "aria-pressed": String(p.flagged), disabled: !canWrite,
    title: "Tell the curator this didn't belong here",
    onClick: async () => {
      try {
        const r = await api(`/api/posts/${p.id}/not-relevant`, { method: "POST" });
        p.flagged = r.flagged;
        flagBtn.setAttribute("aria-pressed", String(r.flagged));
        li.classList.toggle("flagged", r.flagged);
      } catch (err) { alert(err.message); }
    },
  }, "Not relevant");

  const storyLine = p.story_id && p.story_post_count > 1 ? h("p", { class: "story-link" },
    `Part of a story with ${p.story_post_count} updates. `,
    h("button", { onClick: () => toggle("story", () => storyPanel(p)) }, "See the timeline")) : null;

  li.append(...kids(
    byline,
    h("h3", {}, h("a", { href: safeUrl(p.url), target: "_blank", rel: "noopener noreferrer" }, p.title)),
    p.delta ? h("p", { class: "delta" }, `New: ${p.delta}`) : null,
    h("p", { class: "summary" }, p.summary),
    storyLine,
    tagsHolder,
    h("div", { class: "actions" }, voteBtn, commentBtn, coverageBtn, tagBtn, flagBtn),
    slot,
  ));
  return li;
}

function coveragePanel(p) {
  const panel = h("div", { class: "panel" }, h("p", {}, "Loading…"));
  api(`/api/posts/${p.id}/coverage`).then((rows) => {
    panel.replaceChildren(h("ul", {}, rows.map((r) => h("li", {},
      h("a", { href: safeUrl(r.url), target: "_blank", rel: "noopener noreferrer" }, r.title),
      h("span", { class: "when" }, r.source_name || "")))));
  }).catch((err) => panel.replaceChildren(h("p", {}, err.message)));
  return panel;
}

function storyPanel(p) {
  const panel = h("div", { class: "panel" }, h("p", {}, "Loading…"));
  api(`/api/stories/${p.story_id}`).then((s) => {
    panel.replaceChildren(
      h("p", {}, h("strong", {}, s.headline)),
      h("ul", {}, s.posts.map((sp) => h("li", {},
        h("span", { class: "who" }, shortDate(sp.feed_at)), " ",
        h("a", { href: safeUrl(sp.url), target: "_blank", rel: "noopener noreferrer" }, sp.delta || sp.title),
        h("span", { class: "when" }, sp.source_name || "")))));
  }).catch((err) => panel.replaceChildren(h("p", {}, err.message)));
  return panel;
}

function commentsPanel(p, countBtn) {
  const list = h("ul", {});
  const panel = h("div", { class: "panel" }, list);
  const draw = (rows) => {
    list.replaceChildren(...(rows.length ? rows.map((c) => h("li", {},
      h("span", { class: "who", title: c.user_email }, c.name),
      h("span", { class: "when" }, shortDate(c.created_at)),
      c.user_email === state.me.email || state.me.admin ? h("button", {
        class: "link-btn", style: "margin-left:0.5rem",
        onClick: async () => {
          try { await api(`/api/comments/${c.id}`, { method: "DELETE" }); rows = rows.filter((x) => x.id !== c.id); update(rows); }
          catch (err) { alert(err.message); }
        },
      }, "Delete") : null,
      h("p", {}, c.body))) : [h("li", { class: "when" }, "No comments yet.")]));
  };
  const update = (rows) => {
    p.comment_count = rows.length;
    countBtn.textContent = rows.length ? `${rows.length} ${rows.length === 1 ? "comment" : "comments"}` : "Comment";
    draw(rows);
  };
  let rows = [];
  api(`/api/posts/${p.id}/comments`).then((r) => { rows = r; draw(rows); }).catch((err) => list.replaceChildren(h("li", {}, err.message)));
  if (state.me.email) {
    const box = h("textarea", { "aria-label": "Your comment", placeholder: "Add a comment", maxlength: "4000" });
    const form = h("form", {
      onSubmit: async (e) => {
        e.preventDefault();
        const body = box.value.trim();
        if (!body) return;
        try {
          const c = await api(`/api/posts/${p.id}/comments`, { method: "POST", body: JSON.stringify({ body }) });
          rows = rows.concat([c]); box.value = ""; update(rows);
        } catch (err) { alert(err.message); }
      },
    }, box, h("button", { class: "btn", type: "submit" }, "Post comment"));
    panel.append(form);
  } else {
    panel.append(h("p", { class: "when" }, h("a", { href: `/login?next=${encodeURIComponent(location.pathname + location.search)}` }, "Sign in"), " to comment."));
  }
  return panel;
}

function tagPanel(p, tagsHolder) {
  const input = h("input", { type: "text", "aria-label": "Tag name", placeholder: "Type a tag", maxlength: "60", autocomplete: "off" });
  const hints = h("div", { class: "suggest-inline" });
  const add = async (value) => {
    const v = (value || "").trim();
    if (!v) return;
    try {
      const t = await api(`/api/posts/${p.id}/tags`, { method: "POST", body: JSON.stringify({ tag: v }) });
      if (!p.tags.some((x) => x.slug === t.slug)) p.tags.push(t);
      tagsHolder.replaceChildren(renderTags(p));
      input.value = ""; hints.replaceChildren();
    } catch (err) { alert(err.message); }
  };
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const term = input.value.trim().replace(/^#/, "");
      if (!term) return hints.replaceChildren();
      const tags = await api(`/api/tags?prefix=${encodeURIComponent(term)}&limit=6`).catch(() => []);
      hints.replaceChildren(...tags.filter((t) => !p.tags.some((x) => x.slug === t.slug))
        .map((t) => h("button", { class: "tag", type: "button", onClick: () => add(t.slug) }, t.slug)));
    }, 120);
  });
  const form = h("form", { class: "row", onSubmit: (e) => { e.preventDefault(); add(input.value.replace(/^#/, "")); } },
    input, h("button", { class: "btn", type: "submit" }, "Add tag"));
  setTimeout(() => input.focus(), 0);
  return h("div", { class: "panel" }, form, hints,
    h("p", { class: "when" }, "Pick an existing tag or type a new one. New tags are available as filters straight away."));
}

// ---- header and footer -------------------------------------------------
function renderMe() {
  const el = $("#me");
  el.replaceChildren();
  const back = encodeURIComponent(location.pathname + location.search);
  el.append(
    state.me.email ? `${state.me.name}. ` : h("a", { href: `/login?next=${back}` }, "Sign in"),
    state.me.email ? null : " to vote and comment. ",
    h("a", { href: "/sources" }, "Sources"), " ", h("a", { href: "/runs" }, "Scan log"),
    state.me.email ? [" ", h("a", { href: "/logout" }, "Sign out")] : null);
}

function renderFooter() {
  const f = $("#footer");
  f.replaceChildren();
  if (state.meta.similar && state.meta.similar.length) {
    f.append(h("h2", {}, "Also worth reading"),
      h("ul", {}, state.meta.similar.map((s) => h("li", {},
        h("a", { href: safeUrl(s.url), target: "_blank", rel: "noopener noreferrer" }, s.name),
        s.note ? h("span", {}, ` ${s.note}`) : null))));
  }
  const last = state.meta.last_run;
  f.append(h("p", {},
    last && last.finished_at ? `Last scan ${shortDate(last.finished_at)}${last.status === "ok" ? "" : " (failed, see the scan log)"}. ` : "No scans yet. ",
    "Scans run at 04:00 and 13:00 UK time. Press / to search."));
}

async function init() {
  readUrl();
  search.init();
  renderFilters();
  const [me, meta] = await Promise.all([api("/api/me").catch(() => ({})), api("/api/meta").catch(() => ({}))]);
  state.me = me || { email: null };
  state.meta = { ...state.meta, ...(meta || {}) };
  renderMe();
  renderFooter();
  load(true);
}

init();
