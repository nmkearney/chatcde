"""The ``chatcde experts`` workflow: a ranked list from code, a reason per person.

Issue #71; see docs/decisions/0046-expertise-finder.md. A fixed chain, like
``chatcde gaps``:

1. **Rank** (code): :func:`chatcde.tools.expertise.expertise_report` matches
   records to the topic and filters and ranks people by them. Its counts are the
   only numbers in the output.
2. **Evidence** (code): each listed person's best-matching records (at most
   ``experts.evidence_per_person``), with title, year and the start of the
   passage the search found (or of the abstract, without a topic).
3. **Caveats** (code): records without text, people merged by initials, records
   with no CDE author, matches left out by ``experts.max_records``, the data
   edition.
4. **Reasons** (LLM, ``models.agent``): one structured request with the prompt
   ``experts.prompt``. For each person the model writes one or two sentences on
   what their matching work is about, citing only that person's evidence
   records. IDs outside it are dropped and reported; a person the model skips
   gets a placeholder. A failed reply is reported; the ranking still stands.

The :class:`ExpertsResult` records every input and output with the model,
prompt, vocabulary and code versions (CLAUDE.md: reproducibility).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from chatcde.agent import AgentError
from chatcde.harvest.common import code_version, now
from chatcde.schemas import Citation, ExpertiseResult, Filters
from chatcde.store.endpoint import EndpointError
from chatcde.tools.context import ToolContext, ToolError
from chatcde.tools.expertise import ExpertiseReport, TopicSearch, expertise_report
from chatcde.tools.publications import citation, records_by_id
from chatcde.workflows.answer import data_note
from chatcde.workflows.structured import request_structured

log = logging.getLogger(__name__)

#: Characters of each evidence passage passed to the model and shown with --json.
SNIPPET_CHARS = 400

#: Shown for a person the model gave no reason for.
NO_REASON = "The model gave no reason for this person."


class ExpertsError(Exception):
    """The workflow cannot run (no catalogue, no topic or filters, bad codes)."""


# --- evidence (code) ------------------------------------------------------------------


class EvidenceRecord(BaseModel):
    """One of a person's best-matching records."""

    citation: Citation
    snippet: str | None = Field(
        default=None, description="Start of the matching passage or the abstract"
    )
    score: float | None = Field(default=None, description="Search score, if searched")


class PersonEvidence(BaseModel):
    """A listed person with the records the model may cite for them."""

    rank: int = Field(ge=1)
    name: str
    records: list[EvidenceRecord]


def collect_evidence(
    ctx: ToolContext, report: ExpertiseReport, per_person: int
) -> list[PersonEvidence]:
    """Step 2: each listed person's best-matching records, best first."""
    result, matches = report.result, report.matches
    wanted = {rid for e in result.experts for rid in e.record_ids[:per_person]}
    with ctx.catalogue() as con:
        records = records_by_id(con, sorted(wanted))
    out = []
    for rank, e in enumerate(result.experts, 1):
        items = []
        for rid in e.record_ids[:per_person]:
            rec = records.get(rid)
            if rec is None:
                continue
            p = matches.passages.get(rid)
            text = p.chunk.text if p else (rec.abstract or "")
            items.append(
                EvidenceRecord(
                    citation=p.citation if p else citation(rec),
                    snippet=" ".join(text.split())[:SNIPPET_CHARS] or None,
                    score=round(p.score, 4) if p else None,
                )
            )
        out.append(PersonEvidence(rank=rank, name=e.name, records=items))
    return out


def _without_text(ctx: ToolContext, record_ids: list[str]) -> int:
    """Matching records with neither parsed pages nor an abstract."""
    if not record_ids:
        return 0
    with ctx.catalogue() as con:
        has_pages = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'pages'"
        ).fetchone()
        pages = (
            "OR record_id IN (SELECT DISTINCT record_id FROM pages)"
            if has_pages and has_pages[0]
            else "OR has_fulltext"
        )
        row = con.execute(
            "SELECT count(*) FROM records WHERE record_id IN "
            "(SELECT unnest(?::VARCHAR[])) AND NOT (coalesce(abstract, '') <> '' "
            f"{pages})",
            [record_ids],
        ).fetchone()
    return int(row[0]) if row else 0


