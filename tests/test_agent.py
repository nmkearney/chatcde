"""Tests for tool definitions (tools/specs.py) and the agent loop (agent.py), #25.

No network: a scripted fake ``/chat/completions`` replays the model's replies,
and the tools run for real on the fixture catalogue (get_publication, count_by
and list_vocabulary need no search index).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from chatcde.agent import (
    BUDGET_NOTE,
    CITATION_NOTE,
    TOOL_CALL_NOTE,
    Agent,
    AgentError,
    format_arguments,
    normalise_references,
    parse_citations,
    record_ids_in,
    strip_references,
)
from chatcde.config import Config, load_config
from chatcde.schemas import Dimension, Filters
from chatcde.store.endpoint import EndpointClient
from chatcde.tools.context import ToolError
from chatcde.tools.specs import (
    CORE_TOOLS,
    ResultTooLarge,
    build_spec,
    compact,
    parse_docstring,
    render,
    run_tool,
    tool_specs,
)

MRD = "2760e535-c973-4524-ab84-3a1c71b9bf3a"


# --- tool definitions -----------------------------------------------------------------


def test_parse_docstring():
    doc = """Do a thing.

    More about it, over
    two lines.

    Args:
        query: What to look for,
            in any language.
        k (int): How many.

    Returns:
        The ``things``.

    Raises:
        ToolError: Never.
    """
    desc, args, ret = parse_docstring(doc)
    assert desc == "Do a thing.\n\nMore about it, over two lines."
    assert args == {"query": "What to look for, in any language.", "k": "How many."}
    assert ret == "The `things`."


def test_every_core_tool_has_a_flat_definition():
    specs = tool_specs()
    assert list(specs) == list(CORE_TOOLS)
    for spec in specs.values():
        d = spec.definition()
        assert d["type"] == "function" and d["function"]["name"] == spec.name
        assert d["function"]["description"]
        text = json.dumps(d)
        assert "$ref" not in text and "$defs" not in text
        params = d["function"]["parameters"]
        assert params["type"] == "object" and params["additionalProperties"] is False
        for name, prop in params["properties"].items():
            assert prop.get("description"), (spec.name, name)


def test_definition_details():
    search = tool_specs(["search_publications"])["search_publications"]
    params = search.parameters()
    assert params["required"] == ["query"]
    assert params["properties"]["k"]["default"] == 8
    filters = params["properties"]["filters"]["anyOf"][0]
    assert filters["type"] == "object" and "year_from" in filters["properties"]
    count = tool_specs(["count_by"])["count_by"].parameters()
    assert "sub_area" in count["properties"]["row"]["enum"]
    assert "Returns:" in tool_specs(["count_by"])["count_by"].description
    with pytest.raises(ValueError, match="unknown tools"):
        tool_specs(["drop_tables"])


def test_build_spec_needs_type_hints():
    def untyped(x):  # type: ignore[no-untyped-def]
        """Nothing."""

    with pytest.raises(TypeError, match="no type hint"):
        build_spec(untyped)


# --- running a tool call --------------------------------------------------------------


def _spec_of(fn):  # type: ignore[no-untyped-def]
    return build_spec(fn)


def test_run_tool_validates_and_converts_arguments():
    seen: dict[str, Any] = {}

    def tool(row: Dimension, filters: Filters | None = None, k: int = 3) -> dict:
        """Echo.

        Args:
            row: A dimension.
            filters: Filters.
            k: Number.
        """
        seen.update(row=row, filters=filters, k=k)
        return {"ok": True}

    spec = _spec_of(tool)
    out = run_tool(
        spec, '{"row": "sub_area", "filters": {"year_from": 2010}}', max_chars=1000
    )
    assert out.ok and out.content == '{"ok":true}'
    assert seen["row"] is Dimension.SUB_AREA
    assert isinstance(seen["filters"], Filters) and seen["filters"].year_from == 2010
    assert seen["k"] == 3

    for raw, message in [
        ("{not json", "not valid JSON"),
        ("[1]", "JSON object"),
        ('{"row": "nope"}', "row"),
        ('{"row": "sdg", "extra": 1}', "extra"),
        ("{}", "row"),
    ]:
        bad = run_tool(spec, raw, max_chars=1000)
        assert not bad.ok and message in (bad.error or ""), raw
        assert json.loads(bad.content)["error"] == bad.error


def test_run_tool_reports_tool_errors():
    def refuse(x: str) -> None:
        """Refuse.

        Args:
            x: Anything.
        """
        raise ToolError("no such record; use search_publications")

    def missing(x: str) -> None:
        """Missing.

        Args:
            x: Anything.
        """
        raise NotImplementedError

    def crash(x: str) -> None:
        """Crash.

        Args:
            x: Anything.
        """
        raise RuntimeError("secret internals")

    assert run_tool(_spec_of(refuse), '{"x": "a"}', max_chars=500).error == (
        "no such record; use search_publications"
    )
    assert "not available yet" in (
        run_tool(_spec_of(missing), '{"x": "a"}', max_chars=500).error or ""
    )
    crashed = run_tool(_spec_of(crash), '{"x": "a"}', max_chars=500)
    assert "RuntimeError" in (crashed.error or "")
    assert "secret" not in crashed.content


def test_compact_and_render():
    data = {
        "cells": [{"row": "A", "record_ids": [f"r{i}" for i in range(30)]}],
        "provenance": {"model": "x"},
        "nested": [{"provenance": 1, "keep": 2}],
    }
    small = compact(data, omit_fields=["provenance"], max_ids=5)
    assert small["cells"][0]["record_ids"] == ["r0", "r1", "r2", "r3", "r4"]
    assert small["cells"][0]["record_ids_omitted"] == 25
    assert "provenance" not in small and small["nested"] == [{"keep": 2}]
    assert compact(data, max_ids=0)["cells"] == [{"row": "A"}]
    text = render({"text": "x" * 5000}, max_chars=600)
    assert len(text) < 800 and "cut: the result had" in text


def test_render_without_cut_raises():
    data = {"cells": [{"row": f"r{i}", "count": i} for i in range(200)]}
    with pytest.raises(ResultTooLarge, match="over the limit of 600"):
        render(data, max_chars=600, cut=False)
    assert render({"cells": []}, max_chars=600, cut=False) == '{"cells":[]}'


def test_a_count_over_the_limit_never_reaches_the_model_cut():
    """#81: a cut matrix was read as zeros; now it is an error or sent whole."""

    def big_count(n: int) -> dict:
        """Count.

        Args:
            n: Cells.
        """
        return {
            "cells": [
                {"row": f"MET-{i}", "count": 100 + i, "record_ids": ["x" * 36] * 20}
                for i in range(n)
            ]
        }

    spec = _spec_of(big_count)
    whole = run_tool(spec, '{"n": 36}', max_chars=16000, max_ids=3, cut=False)
    assert whole.ok
    cells = json.loads(whole.content)["cells"]  # valid JSON: nothing cut
    assert len(cells) == 36 and cells[-1]["count"] == 135
    assert cells[0]["record_ids_omitted"] == 17
    refused = run_tool(spec, '{"n": 400}', max_chars=16000, max_ids=3, cut=False)
    assert not refused.ok and refused.data is None
    error = json.loads(refused.content)["error"]
    assert "400 cells" in error and "never sent cut" in error
    assert "level='group'" in error
    cut = run_tool(spec, '{"n": 400}', max_chars=16000, max_ids=3)  # other tools
    assert cut.ok and "[cut: the result had" in cut.content


