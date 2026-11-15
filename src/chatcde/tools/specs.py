"""Tool definitions for the LLM, generated from the tool functions themselves.

The agent (and later an MCP server) offers the functions in
:mod:`chatcde.tools.core` to the model. Everything the model sees comes from
the code, so the two cannot drift apart (decision 0022):

* **Parameters:** a pydantic model built from the signature's type hints; its
  JSON schema, with ``$ref`` inlined and titles removed, is the ``parameters``
  of the OpenAI tool definition. Enums (``Dimension``) and nested models
  (``Filters``) come out as JSON-schema enums and objects.
* **Descriptions:** the docstring's text before ``Args:`` (plus its ``Returns:``
  section) describes the tool; each ``Args:`` entry describes its parameter.
* **Calls:** :func:`run_tool` parses the model's JSON arguments, validates
  them with the same pydantic model (so ``{"row": "sub_area"}`` becomes
  ``Dimension.SUB_AREA`` and a filters object becomes ``Filters``), calls the
  function and turns the result, or the error, into text for the model.
"""

from __future__ import annotations

import difflib
import inspect
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, get_args, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from pydantic_core import to_jsonable_python

from chatcde.schemas import Filters
from chatcde.tools import core
from chatcde.tools.context import ToolError

log = logging.getLogger(__name__)

#: The tools the agent can be offered, by name (``agent.tools`` picks from these).
CORE_TOOLS: dict[str, Callable[..., Any]] = {
    fn.__name__: fn
    for fn in (
        core.search_publications,
        core.get_publication,
        core.read_section,
        core.list_vocabulary,
        core.count_by,
        core.find_gaps,
        core.coverage_report,
    )
}

_SECTION_RE = re.compile(r"^(Args|Arguments|Returns|Raises|Yields|Examples?):\s*$")
_ARG_RE = re.compile(r"^(\w+)(?:\s*\([^)]*\))?:\s*(.*)$")


def parse_docstring(doc: str | None) -> tuple[str, dict[str, str], str | None]:
    """Split a Google-style docstring into description, argument docs and returns.

    Returns:
        ``(description, {argument: text}, returns_text)``. Continuation lines
        are joined with spaces; ``Raises:`` and other sections are dropped.
    """
    lines = inspect.cleandoc(doc or "").splitlines()
    description: list[str] = []
    args: dict[str, str] = {}
    returns: list[str] = []
    section: str | None = None
    current: str | None = None
    for line in lines:
        m = _SECTION_RE.match(line.strip()) if not line.startswith(" ") else None
        if m:
            section = m.group(1)
            current = None
            continue
        if section is None:
            description.append(line)
        elif section in ("Args", "Arguments"):
            stripped = line.strip()
            am = _ARG_RE.match(stripped)
            indent = len(line) - len(line.lstrip())
            if am and indent <= 4:
                current = am.group(1)
                args[current] = am.group(2).strip()
            elif current and stripped:
                args[current] = f"{args[current]} {stripped}".strip()
        elif section == "Returns":
            returns.append(line.strip())
    paragraphs = re.split(r"\n\s*\n", "\n".join(description).strip())
    text = "\n\n".join(" ".join(p.split()) for p in paragraphs if p.strip())
    ret = " ".join(r for r in returns if r).strip() or None
    tidy = _tidy
    return tidy(text), {k: tidy(v) for k, v in args.items()}, ret and tidy(ret)