def code_caveats(
    ctx: ToolContext, report: ExpertiseReport, cde_only: bool
) -> list[str]:
    """Step 3: the limits of the ranking, stated by code with their numbers."""
    r, m = report.result, report.matches
    s = ctx.cfg.experts
    out = []
    if m.searched:
        out.append(
            f"Matching records are the {r.matched_records:,} that the search for "
            f"{r.topic!r} found within the filters (rerank score >= "
            f"{s.min_score:g}); a person's other work on the topic may be missed."
        )
    if m.cut:
        out.append(
            f"{m.cut:,} more records scored above the threshold but were left out "
            f"(experts.max_records = {s.max_records})."
        )
    bare = _without_text(ctx, m.record_ids)
    if bare:
        out.append(
            f"{bare:,} of {r.matched_records:,} matching records have metadata only "
            "(no abstract or full text), so their match rests on title and tags."
        )
    if cde_only and r.records_without_cde_author:
        out.append(
            f"{r.records_without_cde_author:,} matching records have no author with "
            "a CDE affiliation in BORIS and add no one to the list (--all-authors "
            "lists external co-authors too)."
        )
    if report.by_initials:
        out.append(
            "Spellings with initials only were merged into "
            f"{', '.join(report.by_initials)}; check the record lists if a name is "
            "common."
        )
    if r.people_found > len(r.experts):
        out.append(
            f"{len(r.experts)} of {r.people_found} people are shown (raise -k for "
            "more)."
        )
    out.append(
        "Authors are merged by ORCID and name, not by the BORIS person ID; CDE "
        "affiliation counts if BORIS records it on at least one publication."
    )
    return out


# --- step 4: reasons (LLM) ------------------------------------------------------------


class PersonReason(BaseModel):
    """The model's reason for one listed person."""

    rank: int = Field(description="The person's rank, as given")
    reason: str = Field(
        description="One or two sentences on what their matching work is about, "
        "no numbers of your own"
    )
    evidence: list[str] = Field(
        description="record_id values from this person's records that support it"
    )


class ExpertsJudgement(BaseModel):
    """What the model must return (sent as the JSON schema of the reply)."""

    summary: str = Field(description="A short paragraph on who to contact and why")
    people: list[PersonReason]
    caveats: list[str] = Field(
        description="Limits of the evidence the reader should know"
    )


def _evidence_payload(result: ExpertsResult) -> dict[str, Any]:
    """The JSON the model reads: topic, scope, denominators, people, caveats."""
    by_rank = {e.rank: e for e in result.evidence}
    return {
        "topic": result.expertise.topic,
        "scope": result.expertise.filters.model_dump(exclude_defaults=True),
        "matching_records": result.expertise.matched_records,
        "people": [
            {
                "rank": rank,
                "name": x.name,
                "cde_units": x.cde_units,
                "matching_records": x.record_count,
                "years": [x.first_year, x.last_year],
                "records": [
                    {
                        "record_id": ev.citation.record_id,
                        "title": ev.citation.title,
                        "year": ev.citation.year,
                        "passage": ev.snippet,
                    }
                    for ev in by_rank[rank].records
                ],
            }
            for rank, x in enumerate(result.expertise.experts, 1)
        ],
        "caveats": result.caveats,
    }


def _check_judgement(
    judgement: ExpertsJudgement, evidence: list[PersonEvidence]
) -> tuple[ExpertsJudgement, list[str]]:
    """One reason per listed person, in rank order; drop IDs outside their evidence."""
    by_rank = {j.rank: j for j in judgement.people}
    kept, dropped = [], []
    for ev in evidence:
        j = by_rank.get(ev.rank)
        if j is None:
            kept.append(PersonReason(rank=ev.rank, reason=NO_REASON, evidence=[]))
            continue
        allowed = {r.citation.record_id for r in ev.records}
        dropped += [rid for rid in j.evidence if rid not in allowed]
        kept.append(
            j.model_copy(
                update={"evidence": [rid for rid in j.evidence if rid in allowed]}
            )
        )
    return judgement.model_copy(update={"people": kept}), sorted(set(dropped))


def judge(
    ctx: ToolContext, result: ExpertsResult, model: str
) -> tuple[ExpertsJudgement, list[str], dict[str, int]]:
    """Step 4: one structured request to the agent model.

    Returns:
        The checked reasons, the dropped record IDs and the token usage.

    Raises:
        AgentError: The reply is missing, not JSON or not valid.
        chatcde.store.endpoint.EndpointError: The request fails.
    """
    s = ctx.cfg.experts
    parsed, usage = request_structured(
        ctx,
        model_name=model,
        prompt=s.prompt,
        reply_model=ExpertsJudgement,
        schema_name="experts_reasons",
        evidence=_evidence_payload(result),
        temperature=s.temperature,
        max_tokens=s.max_tokens,
        structured_output=s.structured_output,
        tokens_setting="experts.max_tokens",
        reply_label="list of reasons",
    )
    checked, dropped = _check_judgement(parsed, result.evidence)
    return checked, dropped, usage