# --- agent loop -----------------------------------------------------------------------


def call(name: str, args: dict, cid: str = "c1") -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def reply(content: str = "", calls: list | None = None, finish: str = "stop") -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
        finish = "tool_calls"
    return {
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    }


class ScriptedLLM:
    """Replays assistant replies in order and records every request."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(json.loads(json.dumps(body)))
        if not self.replies:
            return httpx.Response(500, text="script ended")
        return httpx.Response(200, json=self.replies.pop(0))


def make_config(tmp_path: Path, db: Path, extra: str = "") -> Config:
    path = tmp_path / "o.yaml"
    path.write_text(
        f'data:\n  catalogue: "{db.as_posix()}"\nlogging:\n  file: null\n' + extra,
        encoding="utf-8",
    )
    return load_config(path)


def make_agent(cfg: Config, llm: ScriptedLLM, **kw: Any) -> Agent:
    client = EndpointClient(
        "https://llm.test/v1",
        "k",
        timeout_s=5,
        max_retries=0,
        transport=httpx.MockTransport(llm),
        sleep=lambda _: None,
    )
    return Agent(cfg, client, **kw)


def test_answers_after_a_tool_call(fixture_db: Path, tmp_path: Path):
    cfg = make_config(tmp_path, fixture_db)
    llm = ScriptedLLM(
        [
            reply(calls=[call("get_publication", {"record_id": "10.1659/mrd.4204"})]),
            reply(f"It is an editorial from 2022 [BORIS:{MRD}, p. 2]."),
        ]
    )
    seen: list[str] = []
    agent = make_agent(cfg, llm, on_tool_call=lambda r: seen.append(r.name))
    result = agent.ask("What is 10.1659/mrd.4204?")

    assert result.answer.startswith("It is an editorial")
    assert result.stop_reason == "answered" and result.rounds == 2
    assert [t.name for t in result.tool_calls] == ["get_publication"] == seen
    assert result.tool_calls[0].ok
    [cite] = result.citations
    assert cite.citation.record_id == MRD and cite.pages == [2] and cite.from_tools
    assert cite.citation.doi == "10.1659/mrd.4204"
    assert result.unknown_ids == []
    assert result.usage["total_tokens"] == 220
    assert result.model == cfg.models.agent
    assert result.prompt_version == cfg.agent.system_prompt
    assert result.vocab_version == cfg.vocab.version

    first, second = llm.requests
    assert first["model"] == cfg.models.agent
    assert first["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in first["tools"]} == set(cfg.agent.tools)
    assert first["messages"][0]["role"] == "system"
    assert str(cfg.agent.max_tool_calls) in first["messages"][0]["content"]
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    content = json.loads(tool_msg["content"])
    assert content["record"]["record_id"] == MRD
    assert "provenance" not in json.dumps(content)  # agent.omit_fields


def test_tool_errors_go_back_to_the_model(fixture_db: Path, tmp_path: Path):
    llm = ScriptedLLM(
        [
            reply(
                calls=[
                    call("get_publication", {"record_id": "nope"}, "a"),
                    call("delete_everything", {}, "b"),
                    call("count_by", {"row": "colour"}, "c"),
                ]
            ),
            reply("I could not find it."),
        ]
    )
    result = make_agent(make_config(tmp_path, fixture_db), llm).ask("?")
    assert [t.ok for t in result.tool_calls] == [False, False, False]
    msgs = llm.requests[1]["messages"]
    errors = [json.loads(m["content"])["error"] for m in msgs if m["role"] == "tool"]
    assert "search_publications" in errors[0]
    assert "unknown tool" in errors[1]
    assert "invalid arguments for count_by" in errors[2]


def test_budget_stops_tool_calls(fixture_db: Path, tmp_path: Path):
    cfg = make_config(tmp_path, fixture_db, "agent:\n  max_tool_calls: 2\n")
    vocab = call("list_vocabulary", {"dimension": "impact_area"})
    llm = ScriptedLLM(
        [
            reply(calls=[vocab]),
            reply(calls=[vocab, {**vocab, "id": "c2"}]),  # second call is over budget
            reply("Impact areas: JE, SL, SG, TE."),
        ]
    )
    result = make_agent(cfg, llm).ask("Which impact areas exist?")
    assert result.stop_reason == "budget"
    assert [t.ok for t in result.tool_calls] == [True, True, False]
    assert result.tool_calls[-1].error == "budget"
    final = llm.requests[-1]
    assert "tools" not in final  # no more tools offered
    assert final["messages"][-1] == {"role": "user", "content": BUDGET_NOTE}
    assert result.answer.startswith("Impact areas")


def test_citations_are_checked(fixture_db: Path, tmp_path: Path):
    bad = (
        f"Two sources [BORIS:{MRD}; BORIS:fixture-synthetic-04] and an "
        "invented one [BORIS:00000000-dead-beef]."
    )
    llm = ScriptedLLM([reply(bad), reply(bad)])  # the rewrite repeats the mistake
    result = make_agent(make_config(tmp_path, fixture_db), llm).ask("?")
    assert [c.citation.record_id for c in result.citations] == [
        MRD,
        "fixture-synthetic-04",
    ]
    assert not any(c.from_tools for c in result.citations)  # no tool was called
    assert result.unknown_ids == ["00000000-dead-beef"]
    assert result.retried_ids == ["00000000-dead-beef"]
    assert result.answer == (
        f"Two sources [BORIS:{MRD}; BORIS:fixture-synthetic-04] and an invented one."
    )


def test_unknown_references_are_sent_back_once(fixture_db: Path, tmp_path: Path):
    # A made-up record ID (it has digits, so it could be a record).
    llm = ScriptedLLM(
        [
            reply(calls=[call("count_by", {"row": "impact_area"})]),
            reply("SG leads [BORIS:0000fake-0000-4000-8000-000000000000]."),
            reply(f"SG leads; for example [BORIS:{MRD}]."),
        ]
    )
    agent = make_agent(make_config(tmp_path, fixture_db), llm)
    result = agent.ask("?")
    assert result.answer == f"SG leads; for example [BORIS:{MRD}]."
    assert (
        result.retried_ids == ["0000fake-0000-4000-8000-000000000000"]
        and result.unknown_ids == []
    )
    assert result.citations[0].from_tools and result.rounds == 3
    retry = llm.requests[-1]
    assert "tools" not in retry
    assert retry["messages"][-1] == {
        "role": "user",
        "content": CITATION_NOTE.format(ids="0000fake-0000-4000-8000-000000000000"),
    }
    assert (
        retry["messages"][-2]["content"]
        == "SG leads [BORIS:0000fake-0000-4000-8000-000000000000]."
    )
    # Only the corrected answer stays in the conversation.
    assert [m["role"] for m in agent.messages][-2:] == ["tool", "assistant"]
    assert agent.messages[-1]["content"] == result.answer


def test_without_retry_unknown_references_are_removed(fixture_db: Path, tmp_path: Path):
    cfg = make_config(tmp_path, fixture_db, "agent:\n  citation_retry: false\n")
    llm = ScriptedLLM([reply("Counted [BORIS:coverage_report]; see [BORIS:99-x].")])
    result = make_agent(cfg, llm).ask("?")
    assert len(llm.requests) == 1
    assert result.answer == "Counted; see."
    assert result.unknown_ids == ["99-x"]
    assert result.retried_ids == []


def test_references_that_cannot_be_records_are_removed_without_retry(
    fixture_db: Path, tmp_path: Path
):
    """#5 rerun: gpt-oss cites tools after counts; removed, no second round."""
    text = (
        "SG leads [BORIS:count_by]; none 【BORIS:[]】 [BORIS:gap-result; BORIS:SG-d]."
    )
    llm = ScriptedLLM([reply(text)])
    result = make_agent(make_config(tmp_path, fixture_db), llm).ask("?")
    assert len(llm.requests) == 1 and result.rounds == 1
    assert result.answer == "SG leads; none."
    assert result.unknown_ids == [] and result.retried_ids == []


