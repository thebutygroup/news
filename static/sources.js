"use strict";

/* Sources: every organisation we follow, every channel we know for it, how each channel is
   read, and whether it's working. Plus organisations the system thinks we should follow. */

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
function ago(iso) {
  if (!iso) return "never";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 90) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  return hrs < 48 ? `${hrs}h ago` : `${Math.round(hrs / 24)}d ago`;
}

const CHANNEL = {
  newsroom: "Newsroom", blog: "Blog", research: "Research", substack: "Substack", podcast: "Podcast",
  youtube: "YouTube", x: "X", linkedin: "LinkedIn", bluesky: "Bluesky", mastodon: "Mastodon", threads: "Threads",
  instagram: "Instagram", github: "GitHub", community: "Community", "search-feed": "Search feed", other: "Other",
};
const GROUPS = [
  ["lab", "AI labs"], ["company", "Companies"], ["regulator", "Regulators"],
  ["publication", "Publications and newsletters"], ["person", "People"], ["community", "Communities"],
];

let me = {};
let meta = {};
const root = document.getElementById("sources");

function modeText(ch) {
  if (ch.status === "paused") return "paused";
  if (ch.last_error) return "failing";
  if (ch.kind === "none") return "link";
  if (ch.kind === "x") return meta.x_enabled ? `polled ${ago(ch.last_fetched_at)}` : "link, no X token";
  if (!ch.last_fetched_at) return ch.kind === "page" ? "page watch, not read yet" : "feed, not read yet";
  return `${ch.kind === "page" ? "page watch" : "feed"} ${ago(ch.last_fetched_at)}`;
}
function isPolled(ch) {
  return ch.status === "active" && (ch.kind === "rss" || ch.kind === "page" || ch.kind === "hn" || (ch.kind === "x" && meta.x_enabled));
}

function channelChip(ch) {
  const bad = ch.status === "paused" || ch.last_error;
  const label = ch.kind === "rss" && ch.channel === "youtube" ? "YouTube feed" : (CHANNEL[ch.channel] || ch.channel);
  return h("a", {
    class: `ch${isPolled(ch) && !bad ? " polled" : ""}${bad ? " bad" : ""}`,
    href: safeUrl(ch.url), target: "_blank", rel: "noopener noreferrer",
    title: [ch.url, ch.last_error ? `Last error: ${ch.last_error}` : null,
      ch.found_30d !== undefined ? `${ch.found_30d} found, ${ch.posted_30d} posted in 30 days` : null].filter(Boolean).join("\n"),
  }, h("span", {}, label), h("span", { class: "mode" }, modeText(ch)));
}

function channelTable(org, reload) {
  return h("div", { class: "scroll-x" }, h("table", { class: "runs-table" },
    h("thead", {}, h("tr", {}, ["Channel", "Read as", "Last read", "Last new item", "30 days", "Where from", ""].map((c) => h("th", { scope: "col" }, c)))),
    h("tbody", {}, org.channels.map((ch) => h("tr", {},
      h("td", {}, h("a", { href: safeUrl(ch.url), target: "_blank", rel: "noopener noreferrer" }, CHANNEL[ch.channel] || ch.channel),
        ch.last_error ? h("div", { class: "why" }, ch.last_error) : null),
      h("td", {}, { rss: "Feed", page: "Page watch", x: "X API", hn: "Hacker News", none: "Link only" }[ch.kind] || ch.kind),
      h("td", {}, ch.kind === "none" ? "" : ago(ch.last_fetched_at)),
      h("td", {}, ch.kind === "none" ? "" : ago(ch.last_new_item_at)),
      h("td", {}, ch.kind === "none" ? "" : `${ch.found_30d} found, ${ch.posted_30d} posted`),
      h("td", {}, { seed: "Registry", discovered: "Their site", added: "Added by hand" }[ch.origin] || ch.origin),
      h("td", {}, me.admin ? h("span", { class: "row" },
        ch.kind !== "none" ? h("button", { class: "link-btn", onClick: () => act(`/api/sources/${ch.id}/status?status=${ch.status === "paused" ? "active" : "paused"}`, reload) },
          ch.status === "paused" ? "Resume" : "Pause") : null, " ",
        h("button", { class: "link-btn", onClick: () => act(`/api/sources/${ch.id}/status?status=dismissed`, reload) }, "Remove")) : ""))))));
}

async function act(path, reload, body) {
  try {
    const r = await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
    await reload();
    return r;
  } catch (err) { alert(err.message); return null; }
}

