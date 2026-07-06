"""Unit tests for the OpenRouter/citation re-keying of the coverage guards
(config#1659 Phase B).

The three production incident-guards originally keyed on Anthropic per-query
``web_search`` events. The OpenRouter web-search server tool does not expose
query text — only ``url_citation`` annotations + a ``usage.web_search_requests``
billing counter. These tests lock down that both guards behave byte-identically
on the Anthropic path (searches populated, citations empty) AND work on the
OpenRouter path (searches empty, citations populated), so the ratified flip to
``openrouter:moonshotai/kimi-k2.6`` does not silently disarm them.

Duck-typed fakes throughout (matches the repo's no-real-SDK-types convention).
"""

from __future__ import annotations

from types import SimpleNamespace

from morning_signal.search_telemetry import (
    grounded_search_count,
    unmet_required_topics,
)


def _search(query: str) -> dict:
    """An Anthropic-style extracted search event."""
    return {"query": query, "urls": [], "result_count": 0, "error": None}


def _citation(url: str, title: str | None = None, snippet: str | None = None) -> dict:
    """An OpenRouter ``url_citation`` extracted into a krepis Citation."""
    return {"url": url, "title": title, "snippet": snippet}


def _result(*, searches=None, citations=None, web_search_requests=0):
    """Duck-typed krepis GroundedResult (only the fields the guards read)."""
    return SimpleNamespace(
        searches=list(searches or []),
        citations=list(citations or []),
        usage=SimpleNamespace(web_search_requests=web_search_requests),
    )


# ── grounded_search_count (the min_web_searches floor input) ──────────────────

def test_count_uses_usage_counter_when_present():
    # OpenRouter: searches empty, but the billing counter is the truth.
    r = _result(searches=[], citations=[_citation("https://x")], web_search_requests=3)
    assert grounded_search_count(r) == 3


def test_count_falls_back_to_searches_when_counter_zero():
    # Older Anthropic path that didn't thread web_search_requests — preserve
    # the pre-Phase-B behavior exactly (len of per-query events).
    r = _result(searches=[_search("a"), _search("b")], web_search_requests=0)
    assert grounded_search_count(r) == 2


def test_count_prefers_counter_over_searches_when_both_present():
    # Anthropic with the counter threaded: counter wins (they agree in
    # practice; the counter is the canonical, transport-agnostic signal).
    r = _result(searches=[_search("a")], web_search_requests=5)
    assert grounded_search_count(r) == 5


def test_count_zero_when_no_search_at_all():
    assert grounded_search_count(_result()) == 0


def test_count_handles_missing_usage_gracefully():
    r = SimpleNamespace(searches=[_search("a")], citations=[])
    assert grounded_search_count(r) == 1


# ── unmet_required_topics: OpenRouter citation path ───────────────────────────

def test_topic_covered_by_citation_url():
    # Keyword hits the citation TITLE (the space-keyword "truth social" appears
    # in "Truth Social post ...").
    topics = [{"name": "Political pulse", "keywords": ["truth social", "maga"]}]
    citations = [
        _citation("https://truthsocial.com/@x/posts/123", title="Truth Social post by X")
    ]
    script = "Welcome to Morning Signal. On the political pulse, Truth Social..."
    assert unmet_required_topics(
        [], topics, script=script, citations=citations
    ) == []


def test_url_only_match_needs_url_oriented_keyword_variant():
    # Documents the issue#1659 caveat: a space-keyword ("truth social") does
    # NOT match a spaceless URL host ("truthsocial.com"). Operators re-keying
    # for OpenRouter must add URL-oriented keyword variants. With ONLY the URL
    # to match against (bare title), the space-keyword misses; the spaceless
    # variant "truthsocial" hits.
    citations = [_citation("https://truthsocial.com/@x/posts/123", title="Post")]
    # script=None isolates the "searched" dimension (condition 1 only).
    space_kw = [{"name": "T", "keywords": ["truth social"]}]
    url_kw = [{"name": "T", "keywords": ["truthsocial"]}]
    assert unmet_required_topics([], space_kw, citations=citations) == ["T"]
    assert unmet_required_topics([], url_kw, citations=citations) == []


def test_topic_covered_by_citation_title_or_snippet():
    topics = [{"name": "Seattle", "keywords": ["seattle"]}]
    citations = [_citation("https://news.example/story", title="Storm hits Seattle")]
    script = "Welcome to Morning Signal. In Seattle, a storm..."
    assert unmet_required_topics(
        [], topics, script=script, citations=citations
    ) == []
    # snippet-only match also counts
    cit2 = [_citation("https://news.example/y", snippet="rain across Seattle today")]
    assert unmet_required_topics([], topics, script=script, citations=cit2) == []


def test_topic_unmet_when_no_citation_matches_openrouter():
    topics = [{"name": "Political pulse", "keywords": ["truth social"]}]
    citations = [_citation("https://markets.example/tesla", title="Tesla earnings")]
    script = "Welcome to Morning Signal. Markets and Truth Social both discussed."
    # No citation grounds the topic even though the keyword aired → unmet.
    assert unmet_required_topics(
        [], topics, script=script, citations=citations
    ) == ["Political pulse"]


def test_min_matches_across_citations():
    topics = [{"name": "Politics", "keywords": ["trump"], "min_matches": 2}]
    script = "Welcome. Trump news today."
    one = [_citation("https://a", title="Trump A")]
    two = [_citation("https://a", title="Trump A"), _citation("https://b", snippet="trump b")]
    assert unmet_required_topics([], topics, script=script, citations=one) == ["Politics"]
    assert unmet_required_topics([], topics, script=script, citations=two) == []


def test_citation_search_still_requires_aired_segment():
    # The 2026-06-29 blind-spot invariant holds on OpenRouter too: a citation
    # grounding a topic does NOT cover it if the segment never aired.
    topics = [{"name": "Techno-MAGA", "keywords": ["david sacks"]}]
    citations = [_citation("https://x.com/davidsacks", title="David Sacks on AI")]
    script = "Welcome to Morning Signal. Markets mixed. Seattle weather."
    assert unmet_required_topics(
        [], topics, script=script, citations=citations
    ) == ["Techno-MAGA"]


# ── Anthropic path is byte-identical (citations default None/empty) ───────────

def test_anthropic_path_unchanged_without_citations():
    topics = [{"name": "Politics", "keywords": ["trump"]}]
    searches = [_search("trump truth social today")]
    script = "Welcome. On Trump, he posted today."
    # No citations arg → original query-only behavior.
    assert unmet_required_topics(searches, topics, script=script) == []
    assert unmet_required_topics(searches, topics, script=script, citations=[]) == []


def test_mixed_transport_sources_are_additive():
    # Defensive: if a call ever surfaced both, query + citation matches sum.
    topics = [{"name": "Politics", "keywords": ["trump"], "min_matches": 2}]
    searches = [_search("trump rally")]
    citations = [_citation("https://a", title="Trump op-ed")]
    script = "Welcome. Trump segment aired."
    assert unmet_required_topics(
        searches, topics, script=script, citations=citations
    ) == []


def test_edition_scope_and_empty_keywords_still_respected():
    # Re-keying must not disturb the edition filter / empty-keyword skip.
    topics = [
        {"name": "wknd-only", "keywords": ["nfl"], "editions": ["weekend"]},
        {"name": "no-keywords", "keywords": []},
    ]
    citations = [_citation("https://espn.com/nfl", title="NFL scores")]
    # 'am' edition → weekend-scoped topic is skipped; empty-keyword skipped.
    assert unmet_required_topics(
        [], topics, edition="am", script="Welcome.", citations=citations
    ) == []