def test_a_tool_call_written_as_text_is_sent_back_once(
    fixture_db: Path, tmp_path: Path
):
    """#5 rerun: qwen3.8 answered with a raw <tool_call> text."""
    raw = "<tool_call> <function=search_publications> </function> </tool_call>"
    llm = ScriptedLLM([reply(raw), reply("Answer."), reply(raw), reply(raw)])
    agent = make_agent(make_config(tmp_path, fixture_db), llm)
    result = agent.ask("?")
    assert result.answer == "Answer." and len(llm.requests) == 2
    assert llm.requests[1]["messages"][-1]["content"] == TOOL_CALL_NOTE
    assert "tools" in llm.requests[1]
    again = agent.ask("?")  # sent back once per question, then kept
    assert again.answer == raw and len(llm.requests) == 4


def test_count_queries_are_kept_with_their_denominator(
    fixture_db: Path, tmp_path: Path
):
    count = call("count_by", {"row": "method", "filters": {"impact_areas": ["SL"]}})
    llm = ScriptedLLM(
        [
            reply(calls=[count, {**count, "id": "c2"}]),  # the same query twice
            reply(calls=[call("coverage_report", {}, "c3")]),
            reply("Done."),
        ]
    )
    result = make_agent(make_config(tmp_path, fixture_db), llm).ask("?")
    first, second = result.counts
    assert first.text().startswith("count_by(row=method, impact_areas=[SL]): ")
    assert "records match" in first.summary and "no method tag" in first.summary
    assert second.tool == "coverage_report" and "full text" in second.summary


