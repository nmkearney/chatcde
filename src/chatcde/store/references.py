"""Recognise chunks that are reference lists, so search can leave them out (#68).

Reference lists match almost any topic (they name many titles) and say nothing
about CDE's own findings, yet they ranked among the top search hits (e.g.
"large-scale land acquisitions in Laos" -> record 49690ed2, pp. 12-14; the
#5 baseline showed the same for Mount Kenya and coffee value chains).

A chunk's **reference score** is the number of reference-entry signatures per
word: a year followed by a full stop (``(2021).``, ``2016. Title``), volume and
pages (``14(2): 379``, ``87, 1-15``), ``pp.``, ``In:``, ``Eds.``, ISBN, links and
DOIs, and author names with initials (``Surname, A. B.``, ``A. B. Surname,``).
In-text citations such as ``(Smith, 2006; Jones, 2010)`` match few of these.

On the real index (92,037 full-text chunks, 2026-11-15) the scores are bimodal.
From about 0.06 nearly every chunk is a reference list (hand check in decision
0044). The cut-off is ``retrieval.reference_cutoff``. The score is computed at
search time on the candidates only, so changing the cut-off needs no re-index.
"""

from __future__ import annotations

import re

#: Version of the signature list; bump it when the patterns change.
REFERENCE_SCORE_VERSION = "1"

_SIGNATURES = [
    re.compile(r"\(\s?(?:19|20)\d{2}[a-z]?\s?\)\s?[.,:]"),  # (2021).
    re.compile(r"(?<![(\d])\b(?:19|20)\d{2}[a-z]?\.\s"),  # 2016. Title
    re.compile(r"\b(?:19|20)\d{2}[a-z]?;\s?\d+"),  # 2016;69:147
    re.compile(r"\b\d{1,4}\s?\(\d{1,4}\)\s?[:,]\s?\d"),  # 14(2): 379
    re.compile(r"\b\d{1,4},\s?\d{1,5}\s?[–-]\s?\d{1,5}\b"),  # 87, 1-15
    re.compile(r"\bpp?\.\s?\d"),  # pp. 48
    re.compile(r"\bIn:?\s"),  # In: / In
    re.compile(r"\b[Ee]ds?\.|\(eds?\.?\)"),  # Eds. / (eds)
    re.compile(r"\bISBN|\bISSN|\baccessed\b|\bretrieved\b|\bAbgerufen|\bconsult", re.I),
    re.compile(r"https?://|\bdoi[:.]|\bCrossRef\b|\bPubMed\b", re.I),
    re.compile(r"\b[A-ZÀ-Þ][\w'’-]+,\s(?:[A-ZÀ-Þ]\.\s?-?){1,3}"),  # Surname, A.
    re.compile(r"\b(?:[A-ZÀ-Þ]\.\s?){1,3}[A-ZÀ-Þ][a-zà-ÿ'’-]+,"),  # A. Surname,
]
_WORD = re.compile(r"\w+")


def reference_score(text: str) -> float:
    """Reference-entry signatures per word in ``text`` (0 for empty text)."""
    words = len(_WORD.findall(text))
    if not words:
        return 0.0
    return sum(len(p.findall(text)) for p in _SIGNATURES) / words


def is_reference_list(text: str, cutoff: float | None) -> bool:
    """Whether ``text`` scores at least ``cutoff``; never when ``cutoff`` is None."""
    return cutoff is not None and reference_score(text) >= cutoff