function orgItem(org, reload) {
  const detail = h("div", { class: "org-detail", hidden: true });
  const open = () => {
    if (!detail.hidden) { detail.hidden = true; return; }
    const url = h("input", { type: "url", placeholder: "Paste any link for them: newsroom, feed, X, LinkedIn, YouTube, Substack…", "aria-label": `New channel for ${org.name}` });
    detail.replaceChildren(...kids(
      org.channels.length ? channelTable(org, reload) : h("p", { class: "why" }, "No channels yet."),
      me.admin ? h("form", { class: "inline-form", onSubmit: async (e) => { e.preventDefault(); if (url.value) await act(`/api/orgs/${org.id}/channels`, reload, { url: url.value }); } },
        url, h("button", { class: "btn", type: "submit" }, "Add channel"),
        org.homepage ? h("button", { class: "btn quiet", type: "button", onClick: async (e) => {
          e.target.textContent = "Reading their site…"; e.target.disabled = true;
          const r = await act(`/api/orgs/${org.id}/discover`, reload);
          if (r) alert(r.error ? `Couldn't read ${org.homepage}: ${r.error}` : `Found ${r.found} channels, ${r.added.length} new.`);
        } }, "Find channels on their site") : null,
        h("button", { class: "btn quiet", type: "button", onClick: () => act(`/api/orgs/${org.id}/priority?value=${org.priority === "core" ? "watch" : "core"}`, reload) },
          org.priority === "core" ? "Check daily instead" : "Check every scan"),
        h("button", { class: "btn quiet", type: "button", onClick: () => { if (confirm(`Stop following ${org.name}?`)) act(`/api/orgs/${org.id}/status?status=dismissed`, reload); } }, "Unfollow")) : null,
      org.notes ? h("p", { class: "why" }, org.notes) : null,
      h("p", { class: "why" }, `Channel discovery: ${org.channels_discovered_at ? ago(org.channels_discovered_at) : "not run yet"}.`),
    ));
    detail.hidden = false;
  };
  const polled = org.channels.filter(isPolled).length;
  return h("li", { class: "org", "data-search": `${org.name} ${org.slug} ${(org.aliases || []).join(" ")}`.toLowerCase() },
    h("div", { class: "org-head" },
      h("button", { class: "org-name", "aria-expanded": "false", onClick: (e) => { open(); e.currentTarget.setAttribute("aria-expanded", String(!detail.hidden)); } }, org.name),
      h("span", { class: "org-stats" },
        org.priority === "watch" ? "Checked daily. " : "",
        polled ? `${org.posted_30d} posted from their channels in 30 days. ` : "Nothing polled, web search covers them. ",
        org.tag_slug ? h("a", { href: `/?t=${encodeURIComponent(org.tag_slug)}` }, `${org.mention_count} posts mention them`) : null)),
    h("div", { class: "channels" }, org.channels.map(channelChip),
      org.homepage ? h("a", { class: "ch", href: safeUrl(org.homepage), target: "_blank", rel: "noopener noreferrer" }, h("span", {}, "Website")) : null),
    detail);
}

function proposedItem(org, reload) {
  const home = h("input", { type: "url", value: org.homepage || "", placeholder: "Their website", "aria-label": `Website for ${org.name}` });
  const why = [];
  if (org.mention_count) why.push(`Mentioned in ${org.mention_count} posts`);
  if (org.search_hits) why.push(`web search landed on their site ${org.search_hits} times`);
  return h("li", { class: "org" },
    h("div", { class: "org-head" }, h("span", { class: "org-name" }, org.name), h("span", { class: "org-stats" }, `${why.join(", ")}.`)),
    org.recent_posts.length ? h("ul", { class: "why" }, org.recent_posts.map((p) => h("li", {}, h("a", { href: safeUrl(p.url), target: "_blank", rel: "noopener noreferrer" }, p.title)))) : null,
    me.admin ? h("form", { class: "inline-form", onSubmit: async (e) => {
      e.preventDefault();
      const r = await act(`/api/orgs/${org.id}/follow`, reload, { homepage: home.value || null });
      if (r && r.discovery) alert(r.discovery.error ? `Following. Couldn't read their site: ${r.discovery.error}` : `Following. Found ${r.discovery.found} channels on their site.`);
    } }, home, h("button", { class: "btn", type: "submit" }, "Follow"),
      h("button", { class: "btn quiet", type: "button", onClick: () => act(`/api/orgs/${org.id}/status?status=dismissed`, reload) }, "Dismiss")) : null);
}

