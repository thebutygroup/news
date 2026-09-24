# News

A small team news feed. Twice a day it scans the web for what matters, cuts it down, groups
repeat coverage into stories, and posts the rest to a day-by-day feed. The team can vote,
comment, tag, and tell the curator what didn't belong.

Topic one is AI, tuned for the AI engineering team at Bauer Media Outdoor. More topics are just
more folders.

Lives at news.thebutygroup.com. Runs anywhere Docker runs.

**How it all fits together, and where everything is stored: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).**

## Quick start (no API key, fake curator)

```bash
cp .env.example .env
# set POSTGRES_PASSWORD, LLM_FAKE=1, DEV_USER_EMAIL=you@example.com
docker compose up -d --build
docker compose exec worker python -m app.worker --once   # run a scan now
```

Open http://localhost:8088. With `LLM_FAKE=1` the curator is keyword matching, so the feed is
crude. It proves the plumbing works.

## Going live

1. **Anthropic.** Put a key in `ANTHROPIC_API_KEY` and set `LLM_FAKE=0`. Web search has to be
   switched on for your organisation in the Anthropic Console, or discovery returns nothing.
   Set a monthly spend limit in the Console too.
2. **Start it.** `docker compose up -d --build`. Migrations and seeding run on startup.
3. **Cloudflare Tunnel.** In Zero Trust, add a public hostname `news.thebutygroup.com` on your
   existing tunnel. Point it at the web container:
   - If cloudflared runs on the host, or Docker Desktop can reach the host port:
     `http://host.docker.internal:8088`
   - Or put the web container on cloudflared's Docker network. Set `TUNNEL_NETWORK` in `.env`,
     run `docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build`,
     and point the hostname at `http://news-web:8080`.
4. **Cloudflare Access.** Add a self-hosted application with a policy that allows your team's
   emails. For its domain, use `news.thebutygroup.com` with the path `login`. That keeps the site
   readable by anyone, and signing in (the "Sign in" link, which goes to `/login`) is what lets
   people vote, comment and tag. To make the whole site private instead, leave the path empty.
   Copy two values into `.env`:
   - `CF_ACCESS_TEAM_DOMAIN`: something like `yourteam.cloudflareaccess.com`
   - `CF_ACCESS_AUD`: the Application Audience (AUD) tag on the app's overview page
5. **Admins.** Put your email in `ADMIN_EMAILS`. Admins can start scans, approve sources and
   hide posts.
6. **First scan.** Press "Scan now" on `/runs`, or run
   `docker compose exec worker python -m app.worker --once`. The first scan looks back 72
   hours. After that it looks back 36.

Scans then run at 04:00 and 13:00 UK time (`SCAN_CRON`, `APP_TIMEZONE`).

## Identity

There's no login code. Cloudflare Access signs people in and forwards a signed token. The app
verifies that token and reads the email from it. With the Access app on `/login` only, anyone can
read, and signed-in people can vote, comment and tag. Comments show the name from the email
address. Admin actions still need an email in `ADMIN_EMAILS`.

Modes, in order: verified token (`CF_ACCESS_TEAM_DOMAIN` + `CF_ACCESS_AUD`), then trusting the
email header (`TRUST_CF_EMAIL_HEADER=1`, only if the port is unreachable except through the
tunnel), then `DEV_USER_EMAIL` for local work. With none set, the site is read-only.

## How a scan works

```
watchlist feeds ─┐
Hacker News  ────┼─> candidates ─> URL dedup ─> curator (Claude) ─> story editor (Claude) ─> feed
web search ──────┘       │                          │ rejected + reason    │ same story, nothing new
                         └── every item logged      └── /runs              └── folded in as "N more sources"
```

1. **Collect.** Every channel of every organisation we follow (`registry/orgs.json`, managed on
   `/sources`): feeds, newsroom pages watched for new links, and X accounts if you have an X API
   token. Plus topic feeds like Hacker News, and a Claude web search sweep. Search results are only kept if the URL actually
   appeared in the search results, so no invented links. Sites that keep turning up in search
   but aren't on the watchlist show up on `/runs` as proposed sources.
2. **Dedup, layer one.** URLs are normalised (tracking params, `www.`, trailing slashes,
   YouTube variants) and anything seen before, in any topic, is dropped.
3. **Curate.** Claude keeps or rejects each item against the topic lens. It writes a summary in
   its own words, scores importance 1 to 5 and lens relevance 0 to 3, and picks tags from the
   taxonomy. Actual legislation gets a stage and a jurisdiction. Debate about regulation is
   tagged `policy` instead. Recent "Not relevant" flags and upvotes go into the prompt.