# --- result and the whole chain -------------------------------------------------------


class ExpertsResult(BaseModel):
    """An expertise search: ranking and evidence from code, reasons from the model."""

    expertise: ExpertiseResult
    cde_only: bool
    recent_years: int = Field(description="Window behind each recent_count")
    evidence: list[PersonEvidence]
    caveats: list[str] = Field(description="Caveats computed by code")
    data_note: str | None
    judgement: ExpertsJudgement | None
    judgement_error: str | None = None
    dropped_ids: list[str] = Field(
        default_factory=list,
        description="IDs the model cited that were not in the person's evidence",
    )
    usage: dict[str, int] = Field(default_factory=dict)
    seconds: float
    model: str | None
    prompt_version: str | None
    vocab_version: str
    code_version: str
    created_at: datetime


def run_experts(
    ctx: ToolContext,
    topic: str = "",
    filters: Filters | None = None,
    *,
    k: int = 10,
    cde_only: bool = True,
    use_llm: bool = True,
    model: str | None = None,
    search: TopicSearch | None = None,
) -> ExpertsResult:
    """Run the whole expertise workflow (steps 1-4 above).

    Args:
        ctx: Tool context; its endpoint client serves search and reasons.
        topic: What to search for; may be empty when filters are given.
        filters: Restrictions (tags, years, units, ...).
        k: People to list (capped by ``experts.max_k``).
        cde_only: Only CDE-affiliated people.
        use_llm: ``False`` stops after step 3 (no model call).
        model: Override for ``models.agent``.
        search: Search hook (tests replace it).

    Raises:
        ExpertsError: No catalogue, no authors, no topic or filters, unknown
            codes, or the search failing.
    """
    started = time.monotonic()
    cfg = ctx.cfg
    s = cfg.experts
    try:
        report = expertise_report(ctx, topic, filters, k, cde_only, search)
    except ToolError as exc:
        raise ExpertsError(str(exc)) from exc
    note = data_note(cfg)
    result = ExpertsResult(
        expertise=report.result,
        cde_only=cde_only,
        recent_years=s.recent_years,
        evidence=collect_evidence(ctx, report, s.evidence_per_person),
        caveats=code_caveats(ctx, report, cde_only),
        data_note=note.text() if note else None,
        judgement=None,
        seconds=0.0,
        model=None,
        prompt_version=None,
        vocab_version=cfg.vocab.version,
        code_version=code_version(),
        created_at=now(),
    )
    update: dict[str, Any] = {}
    if use_llm and result.evidence:
        name = model or cfg.models.agent
        update |= {"model": name, "prompt_version": s.prompt}
        try:
            judgement, dropped, usage = judge(ctx, result, name)
            update |= {"judgement": judgement, "dropped_ids": dropped, "usage": usage}
        except (AgentError, EndpointError) as exc:
            log.warning("experts: reasons failed: %s", exc)
            update["judgement_error"] = str(exc)
    update["seconds"] = round(time.monotonic() - started, 2)
    result = result.model_copy(update=update)
    log.info(
        "experts %r: %d people of %d, %.1f s",
        topic,
        len(result.expertise.experts),
        result.expertise.people_found,
        result.seconds,
    )
    return result


# --- terminal output ------------------------------------------------------------------


def source_numbers(result: ExpertsResult) -> dict[str, tuple[int, Citation]]:
    """Every evidence record, numbered in order of first appearance."""
    out: dict[str, tuple[int, Citation]] = {}
    for person in result.evidence:
        for ev in person.records:
            if ev.citation.record_id not in out:
                out[ev.citation.record_id] = (len(out) + 1, ev.citation)
    return out