function addOrgForm(reload) {
  const name = h("input", { type: "text", placeholder: "Organisation name", required: true, "aria-label": "Organisation name" });
  const home = h("input", { type: "url", placeholder: "Website", "aria-label": "Website" });
  const kind = h("select", { "aria-label": "Kind" }, GROUPS.map(([k, label]) => h("option", { value: k }, label.replace(/ and newsletters$/, ""))));
  kind.value = "company";
  return h("form", { class: "inline-form", onSubmit: async (e) => {
    e.preventDefault();
    const r = await act("/api/orgs", reload, { name: name.value, homepage: home.value || null, kind: kind.value });
    if (r) { name.value = ""; home.value = ""; if (r.discovery) alert(r.discovery.error ? `Added. Couldn't read their site: ${r.discovery.error}` : `Added. Found ${r.discovery.found} channels on their site.`); }
  } }, name, home, kind, h("button", { class: "btn", type: "submit" }, "Follow"));
}

async function render() {
  const [following, proposed, topicFeeds] = await Promise.all([
    api("/api/orgs"), api("/api/orgs?status=proposed"), api("/api/sources/topic"),
  ]);
  const channels = following.flatMap((o) => o.channels);
  const failing = channels.concat(topicFeeds).filter((c) => c.status === "paused" || c.last_error);
  const filter = h("input", { type: "search", placeholder: "Filter organisations", "aria-label": "Filter organisations" });
  const out = kids(
    h("h2", { class: "section-title" }, "Sources"),
    h("p", { class: "summary-line" },
      `Following ${following.length} organisations through ${channels.length} channels. `,
      `${channels.filter(isPolled).length} are read every scan or daily, ${channels.filter((c) => !isPolled(c) && c.status === "active").length} are links only. `,
      failing.length ? `${failing.length} need attention.` : "Everything polled is reading cleanly."),
    h("div", { class: "toolbar" }, filter),
    proposed.length ? [h("h2", { class: "section-title" }, "Suggested to follow"),
      h("p", { class: "summary-line" }, "The curator keeps naming these, or web search keeps landing on their sites."),
      h("ul", { class: "orgs" }, proposed.map((o) => proposedItem(o, render)))] : null,
    me.admin ? [h("h2", { class: "section-title" }, "Follow someone new"), h("div", { style: "margin: 0 1rem" }, addOrgForm(render))] : null,
    GROUPS.map(([k, label]) => {
      const list = following.filter((o) => o.kind === k);
      return list.length ? [h("h3", { class: "group-title" }, `${label} (${list.length})`), h("ul", { class: "orgs" }, list.map((o) => orgItem(o, render)))] : null;
    }),
    h("h2", { class: "section-title" }, "Topic feeds"),
    h("p", { class: "summary-line" }, "Feeds that belong to a topic rather than an organisation."),
    h("div", { class: "scroll-x" }, h("table", { class: "runs-table" },
      h("thead", {}, h("tr", {}, ["Feed", "Topic", "Last read", "30 days", ""].map((c) => h("th", { scope: "col" }, c)))),
      h("tbody", {}, topicFeeds.map((s) => h("tr", {},
        h("td", {}, s.url.startsWith("http") ? h("a", { href: safeUrl(s.url), target: "_blank", rel: "noopener noreferrer" }, s.name) : s.name,
          s.last_error ? h("div", { class: "why" }, s.last_error) : null),
        h("td", {}, s.topic), h("td", {}, s.status === "paused" ? "Paused" : ago(s.last_fetched_at)),
        h("td", {}, `${s.found_30d} found, ${s.posted_30d} posted`),
        h("td", {}, me.admin ? h("button", { class: "link-btn", onClick: () => act(`/api/sources/${s.id}/status?status=${s.status === "paused" ? "active" : "paused"}`, render) },
          s.status === "paused" ? "Resume" : "Pause") : "")))))),
  );
  root.replaceChildren(...out);
  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    root.querySelectorAll("li.org[data-search]").forEach((li) => { li.hidden = q && !li.dataset.search.includes(q); });
  });
}

async function init() {
  [me, meta] = await Promise.all([api("/api/me").catch(() => ({})), api("/api/meta").catch(() => ({}))]);
  document.getElementById("me").append(" ",
    me.email ? h("a", { href: "/logout" }, "Sign out") : h("a", { href: "/login?next=/sources" }, "Sign in"));
  try { await render(); } catch (err) { root.replaceChildren(h("p", { class: "notice" }, `Couldn't load sources. ${err.message}`)); }
}

init();