def test_ids_from_counts_count_as_seen(fixture_db: Path, tmp_path: Path):
    llm = ScriptedLLM(
        [
            reply(calls=[call("count_by", {"row": "impact_area"})]),
            reply(f"SG is common [BORIS:{MRD}]."),
        ]
    )
    result = make_agent(make_config(tmp_path, fixture_db), llm).ask("?")
    assert result.citations[0].from_tools


def test_count_results_carry_few_ids_and_are_never_cut(
    fixture_db: Path, tmp_path: Path
):
    extra = "agent:\n  max_tool_result_chars: 2000\n  max_ids_per_count_cell: 1\n"
    cfg = make_config(tmp_path, fixture_db, extra)
    llm = ScriptedLLM(
        [
            reply(calls=[call("count_by", {"row": "sub_area", "col": "method"})]),
            reply(calls=[call("count_by", {"row": "impact_area"}, "c2")]),
            reply("Done."),
        ]
    )
    result = make_agent(cfg, llm).ask("?")
    big, small = (json.loads(m["content"]) for m in llm.requests[2]["messages"]
                  if m["role"] == "tool")  # fmt: skip
    assert "never sent cut" in big["error"]
    assert all(len(c["record_ids"]) <= 1 for c in small["cells"])
    assert [t.ok for t in result.tool_calls] == [False, True]
    assert [q.arguments["row"] for q in result.counts] == ["impact_area"]