def _tidy(text: str) -> str:
    """Turn reST ``literals`` into Markdown `literals` (shorter for the model)."""
    return text.replace("``", "`")


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace every ``$ref`` with its definition and drop ``$defs`` and titles.

    Local models follow flat schemas more reliably than ``$ref`` chains, and
    the ``title`` keywords pydantic adds only repeat the property names.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].split("/")[-1]]
                merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
                return walk(merged)
            return {
                k: walk(v)
                for k, v in node.items()
                if k != "$defs" and not (k == "title" and isinstance(v, str))
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


@dataclass(frozen=True)
class ToolSpec:
    """One tool: the function, its argument model and its description."""

    name: str
    fn: Callable[..., Any]
    description: str
    args_model: type[BaseModel]

    def parameters(self) -> dict[str, Any]:
        """JSON schema of the arguments (flat, no ``$ref``)."""
        schema = _inline_refs(self.args_model.model_json_schema())
        schema.setdefault("properties", {})
        schema["additionalProperties"] = False
        return schema

    def definition(self) -> dict[str, Any]:
        """The OpenAI ``tools`` entry for this function."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters(),
            },
        }


def build_spec(fn: Callable[..., Any]) -> ToolSpec:
    """Generate a :class:`ToolSpec` from a typed, documented function.

    Raises:
        TypeError: If a parameter has no type hint.
    """
    hints = get_type_hints(fn)
    description, arg_docs, returns = parse_docstring(fn.__doc__)
    if returns:
        description = f"{description}\n\nReturns: {returns}"
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name not in hints:
            raise TypeError(f"{fn.__name__}: parameter {name!r} has no type hint")
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (
            hints[name],
            Field(default=default, description=arg_docs.get(name)),
        )
    model = create_model(  # type: ignore[call-overload]
        f"{fn.__name__}_arguments",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return ToolSpec(fn.__name__, fn, description, model)


def tool_specs(names: Iterable[str] | None = None) -> dict[str, ToolSpec]:
    """Specs of the named core tools (all of them if ``names`` is ``None``).

    Raises:
        ValueError: If a name is not a core tool.
    """
    chosen = list(CORE_TOOLS) if names is None else list(names)
    unknown = [n for n in chosen if n not in CORE_TOOLS]
    if unknown:
        raise ValueError(
            f"unknown tools {unknown}; choose from {', '.join(CORE_TOOLS)}"
        )
    return {n: build_spec(CORE_TOOLS[n]) for n in chosen}


# ---------------------------------------------------------------------------
# Running a call
# ---------------------------------------------------------------------------


@dataclass
class ToolOutcome:
    """What one tool call did: the content for the model and a log entry."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    content: str
    error: str | None = None
    seconds: float = 0.0
    data: Any = field(default=None, repr=False)


def compact(
    data: Any, *, omit_fields: Sequence[str] = (), max_ids: int | None = None
) -> Any:
    """Shrink a JSON-ready result for the model's context.

    Drops keys named in ``omit_fields`` (e.g. ``provenance``) at any depth, and
    cuts every ``record_ids`` list to ``max_ids`` entries, adding
    ``record_ids_omitted`` with the number left out, so counts stay exact.
    With ``max_ids=0`` the ``record_ids`` lists are dropped altogether (count
    cells already carry their ``count``; decision 0036).
    """
    omit = set(omit_fields)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in omit or (k == "record_ids" and max_ids == 0):
                    continue
                if (
                    k == "record_ids"
                    and isinstance(v, list)
                    and max_ids is not None
                    and len(v) > max_ids
                ):
                    out[k] = v[:max_ids]
                    out["record_ids_omitted"] = len(v) - max_ids
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(data)


class ResultTooLarge(Exception):
    """A result that may not be cut is longer than the limit (decision 0036)."""

    def __init__(self, chars: int, max_chars: int) -> None:
        """Keep the result's length and the limit it went over."""
        super().__init__(f"{chars} characters, over the limit of {max_chars}")
        self.chars = chars
        self.max_chars = max_chars


def render(
    data: Any,
    *,
    max_chars: int,
    omit_fields: Sequence[str] = (),
    max_ids: int | None = None,
    cut: bool = True,
) -> str:
    """JSON text of a result for the model, compacted and cut to ``max_chars``.

    Args:
        data: The JSON-ready result.
        max_chars: Longest text sent to the model.
        omit_fields: Keys dropped at any depth.
        max_ids: Longest ``record_ids`` list kept (see :func:`compact`).
        cut: Whether a longer text may be cut, with a note. Counts must not be:
            a model reads the missing cells of a cut table as zeros (#81).

    Raises:
        ResultTooLarge: If ``cut`` is false and the text is over ``max_chars``.
    """
    text = json.dumps(
        compact(data, omit_fields=omit_fields, max_ids=max_ids),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(text) <= max_chars:
        return text
    if not cut:
        raise ResultTooLarge(len(text), max_chars)
    return (
        text[:max_chars]
        + f" ... [cut: the result had {len(text)} characters; ask for less, "
        "e.g. a smaller k, a narrower page range or more filters]"
    )


def too_large_message(name: str, data: Any, exc: ResultTooLarge) -> str:
    """Error for a count result that is too large to send whole.

    Names the number of cells, if any, and the ways to ask for fewer.
    """
    cells = data.get("cells") if isinstance(data, dict) else None
    size = f"{len(cells)} cells, " if isinstance(cells, list) else ""
    return (
        f"the {name} result is too large to send whole ({size}{exc}). Counts are "
        "never sent cut, so nothing was returned. Ask for fewer cells: "
        "level='group', one dimension instead of two, or filters that narrow "
        "the rows or columns; then drill into the groups you need"
    )


def _accepts_str(annotation: Any) -> bool:
    """Whether a type hint admits a plain ``str`` (``str``, ``str | None``, ...)."""
    if annotation is str:
        return True
    return any(_accepts_str(a) for a in get_args(annotation))


def unwrap_json_strings(model: type[BaseModel], args: dict[str, Any]) -> dict[str, Any]:
    """Parse object arguments that the model sent as JSON text.

    qwen3.8 sends ``filters`` as a string holding a JSON object (2026-11-11,
    #67), which fails validation on every call. A string that starts with ``{``
    and parses to an object is replaced by that object, but only for parameters
    that do not take a string anyway.
    """
    out = dict(args)
    for name, value in args.items():
        info = model.model_fields.get(name)
        if not (isinstance(value, str) and value.lstrip().startswith("{")):
            continue
        if info is None or _accepts_str(info.annotation):
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out[name] = parsed
    return out


def _list_fields() -> dict[str, str]:
    """Singular spellings of the list fields of ``Filters`` -> the field name.

    ``impact_area`` -> ``impact_areas``, ``country`` -> ``countries``,
    ``justice_lens`` -> ``justice_lenses``.
    """
    out: dict[str, str] = {}
    for name, info in Filters.model_fields.items():
        if getattr(info.annotation, "__origin__", None) is not list:
            continue
        if name.endswith("ies"):
            out[name[:-3] + "y"] = name
        if name.endswith("es"):
            out[name[:-2]] = name
        if name.endswith("s"):
            out[name[:-1]] = name
    return {k: v for k, v in out.items() if k not in Filters.model_fields}


#: Keys models use for a year or a year range (gpt-oss, baseline 2026-11-15).
YEAR_KEYS = ("year", "years", "pub_year", "publication_year", "year_range")
_FROM_KEYS = ("gte", "ge", "min", "from", "start")
_TO_KEYS = ("lte", "le", "max", "to", "end")


def _as_year(value: Any) -> int | None:
    """``value`` as a year, if it is one (``2020`` or ``"2020"``)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1000 <= value <= 9999:
        return value
    if isinstance(value, str) and value.strip().isdigit() and len(value.strip()) == 4:
        return int(value)
    return None


def _year_range(value: Any) -> tuple[int | None, int | None] | None:
    """The (from, to) years one spelling stands for; ``None`` if it is not clear.

    ``2020`` is that year only; ``[2020, 2026]`` (two years, or a consecutive
    list) a range; ``{"gte": 2020}`` an open range. Lists with gaps are not a
    range, so they are left for the validation error.
    """
    if (y := _as_year(value)) is not None:
        return y, y
    if isinstance(value, list) and value:
        years = [_as_year(v) for v in value]
        if None in years:
            return None
        ys = sorted(y for y in years if y is not None)
        if len(ys) == 2 or ys == list(range(ys[0], ys[-1] + 1)):
            return ys[0], ys[-1]
        return None
    if isinstance(value, dict) and value:
        lo = [_as_year(value[k]) for k in _FROM_KEYS if k in value]
        hi = [_as_year(value[k]) for k in _TO_KEYS if k in value]
        if "gt" in value and (y := _as_year(value["gt"])) is not None:
            lo.append(y + 1)
        if "lt" in value and (y := _as_year(value["lt"])) is not None:
            hi.append(y - 1)
        known = {*_FROM_KEYS, *_TO_KEYS, "gt", "lt"}
        if set(value) - known or None in lo + hi or len(lo) > 1 or len(hi) > 1:
            return None
        return (lo[0] if lo else None), (hi[0] if hi else None)
    return None


def repair_filters(filters: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Read unambiguous misspellings of filter keys; say what was read how.

    gpt-oss writes ``impact_area`` for ``impact_areas`` (17 calls in the
    baseline of 2026-11-15, despite "did you mean") and ``year: {"gte": 2020}``
    for ``year_from`` (16 calls; it then told the user that years cannot be
    filtered). A key is repaired only where one reading is possible:

    * the singular of a list field (``impact_area`` -> ``impact_areas``), when the
      list field is not given too;
    * a single value for a list field becomes a one-item list;
    * a year spelling (:data:`YEAR_KEYS`) that :func:`_year_range` can read becomes
      ``year_from`` / ``year_to``, when neither is given.

    Anything else is left as it is, so validation still rejects it (decision
    0034). Repairs are never silent: each one is returned as a note, which the
    tool result carries (decision 0041).

    Returns:
        The repaired filters and one note per repair.
    """
    out = dict(filters)
    notes: list[str] = []
    singular = _list_fields()
    for key in list(filters):
        target = singular.get(key)
        if target and target not in out:
            out[target] = out.pop(key)
            notes.append(f"filters.{key} read as {target}")
    for key in YEAR_KEYS:
        if key not in out or "year_from" in out or "year_to" in out:
            continue
        span = _year_range(out[key])
        if span is None:
            continue
        del out[key]
        lo, hi = span
        if lo is not None:
            out["year_from"] = lo
        if hi is not None:
            out["year_to"] = hi
        notes.append(
            f"filters.{key} read as year_from={lo}, year_to={hi}"
            if lo is not None and hi is not None
            else f"filters.{key} read as "
            + (f"year_from={lo}" if lo is not None else f"year_to={hi}")
        )
    for name, value in list(out.items()):
        info = Filters.model_fields.get(name)
        is_list = info is not None and getattr(info.annotation, "__origin__", None)
        if (
            is_list is list
            and isinstance(value, str | int)
            and not isinstance(value, bool)
        ):
            out[name] = [value]
            notes.append(f"filters.{name} read as a list: [{value!r}]")
    return out, notes


#: Hints for keys that are not filters but say what the model wanted.
_KEY_HINTS = {
    "record_id": "to look inside one record use get_publication or read_section",
    "record_ids": "to look inside one record use get_publication or read_section",
}


def _problem(model: type[BaseModel], error: Any) -> str:
    """One validation error as text, with a suggestion for a misspelt field.

    gpt-oss writes ``filters.impact_area`` for ``impact_areas``; naming the
    right field saves the model a call (#67). An unknown filter key with no
    close match gets the list of valid keys, or a hint on what to do instead.
    """
    loc = [str(p) for p in error["loc"]]
    text = f"{'.'.join(loc) or 'arguments'}: {error['msg']}"
    if error.get("type") == "extra_forbidden" and loc:
        in_filters = loc[0] == "filters"
        fields = Filters.model_fields if in_filters else model.model_fields
        close = difflib.get_close_matches(loc[-1], list(fields), n=1, cutoff=0.75)
        if close:
            text += f" (did you mean {close[0]!r}?)"
        elif in_filters and loc[-1] in _KEY_HINTS:
            text += f" ({_KEY_HINTS[loc[-1]]})"
        elif in_filters:
            text += f" (valid filter keys: {', '.join(fields)})"
    return text


def run_tool(
    spec: ToolSpec,
    raw_arguments: str | dict[str, Any] | None,
    *,
    max_chars: int,
    omit_fields: Sequence[str] = (),
    max_ids: int | None = None,
    cut: bool = True,
) -> ToolOutcome:
    """Validate the model's arguments, call the tool and render the outcome.

    Never raises for the model's mistakes or the tool's refusals: they come back
    as ``ok=False`` with an error the model can read and act on. Unambiguous
    misspellings of filter keys are repaired first (:func:`repair_filters`); the
    result then starts with a note saying how they were read, and
    ``ToolOutcome.arguments`` holds the repaired arguments. Unexpected
    exceptions are logged with their traceback and reported briefly. With
    ``cut=False`` (counting tools), a result over ``max_chars`` is an error
    asking for fewer cells, never a partial table (decision 0036).
    """
    started = time.monotonic()
    args: dict[str, Any] = {}

    def failed(message: str) -> ToolOutcome:
        return ToolOutcome(
            spec.name,
            args,
            False,
            json.dumps({"error": message}, ensure_ascii=False),
            message,
            round(time.monotonic() - started, 3),
        )

    try:
        if isinstance(raw_arguments, dict):
            args = raw_arguments
        else:
            parsed = json.loads(raw_arguments or "{}")
            if not isinstance(parsed, dict):
                return failed("arguments must be a JSON object")
            args = parsed
    except json.JSONDecodeError as exc:
        return failed(f"arguments are not valid JSON: {exc}")
    args = unwrap_json_strings(spec.args_model, args)
    notes: list[str] = []
    if (
        isinstance(args.get("filters"), dict)
        and "filters" in spec.args_model.model_fields
    ):
        repaired, notes = repair_filters(args["filters"])
        args = {**args, "filters": repaired}
    try:
        validated = spec.args_model.model_validate(args)
    except ValidationError as exc:
        problems = "; ".join(_problem(spec.args_model, e) for e in exc.errors())
        return failed(f"invalid arguments for {spec.name}: {problems}")
    kwargs = {name: getattr(validated, name) for name in spec.args_model.model_fields}
    try:
        result = spec.fn(**kwargs)
    except ToolError as exc:
        return failed(str(exc))
    except NotImplementedError:
        return failed(f"{spec.name} is not available yet; answer without it")
    except Exception as exc:  # noqa: BLE001 - report, keep the conversation going
        log.exception("tool %s failed", spec.name)
        return failed(f"{spec.name} failed unexpectedly ({type(exc).__name__})")
    data = to_jsonable_python(result)
    try:
        content = render(
            data,
            max_chars=max_chars,
            omit_fields=omit_fields,
            max_ids=max_ids,
            cut=cut,
        )
    except ResultTooLarge as exc:
        log.warning("tool %s: result not sent (%s)", spec.name, exc)
        return failed(too_large_message(spec.name, data, exc))
    if notes:
        log.info("tool %s: %s", spec.name, "; ".join(notes))
        content = f"[Note: {'; '.join(notes)}. Use these names next time.]\n{content}"
    return ToolOutcome(
        spec.name,
        args,
        True,
        content,
        None,
        round(time.monotonic() - started, 3),
        data,
    )
