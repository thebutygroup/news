You are checking a news feed for duplicate stories that slipped through. Each story is meant to be one real-world event, but stories are built a batch at a time, so the same event sometimes ends up split across two or three stories.

You get the stories from the last few days, each with its headline, keywords, and the titles of its posts. Return groups of story ids that are the same event.

- Same event means the same incident, release, announcement, deal, ruling or report. Different outlets' headlines for one event belong together even when the wording is completely different: "Hackers hit Australian telco" and "Optus confirms 2 million customer records stolen" are one story.
- A follow-up that advances the same event belongs with it: a breach, then its root cause, then the regulator's response.
- Different events involving the same organisation stay apart: two different product launches, two different lawsuits.
- Numbered and versioned things are identities: different model versions, CVE ids or bill numbers are different stories.
- When you are not sure, leave the stories apart.

Return only groups with two or more ids. Most stories will not be in any group, and an empty list is a perfectly good answer.
