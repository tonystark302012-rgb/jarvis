"""One cap on how much a single tool result may put into the conversation.

WHY THIS EXISTS
    Tool results are injected into the Live conversation verbatim. The caps in
    the tools themselves ranged from 2,500 to 40,000 characters, and a good
    number of tools had no cap at all — `file_processor` on a large document,
    `data_query` over a wide table, `scanner treemap` on a big tree. One call
    like that can spend most of a session's context on a single answer, and the
    symptom is not an error: the assistant just starts forgetting what was said
    a minute ago, which is close to impossible to trace back to the tool.

    So there is one backstop at the dispatch point. It is not a policy for how
    long a tool's answer should be — tools must still cap themselves, and the
    ones that do are capped well below this — it is the floor that stops one
    careless result from eating the session.

WHY A NOTE AND NOT SILENCE
    A model that is handed a silently truncated document will summarise the
    part it got and say nothing about the rest — a wrong answer that looks
    right. The note is part of the result: it says how much was dropped and
    what to do instead, and it is written for the model to read and act on.
"""
from __future__ import annotations

#: ~10k tokens of context. Above the largest deliberate cap in the codebase
#: (40,000), so no tool that already limits itself changes behaviour.
LIMIT = 40_000

#: The note is part of the payload, so it is written to earn its size. When the
#: limit is too small for the long form, a short one is used — and if even that
#: does not fit, an ellipsis, because "at most `limit` characters" is a promise.
_NOTE = ("\n\n[... truncated after {kept:,} of {total:,} characters — the result "
         "was larger than one tool call may return. Narrow the request (a "
         "specific file, page, folder or query) or send the rest to the "
         "content panel instead.]")
_NOTE_SHORT = "\n[...{kept:,}/{total:,} chars]"


def _note_for(limit: int, total: int) -> str:
    """The most informative note that still leaves room for the answer."""
    for template in (_NOTE, _NOTE_SHORT):
        worst = len(template.format(kept=total, total=total))
        if worst <= limit // 2:
            return template
    return "…"


def cap(text, limit: int = LIMIT) -> str:
    """Trim `text` to at most `limit` characters, saying what was dropped.

    The note counts against the limit — the promise is about the size of the
    payload, and a note that made the payload bigger would be a poor joke.

    Truncation prefers a line boundary, then a sentence, then a word — cutting
    mid-word in the middle of a table is how a result becomes unreadable in a
    way that looks like corruption.
    """
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text

    # worst case for the note: both numbers as long as the total
    template = _note_for(limit, len(text))
    reserve = len(template.format(kept=len(text), total=len(text)))
    budget = max(1, limit - reserve)

    head = text[:budget]
    for boundary in ("\n", ". ", " "):
        cut = head.rfind(boundary)
        # only take a boundary that keeps most of the budget — otherwise a
        # single early newline would throw away the answer
        if cut > budget * 0.6:
            head = head[:cut]
            break
    head = head.rstrip()
    return head + template.format(kept=len(head), total=len(text))


def needs_capping(text, limit: int = LIMIT) -> bool:
    return len(text) > limit if isinstance(text, str) else len(str(text)) > limit
