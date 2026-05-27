"""Unit tests for ``morning_signal.search_telemetry``.

Locks down the JSONL contract: one line per ``web_search`` invocation,
queries paired with their result URLs by ``tool_use_id``, and graceful
handling of mixed content blocks. Uses duck-typed fakes (matches
``test_cost_telemetry.py`` pattern) so no real ``anthropic`` types are
constructed.
"""

from __future__ import annotations

import json

from morning_signal.search_telemetry import _extract_searches, record_searches


class _Block:
    """Minimal duck-typed Anthropic content block.

    Any of ``type``, ``name``, ``id``, ``input``, ``tool_use_id``,
    ``content``, ``url`` can be set per-instance to drive a specific
    block shape (text, server_tool_use, web_search_tool_result,
    individual web_search_result).
    """

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Message:
    def __init__(self, content):
        self.content = content


def _server_tool_use(*, block_id: str, query: str) -> _Block:
    return _Block(
        type="server_tool_use",
        name="web_search",
        id=block_id,
        input={"query": query},
    )


def _tool_result(*, tool_use_id: str, urls: list[str]) -> _Block:
    return _Block(
        type="web_search_tool_result",
        tool_use_id=tool_use_id,
        content=[_Block(url=u) for u in urls],
    )


def _tool_result_error(*, tool_use_id: str, error_code: str) -> _Block:
    err_obj = _Block(error_code=error_code)
    return _Block(
        type="web_search_tool_result",
        tool_use_id=tool_use_id,
        content=err_obj,
    )


def test_extracts_query_and_urls_for_each_search():
    msg = _Message([
        _Block(type="text", text="opening..."),
        _server_tool_use(block_id="srv_1", query="S&P 500 close today"),
        _tool_result(tool_use_id="srv_1", urls=[
            "https://cnbc.com/a", "https://reuters.com/b",
        ]),
        _server_tool_use(block_id="srv_2", query="VIX level"),
        _tool_result(tool_use_id="srv_2", urls=["https://bloomberg.com/c"]),
        _Block(type="text", text="...closing"),
    ])

    out = _extract_searches(msg)
    assert len(out) == 2
    assert out[0] == {
        "query": "S&P 500 close today",
        "urls": ["https://cnbc.com/a", "https://reuters.com/b"],
        "result_count": 2,
        "error": None,
    }
    assert out[1] == {
        "query": "VIX level",
        "urls": ["https://bloomberg.com/c"],
        "result_count": 1,
        "error": None,
    }


def test_returns_empty_when_no_web_search_blocks():
    msg = _Message([_Block(type="text", text="no tool use here")])
    assert _extract_searches(msg) == []


def test_handles_search_with_missing_result_block():
    # Anthropic occasionally returns server_tool_use without a matching
    # result block (e.g. truncated response). Should still record the
    # query with empty urls + no error code.
    msg = _Message([
        _server_tool_use(block_id="srv_1", query="orphan query"),
    ])
    out = _extract_searches(msg)
    assert len(out) == 1
    assert out[0]["query"] == "orphan query"
    assert out[0]["urls"] == []
    assert out[0]["result_count"] == 0
    assert out[0]["error"] is None


def test_handles_error_result_block():
    msg = _Message([
        _server_tool_use(block_id="srv_1", query="will fail"),
        _tool_result_error(tool_use_id="srv_1", error_code="max_uses_exceeded"),
    ])
    out = _extract_searches(msg)
    assert len(out) == 1
    assert out[0]["urls"] == []
    assert out[0]["error"] == "max_uses_exceeded"


def test_record_writes_one_line_per_search(tmp_path):
    msg = _Message([
        _server_tool_use(block_id="srv_1", query="q1"),
        _tool_result(tool_use_id="srv_1", urls=["https://a.com/1"]),
        _server_tool_use(block_id="srv_2", query="q2"),
        _tool_result(tool_use_id="srv_2", urls=["https://b.com/2", "https://b.com/3"]),
    ])
    n = record_searches(
        msg=msg, date_str="2026-05-27", edition="am", episodes_dir=tmp_path,
    )
    assert n == 2

    out = tmp_path / "2026-05-27-am.searches.jsonl"
    assert out.exists()
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2

    rec0 = json.loads(lines[0])
    assert rec0["query"] == "q1"
    assert rec0["urls"] == ["https://a.com/1"]
    assert rec0["result_count"] == 1
    assert rec0["date"] == "2026-05-27"
    assert rec0["edition"] == "am"
    assert "ts" in rec0

    rec1 = json.loads(lines[1])
    assert rec1["query"] == "q2"
    assert rec1["result_count"] == 2


def test_record_returns_zero_and_skips_file_when_no_searches(tmp_path):
    msg = _Message([_Block(type="text", text="no searches")])
    n = record_searches(
        msg=msg, date_str="2026-05-27", edition="pm", episodes_dir=tmp_path,
    )
    assert n == 0
    # File must NOT be created when there's nothing to write — keeps
    # the episodes/ dir clean for editions that did no web_search.
    assert not (tmp_path / "2026-05-27-pm.searches.jsonl").exists()


def test_record_creates_episodes_dir_if_absent(tmp_path):
    nested = tmp_path / "fresh-episodes-dir"
    assert not nested.exists()

    msg = _Message([
        _server_tool_use(block_id="srv_1", query="q"),
        _tool_result(tool_use_id="srv_1", urls=["https://a.com"]),
    ])
    record_searches(
        msg=msg, date_str="2026-05-27", edition="am", episodes_dir=nested,
    )
    assert nested.exists()
    assert (nested / "2026-05-27-am.searches.jsonl").exists()


def test_record_appends_subsequent_calls_to_same_file(tmp_path):
    # Forward-compat: per-segment fanout would produce multiple
    # ``messages.create`` calls per (date, edition). All append.
    for i in range(3):
        msg = _Message([
            _server_tool_use(block_id=f"srv_{i}", query=f"q{i}"),
            _tool_result(tool_use_id=f"srv_{i}", urls=[f"https://a.com/{i}"]),
        ])
        record_searches(
            msg=msg, date_str="2026-05-27", edition="am", episodes_dir=tmp_path,
        )

    out = tmp_path / "2026-05-27-am.searches.jsonl"
    assert len(out.read_text().strip().splitlines()) == 3
