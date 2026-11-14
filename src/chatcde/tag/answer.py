"""The JSON schema the tagging model answers in, built per batch from the vocabulary.

Each batch of dimensions (``tagging.batches``) gets its own pydantic model, sent
as ``response_format`` by :func:`chatcde.tag.llm.chat_json`, so the server only
lets the model write codes that exist (decision 0028):

* ``term`` is a ``Literal`` of the live codes of that dimension, read with
  :func:`chatcde.tools.vocabulary.dimension_entries`, so groups and retired codes
  are never offered. Countries come from ``vocab/m49.csv`` plus the
  global/multi-country flag; SDGs are "1"–"17"; output type is the audience list.
* Curated dimensions (method, setting, actors, …) also allow ``"other"``, with a
  short ``other_label``, so gaps in the vocabulary show up for its next version.
* ``strength`` (explicit / inferred / weak) is mapped to a confidence in code
  (``tagging.confidence``); the model never writes a number.
* ``role`` (studied / co_producer / audience) is required for actors only.
* Each list is capped at ``tagging.max_per_dimension``.
* Country codes are normalised before validation (#70): the server does not
  always hold the model to the enum, and the model sometimes writes sports codes
  (``LIT``), alpha-2 codes or names. These are mapped to alpha-3 with
  :func:`chatcde.tag.regions.country_lookup`; codes that cannot be mapped are
  dropped and logged, so one bad code no longer fails the whole batch.

Every field is required (nullable where it may be empty) because vLLM's strict
JSON-schema mode expects that.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from chatcde.config import TaggingConfig
from chatcde.schemas import ActorRole, Dimension
from chatcde.tag.curation import PREFIXES
from chatcde.tag.regions import GLOBAL_FLAG, Country, country_lookup
from chatcde.tools.vocabulary import dimension_entries

log = logging.getLogger(__name__)
OTHER = "other"
Strength = Literal["explicit", "inferred", "weak"]


def allows_other(dimension: str) -> bool:
    """Whether a dimension accepts ``other`` (the curated ones, decision 0026)."""
    return dimension in PREFIXES


def codes_for(
    dimension: str,
    vocab: dict[str, Any],
    countries: dict[str, Country],
    exclude: Collection[str] = (),
) -> list[str]:
    """Codes the model may use for one dimension, in vocabulary order.

    ``exclude`` drops codes filled in by code instead (``tagging.none_terms``).

    Raises:
        ValueError: The dimension has no codes (missing from the vocabulary).
    """
    dim = Dimension(dimension)
    if dim is Dimension.COUNTRY:
        codes = sorted(countries) + [GLOBAL_FLAG]
    else:
        codes = [e.code for e in dimension_entries(vocab, dim) if e.code not in exclude]
    if allows_other(dimension):
        codes.append(OTHER)
    if not codes:
        raise ValueError(f"no vocabulary codes for {dimension}")
    return codes


def item_model(dimension: str, codes: list[str]) -> type[BaseModel]:
    """The model of one tag of ``dimension`` (term, evidence, strength, …)."""
    fields: dict[str, Any] = {
        "term": (
            Literal[tuple(codes)],  # type: ignore[valid-type]
            Field(description=f"A {dimension} code from the list"),
        ),
        "evidence": (
            str,
            Field(description="Short verbatim quote from the text, original language"),
        ),
        "strength": (
            Strength,
            Field(description="explicit: stated; inferred: clearly implied; weak"),
        ),
    }
    if allows_other(dimension):
        fields["other_label"] = (
            str | None,
            Field(description="Short label when term is 'other', else null"),
        )
    if dimension == Dimension.ACTORS.value:
        fields["role"] = (
            Literal[tuple(r.value for r in ActorRole)],  # type: ignore[valid-type]
            Field(description="How the actors relate to the research"),
        )
    name = "".join(p.title() for p in dimension.split("_")) + "Tag"
    return create_model(
        name,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def normalise_countries(lookup: dict[str, str]) -> Callable[[Any], Any]:
    """A before-validator for a country tag list that maps codes to alpha-3.

    Each item's ``term`` is looked up case-insensitively in ``lookup`` (see
    :func:`chatcde.tag.regions.country_lookup`); the global/multi-country flag
    passes unchanged. Items whose term cannot be mapped are dropped with a
    warning. Anything that is not a list of objects is left for pydantic to reject.
    """

    def fix(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        out: list[Any] = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("term"), str):
                out.append(item)
                continue
            raw = item["term"]
            if raw == GLOBAL_FLAG:
                out.append(item)
            elif (code := lookup.get(raw.strip().upper())) is not None:
                if code != raw:
                    log.info("country code %r mapped to %s", raw, code)
                out.append({**item, "term": code})
            else:
                log.warning("country code %r is not a country; tag dropped", raw)
        return out

    return fix


def batch_model(
    batch: str,
    dimensions: list[str],
    vocab: dict[str, Any],
    countries: dict[str, Country],
    settings: TaggingConfig,
) -> type[BaseModel]:
    """The answer model for one batch: one capped list of tags per dimension."""
    fields: dict[str, Any] = {}
    validators: dict[str, Any] = {}
    for dim in dimensions:
        none = settings.none_terms.get(dim)
        codes = codes_for(dim, vocab, countries, exclude={none} if none else ())
        item = item_model(dim, codes)
        fields[dim] = (list[item], Field(max_length=settings.limit(dim)))  # type: ignore[valid-type]
        if dim == Dimension.COUNTRY.value:
            lookup = country_lookup(countries, settings.country_aliases)
            validators["_normalise_countries"] = field_validator(dim, mode="before")(
                normalise_countries(lookup)
            )
    name = "Tags" + "".join(p.title() for p in batch.split("_"))
    return create_model(
        name,
        __config__=ConfigDict(extra="forbid"),
        __validators__=validators,
        **fields,
    )