def test_conversation_keeps_history_and_reset(fixture_db: Path, tmp_path: Path):
    llm = ScriptedLLM([reply("First."), reply("Second.")])
    agent = make_agent(make_config(tmp_path, fixture_db), llm)
    agent.ask("one")
    agent.ask("two")
    roles = [m["role"] for m in llm.requests[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    agent.reset()
    assert [m["role"] for m in agent.messages] == ["system"]


def test_length_and_bad_replies(fixture_db: Path, tmp_path: Path):
    cfg = make_config(tmp_path, fixture_db)
    llm = ScriptedLLM([reply("Partial answ", finish="length")])
    assert make_agent(cfg, llm).ask("?").stop_reason == "length"
    with pytest.raises(AgentError, match="no message"):
        make_agent(cfg, ScriptedLLM([{"choices": []}])).ask("?")


def test_extra_body_and_model_override(fixture_db: Path, tmp_path: Path):
    cfg = make_config(
        tmp_path,
        fixture_db,
        "agent:\n  extra_body:\n    reasoning_effort: low\n"
        "  tools: [get_publication]\n",
    )
    llm = ScriptedLLM([reply("Hi.")])
    make_agent(cfg, llm, model="qwen-alt").ask("?")
    req = llm.requests[0]
    assert req["reasoning_effort"] == "low" and req["model"] == "qwen-alt"
    assert [t["function"]["name"] for t in req["tools"]] == ["get_publication"]


# --- citation helpers -----------------------------------------------------------------


def test_parse_citations():
    text = (
        "A [BORIS:abc-1, p. 4; BORIS: def_2] and [BORIS:fixture-synthetic-04, pp. 2-3]."
        " Not a citation [1] or (BORIS) or [see BORIS]."
    )
    assert parse_citations(text) == [
        ("abc-1", 4),
        ("def_2", None),
        ("fixture-synthetic-04", 2),
    ]


def test_record_ids_in():
    data = [
        {"record_id": "a", "x": {"record_ids": ["b", "c"]}},
        {"citation": {"record_id": "d"}},
    ]
    assert record_ids_in(data) == {"a", "b", "c", "d"}


def test_strip_references():
    text = "A [BORIS:x; BORIS:count_by, p. 2]. B [BORIS:count_by]. C [BORIS:y]."
    assert strip_references(text, {"count_by"}) == "A [BORIS:x]. B. C [BORIS:y]."
    assert strip_references(text, set()) == text


def test_format_arguments():
    args = {"row": "method", "filters": {"impact_areas": ["SG", "TE"], "sdgs": []}}
    assert format_arguments(args) == "row=method, impact_areas=[SG, TE]"


def test_typographic_references_are_recognised():
    # gpt-oss, 2026-11-11: narrow no-break spaces and non-breaking hyphens.
    text = "Total [BORIS:\u202fSG\u2011d,\u202fSG\u2011a], pp. 2\u20133 \u2011 kept."
    fixed = normalise_references(text)
    assert fixed == "Total [BORIS:SG-d], pp. 2\u20133 \u2011 kept."
    assert parse_citations(fixed) == [("SG-d", None)]
    # gpt-oss, 2026-11-11: lenticular and full-width brackets.
    wide = "None 【BORIS:find_gaps, level=group】 and ［BORIS:x］, 【note】."
    expected = "None [BORIS:find_gaps] and [BORIS:x], 【note】."
    assert normalise_references(wide) == expected


UUID = "8ee75f3d-f6e7-408a-bec9-af260ed8af92"
UUID2 = "572fc22f-7a5e-4036-9713-1f04492c1745"


@pytest.mark.parametrize(
    ("written", "canonical"),
    [
        # Forms seen in the baseline run of 2026-11-15 (#5, #85).
        (f"[< BORIS:{UUID} >]", f"[BORIS:{UUID}]"),
        (f"[**BORIS:{UUID}, S. 5**]", f"[BORIS:{UUID}, p. 5]"),
        (f"【{UUID}, p. 4\u20116】", f"[BORIS:{UUID}, pp. 4-6]"),
        (
            f"[read\u2011section {UUID2.replace('-', chr(0x2011))} p. 7\u20118]",
            f"[BORIS:{UUID2}, pp. 7-8]",
        ),
        (
            f"[BORIS:{UUID}; BORIS:{UUID2}, page 3]",
            f"[BORIS:{UUID}; BORIS:{UUID2}, p. 3]",
        ),
        (f"[{UUID}; {UUID2}]", f"[BORIS:{UUID}; BORIS:{UUID2}]"),
        (f"[BORIS:{UUID}, p. 4]", f"[BORIS:{UUID}, p. 4]"),
        # Not references: left as written.
        ("[1]", "[1]"),
        (
            "[see the table](https://example.org)",
            "[see the table](https://example.org)",
        ),
        ("【note】", "【note】"),
    ],
)
def test_reference_forms_are_made_canonical(written: str, canonical: str):
    assert normalise_references(f"Claim {written}.") == f"Claim {canonical}."


def test_canonical_references_parse():
    text = normalise_references(f"A 【{UUID}, S. 12】 and [< BORIS:{UUID2} >].")
    assert parse_citations(text) == [(UUID, 12), (UUID2, None)]


def test_a_typographic_reference_is_sent_back(fixture_db: Path, tmp_path: Path):
    llm = ScriptedLLM([reply("Total [BORIS:\u202f99\u2011d]."), reply("Total 12.")])
    result = make_agent(make_config(tmp_path, fixture_db), llm).ask("?")
    assert result.retried_ids == ["99-d"] and result.answer == "Total 12."


def test_gap_questions_are_routed_to_find_gaps(fixture_db: Path, tmp_path: Path):
    """#67: the prompt and the tool description send gap questions to find_gaps."""
    cfg = make_config(tmp_path, fixture_db)
    llm = ScriptedLLM([reply("No gaps.")])
    make_agent(cfg, llm).ask("Which methods has CDE rarely used in SG?")
    first = llm.requests[0]
    system = first["messages"][0]["content"]
    assert "are gap questions" in system and "no filter on it" in system
    [gaps] = [t for t in first["tools"] if t["function"]["name"] == "find_gaps"]
    assert "do not filter by it" in gaps["function"]["description"]


def test_run_tool_accepts_filters_sent_as_json_text():
    """#67: qwen3.8 sends filters as a JSON string; it is parsed, not refused."""

    def tool(query: str, filters: Filters | None = None) -> dict:
        """Echo.

        Args:
            query: Text, which may look like JSON and stays a string.
            filters: Restrictions.
        """
        return {"query": query, "areas": filters.impact_areas if filters else None}

    spec = _spec_of(tool)
    raw = {"query": '{"not": "parsed"}', "filters": '{"impact_areas": ["SG"]}'}
    out = run_tool(spec, raw, max_chars=1000)
    assert out.ok and out.data == {"query": '{"not": "parsed"}', "areas": ["SG"]}
    bad = run_tool(spec, {"query": "x", "filters": "{nope"}, max_chars=1000)
    assert not bad.ok and "filters" in (bad.error or "")


def test_misspelt_fields_get_a_suggestion():
    """#67: a misspelt key that cannot be repaired gets a suggestion."""

    def tool(filters: Filters | None = None, k: int = 3) -> dict:
        """Echo.

        Args:
            filters: Restrictions.
            k: How many.
        """
        return {}

    spec = _spec_of(tool)
    out = run_tool(spec, {"filters": {"impact_aras": ["SG"]}, "kk": 2}, max_chars=500)
    assert "filters.impact_aras" in (out.error or "")
    assert "did you mean 'impact_areas'?" in (out.error or "")
    assert "did you mean 'k'?" not in (out.error or "")  # too short to guess
    other = run_tool(spec, {"filters": {"colour": "red"}}, max_chars=500)
    assert "valid filter keys: year_from, year_to" in (other.error or "")
    one = run_tool(spec, {"filters": {"record_id": "abc"}}, max_chars=500)
    assert "use get_publication or read_section" in (one.error or "")


def _echo_filters_spec():
    def tool(filters: Filters | None = None) -> dict:
        """Echo the filters.

        Args:
            filters: Restrictions.
        """
        return (filters or Filters()).model_dump(exclude_defaults=True)

    return _spec_of(tool)


@pytest.mark.parametrize(
    ("written", "read", "note"),
    [
        # gpt-oss, baseline 2026-11-15 (decision 0041).
        ({"impact_area": ["SG"]}, {"impact_areas": ["SG"]}, "impact_area read as"),
        ({"country": "LAO"}, {"countries": ["LAO"]}, "country read as countries"),
        ({"sdg": 8}, {"sdgs": [8]}, "read as a list"),
        ({"justice_lens": ["JUS-gender"]}, {"justice_lenses": ["JUS-gender"]}, "lens"),
        ({"year": {"gte": 2020}}, {"year_from": 2020}, "year_from=2020"),
        ({"year": 2020}, {"year_from": 2020, "year_to": 2020}, "year_to=2020"),
        ({"years": [2020, 2021, 2022]}, {"year_from": 2020, "year_to": 2022}, ""),
        ({"year_range": [2015, 2020]}, {"year_from": 2015, "year_to": 2020}, ""),
        (
            {"pub_year": {"min": "2018", "max": 2020}},
            {"year_from": 2018, "year_to": 2020},
            "",
        ),
        ({"year": {"gt": 2019, "lt": 2025}}, {"year_from": 2020, "year_to": 2024}, ""),
    ],
)
def test_unambiguous_filter_spellings_are_repaired(written, read, note):
    out = run_tool(_echo_filters_spec(), {"filters": written}, max_chars=2000)
    assert out.ok, out.error
    assert {k: v for k, v in out.data.items() if k != "min_confidence"} == read
    assert out.content.startswith("[Note: filters.") and note in out.content
    assert out.arguments["filters"] == read


@pytest.mark.parametrize(
    "written",
    [
        {"years": [2018, 2020, 2024]},  # not a range
        {"year": {"around": 2020}},
        {"year": 2020, "year_from": 2019},  # both given: which one?
        {"impact_area": ["SG"], "impact_areas": ["JE"]},
    ],
)
def test_ambiguous_filter_spellings_stay_errors(written):
    out = run_tool(_echo_filters_spec(), {"filters": written}, max_chars=2000)
    assert not out.ok and "Extra inputs are not permitted" in (out.error or "")
