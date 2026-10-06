# core/text_search.py
"""Lexical text matching — one tokenizer, shared by everything that has to
compare the user's plain-language input against a fixed corpus.

Two callers, one reason: `core.tool_tiers.search()` ranks the deferred tool
declarations against a request, and `core.episode_memory` ranks past task runs
against a new goal. Both need the same answer to "do these two phrases look
like they are about the same thing", and both deliberately want a LEXICAL
answer rather than an embedding:

  * instant and offline — the router runs mid-sentence, the planner mid-task;
  * no API call and no key;
  * a name match is almost always decisive on a corpus this small, and when it
    is not, the fallback to stable order is predictable rather than clever.

This lives in its own module because the alternative was the same twenty lines
in two places, which is how the string-classification logic ended up with four
different readings of the same tool result (see `action_loader.classify_result`
and `tests/test_verdict_honesty.py`).
"""
from __future__ import annotations

import re

WORD = re.compile(r"[a-z0-9]+")

#: Words that appear in almost every tool description and so carry no signal.
STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "is", "it",
    "this", "that", "with", "use", "used", "using", "you", "your", "user",
    "tool", "action", "actions", "when", "from", "by", "as", "at", "be", "can",
    "will", "any", "all", "if", "not", "no", "do", "does", "into", "out",
})


def stem(word: str) -> str:
    """Crude singulariser, enough to stop "documents" missing "document".

    Deliberately not a real stemmer: the corpora are short phrase lists, and
    the failure being fixed is a plural (or a trailing 'e') keeping two
    obviously-related words apart. Anything cleverer would need a dependency
    and would risk merging words that should stay distinct.
    """
    for suffix in ("ies", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def tokens(text: str) -> list[str]:
    """Significant words, lowercased and lightly stemmed. Order preserved."""
    return [stem(w) for w in WORD.findall(str(text or "").lower())
            if w not in STOP]
