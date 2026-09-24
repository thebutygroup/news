from app.normalize import canonical_url, clean_text, jaccard, slugify
from app.queries import parse_tag_params


def test_canonical_url_strips_tracking_and_www():
    a = canonical_url("http://www.Example.com/post/?utm_source=rss&utm_medium=x&id=3#comments")
    assert a == "https://example.com/post?id=3"


def test_canonical_url_keeps_youtube_video_id_only():
    assert canonical_url("https://youtu.be/abc123?t=40") == "https://youtube.com/watch?v=abc123"
    assert canonical_url("https://m.youtube.com/watch?v=abc123&feature=share") == "https://youtube.com/watch?v=abc123"


def test_canonical_url_keeps_hn_item_ids():
    assert canonical_url("https://news.ycombinator.com/item?id=42") == "https://news.ycombinator.com/item?id=42"


def test_clean_text_and_slugify():
    assert clean_text("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"
    assert slugify("OpenAI / Hugging Face breach, Aug 2026") == "openai-hugging-face-breach-aug-2026"


def test_jaccard():
    assert jaccard("Hugging Face confirms breach", "Hugging Face confirms breach") == 1.0
    assert jaccard("apples", "oranges") == 0.0


def test_parse_tag_params():
    inc, exc = parse_tag_params(["ai", "-legislation", "security,-uk", "ai"])
    assert inc == ["ai", "security"]
    assert exc == ["legislation", "uk"]