4. **Cluster.** Postgres fuzzy title matching finds candidate stories. Claude decides whether
   each item is the same event and whether it adds new facts. New facts become a follow-up post
   with a "New:" line. A rewrite gets folded into the earlier post as another source.
5. **Merge pass.** After clustering, Claude looks over the last 72 hours of stories for ones that
   are really the same event but arrived in different batches, and joins them.
6. **Story tags.** Once a story reaches `STORY_TAG_THRESHOLD` articles (default 3) it gets its
   own tag, like `openai-hugging-face-breach-aug-2026`. Every post in the story carries it, so
   you can follow it or hide it.

The feed stores summaries and links only. It never stores or republishes article text.

## Filtering

Everything lives in the URL, so you can edit it by hand or share it.

| URL | Means |
|---|---|
| `/?t=ai` | only posts tagged `ai` |
| `/?t=-legislation` | hide posts tagged `legislation` |
| `/?t=security&t=uk` | tagged both |
| `/?q=agents` | full-text search, supports `"phrases"` and `-words` |
| `/?t=bauer-relevant&sort=top` | Bauer-relevant, most important first within each day |

Only tags that exist can be used as filters. Unknown ones are ignored and flagged. The search
box suggests tags as you type. Start with `-` to hide one, or press Shift+Enter.

Tag types: `topic` (ai), `category` (from the taxonomy, including the jurisdictions us-federal,
eu, germany and uk), `lens` (bauer-relevant), `story`, `entity` (OpenAI, Hugging Face), and
`user` (anything the team adds).

## Tuning

- `/runs` shows every scan, every item it found, and why each was rejected or folded. Start here
  when the feed feels off.
- `topics/ai/lens.md` is who the feed is for. It changes the curator more than anything else.
- `topics/ai/taxonomy.json` is the tag list the curator may use. Descriptions matter.
- `/sources` is the watchlist: every organisation we follow, every channel we know for each, how
  it's read, whether it's working, and how much it actually yields. It also lists organisations
  the system thinks you should follow. The seed list is `registry/orgs.json`. URLs in it were
  written by hand, so check `/sources` after the first scan. A channel that fails 5 times in a
  row pauses itself.
- `topics/ai/sources.json` holds feeds that belong to the topic rather than an organisation.
- `app/pipeline/prompts/` holds the curator, story editor and discovery prompts.
- Caps: `MAX_LLM_CALLS_PER_RUN`, `MAX_SEARCHES_PER_RUN`, `MAX_CANDIDATES_PER_RUN`. A quiet run
  costs a handful of calls. A busy one hits the cap and logs it rather than overspending.

To add a YouTube channel, Substack, Bluesky profile or newsroom, paste its link on the org's row
on `/sources`. The read mode is worked out from the URL. X accounts are polled only when
`X_BEARER_TOKEN` is set (the X API is pay per use); LinkedIn is always a link, because there is
no public API for company posts.

## Adding a topic

Copy `topics/ai` to `topics/<slug>`, then edit `topic.json`, `lens.md`, `taxonomy.json` and
`sources.json`. Restart. Every post gets its topic tag, so `?t=<slug>` and `?t=-<slug>` work
straight away. An article found by two topics shows once with both tags.

## Moving to Bauer

- **Hosting:** it's three containers and a volume. `docker compose up` on any host.
- **Identity:** swap Cloudflare Access for whatever SSO proxy sits in front. Only
  `app/auth.py` changes.
- **LLM provider:** everything provider-specific is in `app/pipeline/llm.py`. It exposes two
  methods. If another provider is mandated, implement those two.
- **Data:** `docker compose exec db pg_dump -U news news > news.sql` to take the history along.

## Development

```bash
pip install -r requirements-dev.txt
createdb news_test
TEST_DATABASE_URL=postgresql://localhost/news_test pytest
```

The tests run the whole pipeline against local RSS fixtures with the fake curator, then drive
the API. The real Claude wrapper is tested against stubbed SDK responses.

Layout:

```
app/
  web.py          API and pages
  worker.py       scheduler and scan-now requests
  queries.py      feed filters and tag search
  auth.py         Cloudflare Access identity
  pipeline/       collect, curate, cluster, run, llm, prompts/
  migrations/     SQL, applied in order on startup
  sources/        known sources: registry seeding, readers, X, channel discovery, relevance
static/           feed, sources and scan log pages, no build step
registry/         organisations we follow and their channels
topics/ai/        the AI topic
docs/             architecture
```

## Credits

Story clustering rules are adapted from
[open-aggregator](https://github.com/jwmeyert7/open-aggregator) (MIT). Fonts are
Big Shoulders and Atkinson Hyperlegible Next, both under the SIL Open Font License. See
`NOTICE.md`.
