"""Agent-Native UX renderers — the exact envelopes locked at Checkpoint A.

See `DESIGN_DECISIONS.md`. The rules this module exists to enforce:

* **No Markdown tables, ever.** A pipe table cannot hold a verbatim
  `anchor_quote` plus `when_it_fails`, and it collapses at three or more
  disagreeing sources. Every claim is the same three-line prose block.
* **Conflicts are hoisted to the top**, never buried under matched claims.
* **No time estimates at enqueue time.** A reasoning model reads an ETA as
  permission to wait or poll.
* **Library content is fenced; `[AGENT_HINT]` lives outside the fence**, so a
  hostile transcript can neither forge a hint nor be mistaken for one.
* **Token-disciplined.** Every envelope is trimmed to stay under the budget.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from app.config import Settings, settings
from app.core.engine import OpResult
from app.security.sanitizer import neutralize_fences, truncate_words, wrap_untrusted

#: Envelope ceiling from CLAUDE.md §1.2 (AX): keep the host context window clean.
TOKEN_BUDGET = 1500
#: 4 characters per token is the standard rough estimate for English Markdown.
CHARS_PER_TOKEN = 4
#: Beyond this, hoisted conflicts would starve MATCHED CLAIMS of the budget.
MAX_DISAGREEMENT_SECTIONS = 3

HINT = "[AGENT_HINT]"

#: Phrasings the extractor emits when a source stated no condition at all.
#: They are honest in a claim block but name no variable, so they can never
#: stand in for a deciding variable.
CONDITION_PLACEHOLDERS = ("working on", "not stated by the source", "not stated")

#: Locked verbatim at Checkpoint A, Touchpoint 1.
INGEST_HINT = (
    "Background job queued. Do not poll or wait. Acknowledge in one short clause "
    "(under 10 words) and immediately continue the user's current task."
)

EVIDENCE_LABELS = {
    "data": "data",
    "case_study": "case study",
    "anecdote": "anecdote",
    "theory": "theory",
    "opinion": "opinion",
    "user_axiom": "your own rule",
}

LINK_VERBS = {
    "contradicts": "contradicts",
    "refines": "refines",
    "supersedes": "supersedes",
}

#: Phrases that reveal *what* a boundary turns on, used to name the deciding
#: variable in the disagreement header.
_THRESHOLD_RE = re.compile(
    r"\b(?:above|below|under|over|past|beyond|more than|less than|at least|up to)\s+"
    r"([0-9][0-9,.]*\s*[%a-z]*(?:\s+(?:per|a|an|each)\s+[a-z]+)?)",
    re.I,
)
_UNIT_HINTS = (
    ("sends", "daily send volume"),
    ("send", "daily send volume"),
    ("emails", "daily email volume"),
    ("email", "daily email volume"),
    ("per day", "daily volume"),
    ("a day", "daily volume"),
    ("people", "team size"),
    ("person", "team size"),
    ("team", "team size"),
    ("seats", "account size"),
    ("users", "account size"),
    ("%", "the measured rate"),
)


def estimate_tokens(text: str) -> int:
    return (len(text or "") + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# ===========================================================================
# shared pieces
# ===========================================================================
def locator_label(locator_type: str, locator_value: str) -> str:
    """Human-readable citation label for a deep link."""
    value = str(locator_value or "")
    if locator_type == "timestamp" and value.isdigit():
        total = int(value)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"
    if locator_type == "page" and value.isdigit():
        return f"p.{value}"
    if locator_type == "section" and value:
        return truncate_words(value, 6)
    return "source"


def _speaker(claim: dict[str, Any]) -> str:
    source = claim.get("source") or {}
    return (
        claim.get("speaker")
        or source.get("author")
        or claim.get("source_author")
        or source.get("title")
        or claim.get("source_title")
        or "Unattributed"
    )


def _source_title(claim: dict[str, Any]) -> str:
    source = claim.get("source") or {}
    return source.get("title") or claim.get("source_title") or "untitled source"


def claim_block(claim: dict[str, Any], *, quote_words: int = 40) -> str:
    """The locked three-line prose block. Nothing here is ever dropped.

    Line 1 — who said it, on what evidence, how salient.
    Line 2 — the claim and its explicit boundaries.
    Line 3 — the verbatim quote, a clickable locator, and the id to `fetch`.
    """
    basis = EVIDENCE_LABELS.get(claim.get("evidence_basis", ""), claim.get("evidence_basis", ""))
    salience = claim.get("salience", 3)
    header = f"**{_speaker(claim)}** (*{_source_title(claim)}*, {basis} · salience {salience}/5)"

    body = (claim.get("claim") or "").strip().rstrip(".")
    applies = (claim.get("when_it_applies") or "").strip().rstrip(".")
    fails = (claim.get("when_it_fails") or "").strip().rstrip(".")
    metrics = (claim.get("metrics_and_quotes") or "").strip()

    # A weak extractor can derive both conditions from the same clause. Printing
    # "Works when: X. Breaks when: X." reads as nonsense, and the failure sense
    # is the truthful one, so the duplicate `Works when` is dropped.
    if applies and fails and _same_condition(applies, fails):
        applies = ""

    parts = [body]
    if applies:
        parts.append(f" — **Works when:** {applies}.")
    else:
        parts.append(".")
    if fails:
        parts.append(f" **Breaks when:** {fails}.")
    if metrics:
        parts.append(f" {metrics}")
    middle = "".join(parts)

    quote = truncate_words(claim.get("anchor_quote") or "", quote_words)
    label = locator_label(claim.get("locator_type", "none"), claim.get("locator_value", ""))
    url = claim.get("deep_link") or (claim.get("source") or {}).get("url") or ""
    citation = f"[{label}]({url})" if url else f"{label} (no link)"
    proof = f'→ "{quote}" ({citation}) · `{claim["id"]}`'

    return f"{header}\n{middle}\n{proof}"


def _same_condition(left: str, right: str) -> bool:
    def norm(text: str) -> set[str]:
        return {word for word in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(word) > 2}

    a, b = norm(left), norm(right)
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) > 0.8


def _counterpart_claim(tension: dict[str, Any]) -> dict[str, Any]:
    """Re-shape a tension record into a normal claim dict for `claim_block`."""
    return {
        "id": tension["other_id"],
        "claim": tension.get("other_claim", ""),
        "speaker": tension.get("other_speaker", ""),
        "evidence_basis": tension.get("other_evidence_basis", ""),
        "when_it_applies": tension.get("other_when_it_applies", ""),
        "when_it_fails": tension.get("other_when_it_fails", ""),
        "metrics_and_quotes": tension.get("other_metrics_and_quotes", ""),
        "salience": tension.get("other_salience", 3),
        "anchor_quote": tension.get("other_anchor_quote", ""),
        "locator_type": tension.get("other_locator_type", "none"),
        "locator_value": tension.get("other_locator_value", ""),
        "deep_link": tension.get("other_deep_link", ""),
        "source_title": tension.get("other_source_title", ""),
    }


def deciding_variable(claim: dict[str, Any], counterpart: dict[str, Any]) -> str:
    """Name what the disagreement turns on, or admit that it is not yet clear.

    Derived from the two sides' boundary conditions — a quantified threshold
    first ("above 500 emails per day" -> "daily email volume"), then the raw
    condition phrase. If neither side states a condition we say so instead of
    inventing a confident label.
    """
    haystacks = [
        f"{side.get('when_it_fails', '')} {side.get('when_it_applies', '')} {side.get('claim', '')}"
        for side in (claim, counterpart)
    ]
    for text in haystacks:
        match = _THRESHOLD_RE.search(text or "")
        if not match:
            continue
        threshold = " ".join(match.group(1).split())
        lowered = (text or "").lower()
        for needle, label in _UNIT_HINTS:
            if needle in lowered:
                return f"{label} (around {threshold})"
        return f"a threshold around {threshold}"

    for side in (claim, counterpart):
        for field in ("when_it_fails", "when_it_applies"):
            value = (side.get(field) or "").strip()
            # Generated boilerplate names no variable.
            if value and not value.lower().startswith(CONDITION_PLACEHOLDERS):
                return truncate_words(value, 10)
    return "not stated by either source — compare the conditions below"


# ===========================================================================
# Touchpoint 1 — the mid-chat ingestion reply
# ===========================================================================
def ingestion_reply(result: OpResult) -> str:
    """Zero-Distraction Receipt. Echo what was understood; never estimate time."""
    if not result.ok:
        return refusal(result)

    data = result.data
    if data.get("duplicate"):
        title = neutralize_fences(data.get("title") or "that source")
        claims = data.get("claim_count", 0)
        return (
            f"✔️ **Already in your library** — *{truncate_words(title, 12)}* "
            f"· {claims} claim{'s' if claims != 1 else ''} · `{data['source_id']}`\n"
            f"{HINT} Nothing was queued; this source is already processed. "
            f"Tell the user in one short clause and continue their current task. "
            f"Use `search` to pull its claims."
        )

    label = _enqueue_label(data)
    note = (data.get("user_note") or "").strip()
    note_part = f" · note: \"{truncate_words(neutralize_fences(note), 14)}\"" if note else ""
    return (
        f"⚡ **Queued** — {label}{note_part} · `{data['job_id']}`\n"
        f"{HINT} {INGEST_HINT}"
    )


def _enqueue_label(data: dict[str, Any]) -> str:
    """What we can honestly say at enqueue time: nothing has been fetched yet."""
    source_type = data.get("source_type", "source")
    if "filename" in data:
        return f"{source_type} upload `{data['filename']}`"
    url = data.get("canonical_url", "")
    if source_type == "youtube":
        video_id = url.rsplit("v=", 1)[-1] if "v=" in url else url
        return f"YouTube video `{video_id}`"
    if source_type == "github":
        return f"GitHub file `{url.rsplit('/', 1)[-1]}`"
    return f"{source_type} `{truncate_words(url, 8)}`"


# ===========================================================================
# Touchpoint 2 — the knowledge & contradiction envelope
# ===========================================================================
def search_envelope(result: OpResult, *, cfg: Settings | None = None) -> str:
    """Hoisted prose briefing: disagreements first, then matched claims."""
    cfg = cfg or settings()
    if not result.ok:
        return refusal(result)

    data = result.data
    claims: list[dict[str, Any]] = list(data.get("claims") or [])
    skills: list[dict[str, Any]] = list(data.get("skills") or [])
    query = truncate_words(data.get("query", ""), 12)

    if not claims and not skills:
        return _empty_library_envelope(query, data)

    sections: list[str] = []
    disagreements, hoisted_ids = _disagreement_sections(claims)
    sections.extend(disagreements)

    remaining = [claim for claim in claims if claim["id"] not in hoisted_ids]
    if remaining:
        sections.append(
            "## MATCHED CLAIMS\n\n" + "\n\n".join(claim_block(claim) for claim in remaining)
        )
    if skills:
        sections.append(
            "## MATCHED SKILLS\n\n" + "\n\n".join(skill_summary_line(skill) for skill in skills)
        )

    body = _fit_budget("\n\n".join(sections), hint=_search_hint(claims, skills, bool(disagreements)))
    return body


def _disagreement_sections(claims: Sequence[dict[str, Any]]) -> tuple[list[str], set[str]]:
    """One hoisted section per tension link, deduplicated across both sides."""
    sections: list[str] = []
    seen_links: set[str] = set()
    hoisted: set[str] = set()

    for claim in claims:
        for tension in claim.get("tensions") or []:
            # A link is reported from both sides, so the key must not depend on
            # which side we happen to be iterating.
            link_id = tension.get("link_id") or ":".join(
                sorted((claim["id"], tension["other_id"]))
            )
            if link_id in seen_links:
                continue
            seen_links.add(link_id)
            if len(sections) >= MAX_DISAGREEMENT_SECTIONS:
                continue
            counterpart = _counterpart_claim(tension)
            variable = deciding_variable(claim, counterpart)
            verb = LINK_VERBS.get(tension.get("link_type", ""), tension.get("link_type", ""))
            heading = (
                f"## ⚡ UNRESOLVED DISAGREEMENT (Deciding variable: {variable})"
                if verb == "contradicts"
                else f"## ⚡ BOUNDARY DISAGREEMENT — one side {verb} the other "
                f"(Deciding variable: {variable})"
            )
            summary = neutralize_fences(tension.get("divergence_summary") or "")
            sections.append(
                "\n\n".join(
                    part
                    for part in (
                        heading,
                        claim_block(claim),
                        claim_block(counterpart),
                        f"**Why they diverge:** {summary}" if summary else "",
                    )
                    if part
                )
            )
            hoisted.add(claim["id"])
            hoisted.add(counterpart["id"])

    extra = len(seen_links) - len(sections)
    if extra > 0:
        sections.append(
            f"*({extra} further disagreement(s) in your library on this query — "
            "`fetch` a claim id to see them.)*"
        )
    return sections, hoisted


def _search_hint(
    claims: Sequence[dict[str, Any]], skills: Sequence[dict[str, Any]], has_conflict: bool
) -> str:
    counts = f"{len(claims)} claim{'s' if len(claims) != 1 else ''}"
    if skills:
        counts += f" and {len(skills)} skill{'s' if len(skills) != 1 else ''}"
    if has_conflict:
        return (
            f"{HINT} {counts} returned, with an unresolved disagreement shown first. "
            "State both sides to the user with their attributions — do not pick a winner "
            "silently. Name the deciding variable from the `Works when` / `Breaks when` "
            "conditions and ask the user where their own context sits on it. Cite claims "
            "using the bracketed locator link. Call `fetch(<claim_id>)` for the full record."
        )
    return (
        f"{HINT} {counts} returned, no conflicts. Cite each claim using its bracketed "
        "locator link and attribute it to the named speaker. Call `fetch(<claim_id>)` for "
        "the full record, or `get_or_synthesize_skill` to turn these into a workflow."
    )


def _empty_library_envelope(query: str, data: dict[str, Any]) -> str:
    counts = data.get("library_counts") or {}
    total = counts.get("claims", 0)
    if total == 0:
        return (
            f'No claims in the library yet — nothing matches "{query}".\n'
            f"{HINT} The library is empty. Answer from your own knowledge and say plainly "
            "that nothing came from the user's library. Offer `ingest_source` if they have "
            "a video, article or PDF on this."
        )
    return (
        f'Nothing in the library matches "{query}" ({total} claims searched).\n'
        f"{HINT} No match. Answer from your own knowledge and say the library had nothing "
        "on this. Do not retry the same query — try different wording only if the user asks."
    )


def fetch_envelope(result: OpResult) -> str:
    if not result.ok:
        return refusal(result)

    data = result.data
    kind = data.get("kind")

    if kind == "claim":
        claim = dict(data["claim"])
        source = data.get("source") or {}
        claim["source"] = {
            "title": source.get("title", ""),
            "url": source.get("canonical_url", ""),
            "author": source.get("author", ""),
        }
        claim["deep_link"] = data.get("deep_link", "")
        tensions = data.get("tensions") or []
        sections = [claim_block(claim, quote_words=40)]
        for tension in tensions:
            counterpart = _counterpart_claim(tension)
            verb = LINK_VERBS.get(tension.get("link_type", ""), "conflicts with")
            sections.append(
                f"## ⚡ This claim {verb} another in your library "
                f"(Deciding variable: {deciding_variable(claim, counterpart)})\n\n"
                + claim_block(counterpart)
                + (
                    f"\n\n**Why they diverge:** {neutralize_fences(tension['divergence_summary'])}"
                    if tension.get("divergence_summary")
                    else ""
                )
            )
        tags = ", ".join(claim.get("domain_tags") or []) or "untagged"
        steps = claim.get("exact_steps") or []
        if steps:
            sections.append(
                "**Exact steps recorded with this claim:**\n"
                + "\n".join(f"{i}. {step}" for i, step in enumerate(steps, start=1))
            )
        sections.append(f"*Tags: {tags} · captured {claim.get('created_at', '')}*")
        hint = (
            f"{HINT} Full record for one claim"
            + (
                ". It conflicts with another claim shown above — present both sides and ask "
                "the user which conditions match theirs."
                if tensions
                else ". Quote the verbatim text above and cite the locator link."
            )
        )
        return _fit_budget("\n\n".join(sections), hint=hint)

    if kind == "skill":
        return skill_envelope(
            OpResult(
                ok=True,
                action="get_or_synthesize_skill",
                data={
                    "origin": "saved",
                    "skill": data["skill"],
                    "linked_claims": data.get("linked_claims") or [],
                },
            )
        )

    source = data["source"]
    claims = data.get("claims") or []
    header = (
        f"**{neutralize_fences(source['title'])}** "
        f"({source['source_type']}{', ' + source['author'] if source['author'] else ''}) "
        f"· {len(claims)} claim{'s' if len(claims) != 1 else ''} · `{source['id']}`"
    )
    blocks = [
        claim_block({**claim, "source_title": source["title"], "deep_link": ""})
        for claim in claims
    ]
    return _fit_budget(
        header + ("\n\n## CLAIMS FROM THIS SOURCE\n\n" + "\n\n".join(blocks) if blocks else ""),
        hint=f"{HINT} Everything extracted from one source. Cite individual claims by id.",
    )


# ===========================================================================
# Touchpoint 3 — skills: contract vs sketch
# ===========================================================================
def skill_summary_line(skill: dict[str, Any]) -> str:
    steps = skill.get("execution_steps") or []
    return (
        f"**{skill['name']}** · `{skill['slug']}` · v{skill.get('version', 1)} "
        f"· {len(steps)} step{'s' if len(steps) != 1 else ''}\n"
        f"Reach for it when: {skill.get('trigger_when', '')}"
    )


def skill_envelope(result: OpResult) -> str:
    if not result.ok:
        return refusal(result)

    data = result.data
    origin = data.get("origin")
    if origin == "saved":
        return _saved_skill(data)
    if origin == "synthesized":
        return _draft_skill(data)
    return _no_skill(data)


def _saved_skill(data: dict[str, Any]) -> str:
    """A contract: execute as written, with the receipts attached."""
    skill = data["skill"]
    linked = {claim["id"]: claim for claim in (data.get("linked_claims") or [])}
    steps = skill.get("execution_steps") or []
    claim_ids = list(linked.keys()) or list(skill.get("linked_claim_ids") or [])

    lines = [
        f"✅ **SAVED SKILL — {skill['name']}** · `{skill['slug']}` · v{skill.get('version', 1)} "
        f"· {skill.get('origin_type', 'distilled')}",
        f"**Trigger:** {skill.get('trigger_when', '')}",
    ]
    if skill.get("description"):
        lines.append(f"**What it is:** {skill['description']}")
    lines.append("")
    for index, step in enumerate(steps, start=1):
        tag = f" [{claim_ids[index - 1]}]" if index - 1 < len(claim_ids) else ""
        lines.append(f"{index}. {step}{tag}")
    if skill.get("guardrails"):
        lines.append(f"\n**Guardrails:** {skill['guardrails']}")
    if skill.get("output_template"):
        lines.append(f"**Output shape:** {skill['output_template']}")
    if claim_ids:
        lines.append(
            "\n**Backed by:** " + " · ".join(f"`{cid}`" for cid in claim_ids[:8])
        )
    also = data.get("also_matched") or []
    if also:
        lines.append(
            "\n*Also matched:* "
            + ", ".join(f"`{item['slug']}`" for item in also)
        )

    hint = (
        f"{HINT} This is the user's locked-in workflow, version "
        f"{skill.get('version', 1)}. Execute these steps as written — do not reorder, "
        "merge or improvise them, and respect the guardrails. If the user asks why a step "
        "exists, call `fetch` on that step's bracketed claim id. If reality contradicts a "
        "step, say so and offer to amend the skill via `save_skill`."
    )
    return _fit_budget("\n".join(lines), hint=hint, fence=False)


def _draft_skill(data: dict[str, Any]) -> str:
    """A sketch: a proposal with provenance, disputes and gaps on the face of it."""
    draft = data["draft_skill"]
    supporting = {claim["id"]: claim for claim in (data.get("supporting_claims") or [])}
    steps = draft.get("execution_steps") or []
    linked_ids = list(draft.get("linked_claim_ids") or [])

    lines = [
        f"✏️ **DRAFT — NOT SAVED** · synthesised from {len(supporting)} claim"
        f"{'s' if len(supporting) != 1 else ''} in your library",
        f"**Proposed name:** {draft.get('name', '')} (`{draft.get('slug', '')}`)",
        f"**Would trigger:** {draft.get('trigger_when', '')}",
        "",
    ]

    disputed = 0
    for index, step in enumerate(steps, start=1):
        claim = supporting.get(linked_ids[index - 1]) if index - 1 < len(linked_ids) else None
        if claim is None:
            claim = _match_step_to_claim(step, supporting)
        attribution = ""
        warning = ""
        if claim:
            label = locator_label(claim.get("locator_type", "none"), claim.get("locator_value", ""))
            url = claim.get("deep_link") or ""
            cite = f"[{label}]({url})" if url else label
            attribution = f" — *{_speaker(claim)}, {cite}* · `{claim['id']}`"
            if claim.get("tensions"):
                disputed += 1
                other = claim["tensions"][0]
                warning = (
                    f"\n   ⚠️ **disputed** by {other.get('other_speaker') or 'another source'}: "
                    f"{truncate_words(other.get('other_claim', ''), 18)}"
                )
        lines.append(f"{index}. {step}{attribution}{warning}")

    gap = _gap_line(draft, supporting)
    if gap:
        lines.append(f"\n**Gap:** {gap}")
    if draft.get("guardrails"):
        lines.append(f"**Proposed guardrails:** {truncate_words(draft['guardrails'], 40)}")

    hint = (
        f"{HINT} This is a DRAFT, not a saved skill — nothing has been written to the "
        "library. Present it to the user as a proposal in your own words"
        + (
            f", flag the {disputed} disputed step{'s' if disputed != 1 else ''} explicitly"
            if disputed
            else ""
        )
        + (", name the gap," if gap else "")
        + " then ask whether to lock it in. Only call `save_skill` after they say yes."
    )
    return _fit_budget("\n".join(lines), hint=hint, fence=False)


def _match_step_to_claim(step: str, supporting: dict[str, Any]) -> dict[str, Any] | None:
    """Steps are lifted from claim text, so match a step back to its origin."""
    needle = (step or "").lower().strip()
    for claim in supporting.values():
        text = (claim.get("claim") or "").lower()
        if needle and (needle in text or text in needle):
            return claim
        for recorded in claim.get("exact_steps") or []:
            if needle == str(recorded).lower().strip():
                return claim
    return None


def _gap_line(draft: dict[str, Any], supporting: dict[str, Any]) -> str:
    """Say what the library does not cover, rather than papering over it."""
    gaps: list[str] = []
    if len(supporting) < 3:
        gaps.append(
            f"only {len(supporting)} claim{'s' if len(supporting) != 1 else ''} back this, "
            "so the workflow is thin"
        )
    if not any(claim.get("exact_steps") for claim in supporting.values()):
        gaps.append("no source recorded explicit steps, so the ordering is inferred")
    if not any(claim.get("when_it_fails") for claim in supporting.values()):
        gaps.append("no source stated where this fails, so there are no real guardrails yet")
    bases = {claim.get("evidence_basis") for claim in supporting.values()}
    if bases and bases <= {"opinion", "anecdote"}:
        gaps.append("every backing claim is opinion or anecdote, with no data behind it")
    return "; ".join(gaps)


def _no_skill(data: dict[str, Any]) -> str:
    task = truncate_words(data.get("task_description", ""), 14)
    return (
        f'No saved skill and nothing in the library to synthesise one from, for: "{task}".\n'
        f"{HINT} The library has no coverage here. Help the user from your own knowledge, "
        "say plainly that this did not come from their library, and offer `ingest_source` "
        "if they have a source on it, or `save_skill` to record the workflow you build "
        "together."
    )


# ===========================================================================
# the remaining tool surfaces
# ===========================================================================
def remember_envelope(result: OpResult) -> str:
    if not result.ok:
        return refusal(result)
    claim = result.data["claim"]
    tensions = result.data.get("new_tensions") or []
    counts = result.data.get("library_counts") or {}
    lines = [
        f"📌 **Saved as your own rule** · `{claim['id']}` · salience 5/5",
        f"{claim['claim']} — **Works when:** {claim['when_it_applies']}."
        + (f" **Breaks when:** {claim['when_it_fails']}." if claim.get("when_it_fails") else ""),
    ]
    if tensions:
        lines.append(
            f"\n⚡ This disagrees with {len(tensions)} claim"
            f"{'s' if len(tensions) != 1 else ''} already in your library:"
        )
        for tension in tensions[:3]:
            lines.append(f"- {neutralize_fences(tension['divergence_summary'])}")
    lines.append(f"\n*Library: {counts.get('claims', 0)} claims · {counts.get('skills', 0)} skills*")
    hint = (
        f"{HINT} Recorded. Confirm in one short clause"
        + (
            ". It conflicts with existing claims — tell the user which, and that their own "
            "rule now outranks them for their own context. Do not archive anything on your own."
            if tensions
            else " and continue the user's current task."
        )
    )
    return _fit_budget("\n".join(lines), hint=hint, fence=False)


def status_envelope(result: OpResult) -> str:
    if not result.ok:
        return refusal(result)
    jobs = result.data.get("jobs") or []
    if not jobs:
        return (
            f"No ingestion jobs on record.\n"
            f"{HINT} Nothing queued or processing. Continue the user's current task."
        )

    icons = {
        "queued": "⏳",
        "fetching": "⏳",
        "extracting": "⏳",
        "linking": "⏳",
        "ready": "✅",
        "failed": "❌",
        "blocked_by_platform": "🚫",
    }
    lines = []
    for job in jobs:
        icon = icons.get(job["status"], "•")
        label = neutralize_fences(job.get("title") or job.get("input_target", ""))
        detail = ""
        if job["status"] == "ready":
            detail = f" · {job['claims_extracted']} claims · `{job['source_id']}`"
        elif job.get("error_message"):
            detail = f" · {neutralize_fences(job['error_message'])}"
        lines.append(f"{icon} **{job['status']}** — {truncate_words(label, 12)}{detail}")

    pending = [job for job in jobs if job["status"] not in ("ready", "failed", "blocked_by_platform")]
    if pending:
        hint = (
            f"{HINT} {len(pending)} job(s) still processing. Do not poll again in this turn "
            "and do not wait — tell the user it is still running and continue their current task."
        )
    elif any(job["status"] in ("failed", "blocked_by_platform") for job in jobs):
        hint = (
            f"{HINT} A job did not complete. Relay the reason to the user verbatim and do "
            "not retry the same source automatically — ask them first."
        )
    else:
        hint = f"{HINT} All done. Use `search` to pull the new claims."
    return _fit_budget("\n".join(lines), hint=hint, fence=False)


def archive_envelope(result: OpResult) -> str:
    if not result.ok:
        return refusal(result)
    data = result.data
    label = truncate_words(neutralize_fences(data.get("label") or data["id"]), 14)
    if data["status"] == "archived":
        return (
            f"🗄️ **Archived** {data['item_type']} — {label} · `{data['id']}`\n"
            f"{HINT} Soft-archived and fully recoverable; it will no longer appear in "
            "`search`. Nothing was deleted — permanent deletion is only possible by a human "
            "click in the web app. Confirm in one short clause."
        )
    return (
        f"♻️ **Restored** {data['item_type']} — {label} · `{data['id']}`\n"
        f"{HINT} Active again and searchable. Confirm in one short clause."
    )


def save_skill_envelope(result: OpResult) -> str:
    if not result.ok:
        return refusal(result)
    skill = result.data["skill"]
    created = result.data.get("created")
    verb = "Saved" if created else f"Updated to v{skill['version']}"
    lines = [
        f"✅ **{verb}** — **{skill['name']}** · `{skill['slug']}` · v{skill['version']}",
        f"**Trigger:** {skill['trigger_when']}",
    ]
    for index, step in enumerate(skill.get("execution_steps") or [], start=1):
        lines.append(f"{index}. {step}")
    if skill.get("guardrails"):
        lines.append(f"\n**Guardrails:** {skill['guardrails']}")
    if skill.get("linked_claim_ids"):
        lines.append(
            "\n**Backed by:** "
            + " · ".join(f"`{cid}`" for cid in skill["linked_claim_ids"][:8])
        )
    hint = (
        f"{HINT} This is now the user's locked-in workflow — reach for it by slug "
        f"`{skill['slug']}` next time instead of improvising. "
        + (
            f"The previous version was snapshotted, so v{skill['version'] - 1} is recoverable."
            if not created
            else "Confirm in one short clause."
        )
    )
    return _fit_budget("\n".join(lines), hint=hint, fence=False)


# ===========================================================================
# refusals and budget
# ===========================================================================
#: What the agent should do next, per refusal reason. A tool error that does not
#: say "stop" is an invitation for a reasoning model to retry in a loop.
REFUSAL_HINTS = {
    "quota_exceeded": (
        "The user is out of quota. Do not retry. Tell them the number and that they can "
        "archive sources in the web app to free room."
    ),
    "playlist_blocked": "Out of scope. Ask the user for a single video link instead of retrying.",
    "livestream_blocked": "Out of scope. Do not retry — ask them to send it once published.",
    "raw_media_blocked": "Out of scope. Ask for a YouTube link or a document instead.",
    "too_long": "Over the length ceiling. Do not retry. Ask for a shorter source.",
    "private_address": (
        "That link resolves to a private address and was refused for security. Do not retry "
        "or rewrite the URL. Tell the user it must be a publicly reachable link."
    ),
    "blocked_url": (
        "That link was refused for security. Do not retry or rewrite it. Ask the user for a "
        "publicly reachable link."
    ),
    "file_too_large": "Over the upload cap. Do not retry. Ask for a smaller file.",
    "zip_bomb": "That file was refused as unsafe. Do not retry it.",
    "content_type_mismatch": (
        "The file's contents do not match its extension and it was refused. Do not retry."
    ),
    "unsupported_file": "Unsupported type. Ask the user for a PDF, DOCX, TXT, MD or image.",
    "not_found": (
        "No such id. Do not guess another id — run `search` to get valid ids, or tell the "
        "user it is not in their library."
    ),
    "empty_query": "Ask the user what to search for; do not invent a query.",
    "no_steps": "Ask the user for the actual steps before calling `save_skill` again.",
    "no_trigger": "Ask the user when this skill should be reached for, then retry once.",
    "no_name": "Ask the user what to call this skill, then retry once.",
    "missing_when_it_applies": (
        "A remembered rule needs its conditions. Ask the user when it applies, then retry once."
    ),
    "empty_claim": "Nothing to remember. Ask the user what they want recorded.",
}
DEFAULT_REFUSAL_HINT = (
    "This did not work. Relay the reason to the user and ask how to proceed rather than "
    "retrying the same call."
)


def refusal(result: OpResult) -> str:
    """Every error carries an explicit next move, so a loop never starts."""
    reason = result.error_reason or "error"
    guidance = REFUSAL_HINTS.get(reason, DEFAULT_REFUSAL_HINT)
    return (
        f"⚠️ **Could not {result.action.replace('_', ' ')}** — {result.error_message}\n"
        f"{HINT} {guidance}"
    )


def circuit_breaker(decision) -> str:
    """The polite stop handed to a runaway agent loop."""
    return (
        f"🛑 **Paused** — {decision.message}\n"
        f"{HINT} Stop calling this tool. Use what you already have, or tell the user what "
        "you need from them. Do not retry before "
        f"{max(1, int(decision.retry_after))}s."
    )


def _fit_budget(body: str, *, hint: str, fence: bool = True) -> str:
    """Assemble the envelope and trim the *body* until it fits the budget.

    The hint and the security header are never trimmed: losing them is worse
    than losing a claim, because the agent then has no instructions and the
    library content loses its spotlighting.
    """
    overhead = estimate_tokens(hint) + (estimate_tokens(wrap_untrusted("")) if fence else 0) + 16
    allowance = max(200, TOKEN_BUDGET - overhead)

    trimmed = body
    if estimate_tokens(trimmed) > allowance:
        blocks = trimmed.split("\n\n")
        kept: list[str] = []
        used = 0
        for block in blocks:
            cost = estimate_tokens(block) + 1
            if used + cost > allowance:
                break
            kept.append(block)
            used += cost
        dropped = len(blocks) - len(kept)
        trimmed = "\n\n".join(kept) or blocks[0][: allowance * CHARS_PER_TOKEN]
        if dropped > 0:
            trimmed += (
                f"\n\n*({dropped} further block(s) omitted to stay within the context budget — "
                "narrow the query or `fetch` a specific id.)*"
            )

    return f"{wrap_untrusted(trimmed)}\n\n{hint}" if fence else f"{trimmed}\n\n{hint}"
