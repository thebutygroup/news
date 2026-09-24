"""LLM_FAKE=1 swaps the real model for these heuristics.

Useful for seeing the site work end to end before you add an API key, and for tests.
The output is crude on purpose: keyword matching and title overlap, no judgement.
"""
from __future__ import annotations

import re

from ..normalize import domain, jaccard, slugify

AI_WORDS = re.compile(
    r"\b(ai|a\.i\.|llm|llms|gpt|claude|gemini|llama|mistral|openai|anthropic|deepmind|hugging ?face|"
    r"machine learning|neural|model|models|agent|agents|chatbot|copilot|transformer|diffusion|genai|"
    r"inference|fine-?tun\w*|rag|embedding\w*)\b",
    re.I,
)

CATEGORY_WORDS = {
    "model-release": r"\b(release[sd]?|launch\w*|unveil\w*|introduc\w*|weights|v\d)\b",
    "security": r"\b(breach|hack\w*|vulnerab\w*|leak\w*|exploit\w*|jailbreak\w*|cve|attack\w*|malware)\b",
    "legislation": r"\b(act|bill|law|regulation|regulator|executive order|statute|ofcom|ico)\b",
    "funding": r"\b(raise[sd]?|funding|valuation|acqui\w+|series [a-f]|ipo)\b",
    "enterprise-adoption": r"\b(deploy\w*|rollout|enterprise|customers?|case study|in production)\b",
    "open-source": r"\b(open[- ]source|open weights?|apache|mit licen[cs]e)\b",
    "advertising": r"\b(advertis\w*|adtech|billboard|out-of-home|ooh|dooh|audience)\b",
    "media-and-publishing": r"\b(publisher\w*|newsroom|copyright|licensing deal|journalis\w*)\b",
}


class FakeLLM:
    def __init__(self):
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "web_searches": 0}

    def structured(self, *, model, system, payload, tool_name, schema, max_tokens=16000):
        self.usage["calls"] += 1
        if tool_name == "curate_results":
            return self._curate(payload)
        if tool_name == "cluster_results":
            return self._cluster(payload)
        raise ValueError(tool_name)

    def search_and_report(self, *, model, system, prompt, schema, max_searches):
        self.usage["calls"] += 1
        return {"items": []}, set()

    # -- heuristics --------------------------------------------------------
    def _curate(self, payload):
        allowed = {t["slug"] for t in payload["taxonomy"]}
        results = []
        for item in payload["items"]:
            text = f"{item['title']} {item.get('excerpt') or ''}"
            keep = bool(AI_WORDS.search(text))
            cats = [c for c, rx in CATEGORY_WORDS.items() if c in allowed and re.search(rx, text, re.I)]
            host = domain(item["url"])
            ctype = (
                "video" if "youtube" in host else
                "paper" if "arxiv" in host else
                "repo" if "github" in host else
                "discussion" if host in {"news.ycombinator.com", "reddit.com"} else
                "article"
            )
            results.append({
                "id": item["id"],
                "keep": keep,
                "reason": "mentions AI" if keep else "no AI signal in title or excerpt",
                "importance": 3 if "model-release" in cats or "security" in cats else 2,
                "lens_score": 2 if "advertising" in cats or "media-and-publishing" in cats else 0,
                "summary": (item.get("excerpt") or item["title"])[:280],
                "content_type": ctype,
                "categories": cats[:3],
                "entities": [],
                "legislation_stage": "proposed" if "legislation" in cats else None,
                "jurisdiction": None,
            })
        return {"results": results}

    def _cluster(self, payload):
        stories = payload["active_stories"]
        assignments, new_stories = [], []
        for item in payload["items"]:
            best, best_score = None, 0.0
            for s in stories:
                score = jaccard(item["title"], s["headline"])
                if score > best_score:
                    best, best_score = s, score
            for ns in new_stories:
                score = jaccard(item["title"], ns["headline"])
                if score > best_score:
                    best, best_score = {"id": ns["ref"], "headline": ns["headline"]}, score
            if best is not None and best_score >= 0.45:
                assignments.append({
                    "id": item["id"],
                    "story": str(best["id"]),
                    "new_information": best_score < 0.7,
                    "delta": f"Follow-up: {item['title']}" if best_score < 0.7 else None,
                })
            else:
                ref = f"new:{len(new_stories) + 1}"
                new_stories.append({
                    "ref": ref,
                    "headline": item["title"],
                    "keywords": sorted(set(re.findall(r"[a-z0-9]{4,}", item["title"].lower())))[:6],
                    "tag_slug": slugify(" ".join(item["title"].split()[:5])),
                })
                assignments.append({"id": item["id"], "story": ref, "new_information": True, "delta": None})
        return {"assignments": assignments, "new_stories": new_stories}