def render_experts(console: Any, result: ExpertsResult) -> None:
    """Print the ranking, reasons, sources, caveats and footer.

    Args:
        console: A :class:`rich.console.Console`.
        result: The workflow result.
    """
    from rich.markdown import Markdown
    from rich.markup import escape
    from rich.table import Table

    from chatcde.workflows.gaps import _scope_text

    x = result.expertise
    head = f"Experts: {x.topic}" if x.topic else "Experts"
    console.print(f"[bold]{escape(head)}[/bold]", highlight=False)
    who = "CDE-affiliated people" if result.cde_only else "all authors"
    console.print(
        f"Scope: {escape(_scope_text(x.filters, []))}. {x.matched_records:,} matching "
        f"records; {x.people_found:,} {who} with at least one. Ranked by matching "
        f"records, then recent ones (last {result.recent_years} years).",
        highlight=False,
    )
    if not x.experts:
        console.print("No one found.")
        for line in result.caveats:
            console.print(f"[dim]- {escape(line)}[/dim]", highlight=False)
        return

    numbers = source_numbers(result)
    reasons = {j.rank: j for j in result.judgement.people} if result.judgement else {}
    by_rank = {e.rank: e for e in result.evidence}

    def refs(ids: list[str]) -> str:
        return "; ".join(str(numbers[i][0]) for i in ids if i in numbers)

    t = Table(title=None, show_lines=False)
    t.add_column("#", justify="right")
    t.add_column("Name")
    t.add_column("Units")
    t.add_column("Records", justify="right")
    t.add_column("Recent", justify="right")
    t.add_column("Years")
    t.add_column("Sources")
    for rank, e in enumerate(x.experts, 1):
        j = reasons.get(rank)
        ids = j.evidence if j else [r.citation.record_id for r in by_rank[rank].records]
        years = (
            f"{e.first_year}-{e.last_year}"
            if e.first_year and e.first_year != e.last_year
            else str(e.last_year or "-")
        )
        t.add_row(
            str(rank),
            escape(e.name) + ("" if e.is_cde else " [dim](ext.)[/dim]"),
            escape(", ".join(e.cde_units) or "-"),
            str(e.record_count),
            str(e.recent_count),
            years,
            refs(ids),
        )
    console.print(t)

    if result.judgement:
        console.print(Markdown(result.judgement.summary))
    elif result.judgement_error:
        console.print(
            f"[red]No reasons: {escape(result.judgement_error)}[/red] The ranking "
            "above still holds.",
            highlight=False,
        )

    console.print()
    console.print("[bold]People[/bold]")
    for rank, e in enumerate(x.experts, 1):
        orcid = f" ORCID {e.orcid}." if e.orcid else ""
        also = f" Also written {'; '.join(e.variants)}." if e.variants else ""
        j = reasons.get(rank)
        reason = ""
        if j:
            ref = f" [{refs(j.evidence)}]" if refs(j.evidence) else ""
            reason = f" {j.reason}{ref}"
        console.print(
            f"{rank}. [bold]{escape(e.name)}[/bold]"
            f"{':' + escape(reason) if reason else '.'}"
            f"[dim]{escape(orcid + also)}[/dim]",
            highlight=False,
        )
        rest = len(e.record_ids) - len(by_rank[rank].records)
        if rest > 0:
            console.print(
                f"   [dim]{rest} more matching records (--json lists them)[/dim]",
                highlight=False,
            )

    console.print()
    console.print("[bold]Sources[/bold]")
    for n, cit in numbers.values():
        year = f" ({cit.year})" if cit.year else ""
        links = []
        if cit.doi:
            url = f"https://doi.org/{cit.doi}"
            links.append(f"[link={url}]{url}[/link]")
        links.append(f"BORIS: [link={cit.boris_url}]{cit.boris_url}[/link]")
        console.print(
            f"[{n}] {escape(cit.title)}{year}. {' | '.join(links)}", highlight=False
        )

    console.print()
    console.print("[bold]Caveats[/bold]")
    for line in result.caveats:
        console.print(f"- {escape(line)}", highlight=False)
    for line in result.judgement.caveats if result.judgement else []:
        console.print(f"- {escape(line)} [dim](model)[/dim]", highlight=False)
    if result.dropped_ids:
        console.print(
            "[red]- The model cited records outside the evidence; they were "
            f"dropped: {escape(', '.join(result.dropped_ids))}[/red]",
            highlight=False,
        )
    if result.data_note:
        console.print(f"[dim]{escape(result.data_note)}[/dim]", highlight=False)
    parts = [
        result.model or "no model",
        result.prompt_version or "-",
        f"vocab {result.vocab_version}",
        f"{result.seconds:.1f} s",
    ]
    if result.usage.get("total_tokens"):
        parts.append(f"{result.usage['total_tokens']:,} tokens")
    console.print(f"[dim]{' | '.join(parts)}[/dim]", highlight=False)
