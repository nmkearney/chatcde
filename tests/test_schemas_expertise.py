"""The find_expertise output models (#71): they validate and round-trip."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chatcde.schemas import Expert, ExpertiseResult, Filters


def _expert(**overrides: object) -> Expert:
    values: dict[str, object] = {
        "name": "Liniger, Hans Peter",
        "variants": ["Liniger, Hanspeter"],
        "orcid": "0000-0001-8597-8187",
        "is_cde": True,
        "cde_units": ["Sustainable Land Systems"],
        "record_count": 3,
        "first_author_count": 1,
        "recent_count": 2,
        "first_year": 2012,
        "last_year": 2024,
        "total_records": 143,
        "record_ids": ["a", "b", "c"],
    }
    values.update(overrides)
    return Expert.model_validate(values)


def test_result_round_trips() -> None:
    result = ExpertiseResult(
        topic="sustainable land management",
        filters=Filters(regions=["CDE-EAF"]),
        matched_records=12,
        search_records=12,
        records_without_cde_author=2,
        people_found=7,
        experts=[_expert()],
    )
    again = ExpertiseResult.model_validate_json(result.model_dump_json())
    assert again == result
    assert again.experts[0].variants == ["Liniger, Hanspeter"]


def test_expert_needs_counts() -> None:
    with pytest.raises(ValidationError):
        Expert.model_validate({"name": "X", "is_cde": True})


def test_find_expertise_signature_is_a_stub_until_built() -> None:
    from chatcde.tools import core

    with pytest.raises(NotImplementedError):
        core.find_expertise("pastoralism")
