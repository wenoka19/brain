"""The Hybrid Omni-Bar router.

Checkpoint B, Rule 1: this is a **command bar, not a chat**. Three input paths
share one input, and which one you are on is decided here, not by the LLM:

* **typing** → a deterministic local filter. 0ms, no network, no model.
* **a URL or a dropped file** → an ingestion, queued immediately.
* **`Enter` on a natural-language command** → a *proposed* mutation. The router
  never executes; it returns a plan the user confirms with one tap, and
  execution returns a receipt carrying an undo descriptor.

Most commands resolve **deterministically** by grammar, which is why the bar
still works with no model configured. An LLM is consulted only when no pattern
matches, and even then it can only produce a plan — never an effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from app.config import Settings, settings
from app.core import engine as E
from app.db import tenant as tenant_db
from app.security.sanitizer import clean_text, truncate_words

MODE_FILTER = "filter"
MODE_INGEST = "ingest"
MODE_COMMAND = "command"
MODE_CLARIFY = "clarify"

SCOPES = {
    "@skills": "skills",
    "@skill": "skills",
    "@library": "claims",
    "@claims": "claims",
    "@everything": "all",
    "@all": "all",
    "@archived": "archived",
}

_URL_RE = re.compile(r"^(?:https?://|www\.)\S+$|^[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S*$", re.I)
_SCOPE_TOKEN_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_-]+)")
_TARGET_MATCH_FLOOR = 62

#: Words that mean "the thing I just added", resolved by recency.
RECENCY_WORDS = (
    "yesterday's", "yesterdays", "today's", "todays", "last", "latest",
    "most recent", "the last", "that", "this",
)
TYPE_WORDS = {
    "video": "youtube",
    "youtube": "youtube",
    "article": "article",
    "post": "article",
    "pdf": "pdf",
    "doc": "docx",
    "docx": "docx",
    "note": "text",
    "file": "text",
    "github": "github",
    "image": "image",
}


@dataclass
class OmnibarPlan:
    mode: str
    text: str = ""
    scope: str = "all"
    scope_slug: str = ""
    # command mode only
    action: str = ""
    target_kind: str = ""
    target_id: str = ""
    target_label: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    confirm_text: str = ""
    requires_confirmation: bool = False
    router: str = "deterministic"
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "text": self.text,
            "scope": self.scope,
            "scope_slug": self.scope_slug,
            "action": self.action,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "target_label": self.target_label,
            "params": self.params,
            "confirm_text": self.confirm_text,
            "requires_confirmation": self.requires_confirmation,
            "router": self.router,
            "message": self.message,
        }


@dataclass
class OmnibarReceipt:
    """What the one-line receipt bar renders, including its undo affordance."""

    ok: bool
    text: str
    undo_action: str = ""
    undo_params: dict[str, Any] = field(default_factory=dict)
    undo_label: str = ""
    error_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "undo_action": self.undo_action,
            "undo_params": self.undo_params,
            "undo_label": self.undo_label,
            "error_reason": self.error_reason,
        }


# ---------------------------------------------------------------------------
# scope + classification
# ---------------------------------------------------------------------------
def split_scope(raw: str) -> tuple[str, str, str]:
    """Pull `@` scope tokens out of the text. Returns (text, scope, slug)."""
    scope, slug = "all", ""
    text = raw or ""
    for match in list(_SCOPE_TOKEN_RE.finditer(text)):
        token = f"@{match.group(1)}".lower()
        if token in SCOPES:
            scope = SCOPES[token]
        else:
            slug = tenant_db.slugify(match.group(1))
            scope = "skills"
        text = text.replace(match.group(0), " ", 1)
    return " ".join(text.split()), scope, slug


def looks_like_url(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate or " " in candidate:
        return False
    if _URL_RE.match(candidate):
        return True
    parsed = urlsplit(candidate)
    return bool(parsed.scheme in ("http", "https") and parsed.netloc)


# ---------------------------------------------------------------------------
# deterministic command grammar
# ---------------------------------------------------------------------------
COMMAND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("edit_step", re.compile(r"^(?:edit|change|update)\s+step\s+(\d+)\s+(?:of|in|from)\s+(.+?)\s+to\s+(.+)$", re.I)),
    ("rename", re.compile(r"^rename\s+(.+?)\s+to\s+(.+)$", re.I)),
    ("restore", re.compile(r"^(?:restore|unarchive|bring back|un-archive)\s+(.+)$", re.I)),
    ("trash", re.compile(r"^(?:trash|bin|move)\s+(.+?)(?:\s+to\s+(?:the\s+)?trash)?$", re.I)),
    ("archive", re.compile(r"^(?:archive|hide|remove|delete|drop|get rid of)\s+(.+)$", re.I)),
    ("remember", re.compile(r"^(?:remember(?:\s+that)?|note that|my rule is)\s*[:,]?\s*(.+)$", re.I)),
    ("remember", re.compile(r"^((?:i\s+)?(?:always|never)\s+.+)$", re.I)),
    ("filter", re.compile(r"^(?:find|show me|show|search(?:\s+for)?|look up)\s+(.+)$", re.I)),
]


@dataclass
class ResolvedTarget:
    kind: str = ""
    id: str = ""
    label: str = ""
    score: float = 0.0


def resolve_target(ctx: E.Context, phrase: str, *, prefer: str = "") -> ResolvedTarget:
    """Resolve "yesterday's video" or "my cold email skill" to one real row.

    Recency words plus an optional type word resolve by `created_at`; anything
    else is fuzzy-matched against the user's own skill names, source titles and
    claim text. Nothing is guessed: below the match floor we return empty and
    the bar asks instead of acting.
    """
    text = clean_text(phrase).strip().strip("\"'").lower()
    if not text:
        return ResolvedTarget()

    wants_skill = prefer == "skill" or "skill" in text or "workflow" in text
    source_type = next((t for word, t in TYPE_WORDS.items() if word in text.split()), "")
    is_recency = any(word in text for word in RECENCY_WORDS)

    # ---- recency path -----------------------------------------------------
    if is_recency and not wants_skill:
        sql = "SELECT id, title FROM sources WHERE status = 'active'"
        params: list[Any] = []
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        row = ctx.tenant.execute(sql, params).fetchone()
        if row:
            return ResolvedTarget("source", row["id"], row["title"], 100.0)

    # ---- fuzzy path -------------------------------------------------------
    candidates: list[tuple[str, str, str]] = []
    for row in ctx.tenant.execute(
        "SELECT id, name, slug FROM skills WHERE status IN ('active','archived')"
    ).fetchall():
        candidates.append(("skill", row["id"], row["name"]))
        candidates.append(("skill", row["id"], row["slug"].replace("-", " ")))
    if not wants_skill:
        for row in ctx.tenant.execute(
            "SELECT id, title FROM sources WHERE status IN ('active','archived')"
        ).fetchall():
            candidates.append(("source", row["id"], row["title"]))
        for row in ctx.tenant.execute(
            "SELECT id, claim FROM epistemic_claims WHERE status IN ('active','archived') LIMIT 400"
        ).fetchall():
            candidates.append(("claim", row["id"], row["claim"]))

    best = ResolvedTarget()
    stripped = re.sub(
        r"\b(my|the|that|this|a|an|skill|workflow|video|article|pdf|note|file|claim)\b", " ", text
    )
    needle = " ".join(stripped.split()) or text
    for kind, item_id, label in candidates:
        score = max(
            fuzz.partial_ratio(needle, label.lower()),
            fuzz.token_set_ratio(needle, label.lower()),
        )
        if score > best.score:
            best = ResolvedTarget(kind, item_id, label, float(score))
    return best if best.score >= _TARGET_MATCH_FLOOR else ResolvedTarget()


def _confirm(action: str, target: ResolvedTarget, extra: str = "") -> str:
    verbs = {
        "archive": "Archive",
        "restore": "Restore",
        "trash": "Move to Trash",
        "rename": "Rename",
        "edit_step": "Edit",
    }
    label = truncate_words(target.label, 10)
    base = f"{verbs.get(action, action.title())} “{label}”"
    return f"{base} {extra}".strip() + "?"


def _deterministic_command(ctx: E.Context, text: str) -> OmnibarPlan | None:
    for action, pattern in COMMAND_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue

        if action == "filter":
            return OmnibarPlan(mode=MODE_FILTER, text=match.group(1))

        if action == "remember":
            rule = clean_text(match.group(1))
            return OmnibarPlan(
                mode=MODE_COMMAND,
                text=text,
                action="remember",
                target_kind="claim",
                target_label=truncate_words(rule, 12),
                params={"claim": rule},
                confirm_text=f"Save as your own rule: “{truncate_words(rule, 14)}”?",
                requires_confirmation=True,
                message="Your own rules carry the highest salience in your library.",
            )

        if action == "edit_step":
            step_number, skill_phrase, new_text = match.groups()
            target = resolve_target(ctx, skill_phrase, prefer="skill")
            if not target.id:
                return _unresolved(text, skill_phrase)
            return OmnibarPlan(
                mode=MODE_COMMAND,
                text=text,
                action="edit_step",
                target_kind="skill",
                target_id=target.id,
                target_label=target.label,
                params={"step": int(step_number), "new_text": clean_text(new_text)},
                confirm_text=_confirm(
                    "edit_step", target, f"step {step_number} → “{truncate_words(new_text, 10)}”"
                ),
                requires_confirmation=True,
            )

        if action == "rename":
            phrase, new_name = match.groups()
            target = resolve_target(ctx, phrase, prefer="skill")
            if not target.id or target.kind != "skill":
                return _unresolved(text, phrase)
            return OmnibarPlan(
                mode=MODE_COMMAND,
                text=text,
                action="rename",
                target_kind="skill",
                target_id=target.id,
                target_label=target.label,
                params={"name": clean_text(new_name)},
                confirm_text=_confirm("rename", target, f"→ “{truncate_words(new_name, 8)}”"),
                requires_confirmation=True,
            )

        phrase = match.group(1)
        target = resolve_target(ctx, phrase)
        if not target.id:
            return _unresolved(text, phrase)

        requested_delete = bool(re.match(r"^\s*delete\b", text, re.I))
        if action == "trash" and target.kind != "source":
            action = "archive"  # Trash is a source-level concept
        note = ""
        if requested_delete:
            # Never let a typed "delete" become a hard delete.
            action = "trash" if target.kind == "source" else "archive"
            note = (
                "Nothing is deleted outright — it goes to Trash, recoverable for 30 days. "
                "Permanent deletion lives in Profile → Data & Recovery."
            )
        return OmnibarPlan(
            mode=MODE_COMMAND,
            text=text,
            action=action,
            target_kind=target.kind,
            target_id=target.id,
            target_label=target.label,
            confirm_text=_confirm(action, target),
            requires_confirmation=True,
            message=note,
        )
    return None


def _unresolved(text: str, phrase: str) -> OmnibarPlan:
    return OmnibarPlan(
        mode=MODE_CLARIFY,
        text=text,
        message=(
            f"Nothing in your library clearly matches “{truncate_words(phrase, 10)}”. "
            "Filter for it first, then act on the card."
        ),
    )


# ---------------------------------------------------------------------------
# the router
# ---------------------------------------------------------------------------
#: An optional LLM fallback. It may only return a plan dict; it can never run
#: anything. Left unset until an extraction key is configured in prod.
LlmRouter = Callable[[str, dict[str, Any]], dict[str, Any] | None]
_llm_router: LlmRouter | None = None


def set_llm_router(router: LlmRouter | None) -> None:
    global _llm_router
    _llm_router = router


def route(
    ctx: E.Context, raw_text: str, *, submitted: bool = False, cfg: Settings | None = None
) -> OmnibarPlan:
    """Decide what this keystroke or submission means."""
    cfg = cfg or settings()
    text, scope, slug = split_scope(clean_text(raw_text or "")[:500])

    if looks_like_url(text):
        return OmnibarPlan(
            mode=MODE_INGEST,
            text=text,
            scope=scope,
            scope_slug=slug,
            confirm_text=f"Add {truncate_words(text, 8)} to your library?",
            requires_confirmation=False,  # a drop/paste is already the intent
        )

    if not submitted:
        # The 0ms path: typing only ever filters.
        return OmnibarPlan(mode=MODE_FILTER, text=text, scope=scope, scope_slug=slug)

    if not text:
        return OmnibarPlan(mode=MODE_FILTER, text="", scope=scope, scope_slug=slug)

    plan = _deterministic_command(ctx, text)
    if plan is not None:
        plan.scope, plan.scope_slug = scope, slug
        return plan

    if _llm_router is not None:
        proposal = _llm_router(text, {"scope": scope, "user_id": ctx.user_id})
        if proposal:
            merged = OmnibarPlan(
                mode=MODE_COMMAND,
                text=text,
                scope=scope,
                scope_slug=slug,
                router="llm",
                requires_confirmation=True,
                **{
                    key: proposal[key]
                    for key in ("action", "target_kind", "target_id", "target_label", "params", "confirm_text")
                    if key in proposal
                },
            )
            return merged

    # Nothing matched: searching is the safe default, and we say so rather than
    # silently doing nothing.
    return OmnibarPlan(
        mode=MODE_FILTER,
        text=text,
        scope=scope,
        scope_slug=slug,
        router="none",
        message="Showing matches — that did not read as a command.",
    )


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def execute(ctx: E.Context, plan: OmnibarPlan) -> tuple[OmnibarReceipt, E.OpResult | None]:
    """Run a confirmed plan and build its receipt, with the undo wired up."""
    if plan.mode == MODE_INGEST:
        result = E.op_ingest_source(ctx, plan.text)
        if not result.ok:
            return OmnibarReceipt(False, result.error_message, error_reason=result.error_reason), result
        if result.data.get("duplicate"):
            return (
                OmnibarReceipt(True, f"Already in your library — {result.data['title']}"),
                result,
            )
        return (
            OmnibarReceipt(True, f"Queued {plan.text} · {result.data['job_id']}"),
            result,
        )

    if plan.mode != MODE_COMMAND:
        return OmnibarReceipt(False, "Nothing to run.", error_reason="not_a_command"), None

    if plan.action == "remember":
        result = E.op_remember_this(
            ctx,
            plan.params.get("claim", ""),
            plan.params.get("when_it_applies", "") or "My own working rules",
        )
        if not result.ok:
            return OmnibarReceipt(False, result.error_message, error_reason=result.error_reason), result
        claim_id = result.data["claim"]["id"]
        conflicts = len(result.data.get("new_tensions") or [])
        suffix = f" · ⚡ disagrees with {conflicts}" if conflicts else ""
        return (
            OmnibarReceipt(
                True,
                f"Saved your rule{suffix}",
                undo_action="archive_claim",
                undo_params={"item_type": "claim", "id": claim_id},
                undo_label="Undo",
            ),
            result,
        )

    if plan.action in ("archive", "restore"):
        result = E.op_archive_item(
            ctx, plan.target_kind, plan.target_id, restore=plan.action == "restore"
        )
        if not result.ok:
            return OmnibarReceipt(False, result.error_message, error_reason=result.error_reason), result
        verb = "Restored" if plan.action == "restore" else "Archived"
        return (
            OmnibarReceipt(
                True,
                f"{verb} “{truncate_words(result.data['label'], 10)}”",
                undo_action="restore_item" if plan.action == "archive" else "archive_item",
                undo_params={"item_type": plan.target_kind, "id": plan.target_id},
                undo_label="Undo",
            ),
            result,
        )

    if plan.action == "trash":
        result = E.op_trash_item(ctx, "source", plan.target_id)
        if not result.ok:
            return OmnibarReceipt(False, result.error_message, error_reason=result.error_reason), result
        return (
            OmnibarReceipt(
                True,
                f"Moved “{truncate_words(result.data['label'], 10)}” to Trash · "
                "recoverable for 30 days",
                undo_action="restore_item",
                undo_params={"item_type": "source", "id": plan.target_id},
                undo_label="Undo",
            ),
            result,
        )

    if plan.action in ("edit_step", "rename"):
        skill = tenant_db.get_skill(ctx.tenant, plan.target_id)
        if skill is None:
            return OmnibarReceipt(False, "That skill no longer exists.", error_reason="not_found"), None
        previous_version = skill["version"]
        steps = list(skill["execution_steps"])
        name = skill["name"]

        if plan.action == "edit_step":
            index = int(plan.params.get("step", 0)) - 1
            if not 0 <= index < len(steps):
                return (
                    OmnibarReceipt(
                        False,
                        f"“{skill['name']}” has {len(steps)} steps, so there is no step "
                        f"{plan.params.get('step')}.",
                        error_reason="no_such_step",
                    ),
                    None,
                )
            steps[index] = plan.params["new_text"]
        else:
            name = plan.params["name"]

        result = E.op_save_skill(
            ctx,
            slug=skill["slug"],
            name=name,
            description=skill["description"],
            trigger_when=skill["trigger_when"],
            execution_steps=steps,
            guardrails=skill["guardrails"],
            output_template=skill["output_template"],
            linked_claim_ids=skill["linked_claim_ids"],
            origin_type=skill["origin_type"],
        )
        if not result.ok:
            return OmnibarReceipt(False, result.error_message, error_reason=result.error_reason), result
        updated = result.data["skill"]
        what = (
            f"step {plan.params['step']} of “{truncate_words(skill['name'], 8)}”"
            if plan.action == "edit_step"
            else f"“{truncate_words(skill['name'], 8)}” → “{truncate_words(name, 8)}”"
        )
        return (
            OmnibarReceipt(
                True,
                f"Updated {what} · now v{updated['version']}",
                undo_action="revert_skill",
                undo_params={"skill_id": updated["id"], "version": previous_version},
                undo_label=f"Revert to v{previous_version}",
            ),
            result,
        )

    return OmnibarReceipt(False, f"`{plan.action}` is not a supported command.", error_reason="unsupported_action"), None


def undo(ctx: E.Context, action: str, params: dict[str, Any]) -> OmnibarReceipt:
    """Reverse whatever the receipt bar is offering to reverse."""
    if action == "restore_item":
        result = E.op_archive_item(ctx, params["item_type"], params["id"], restore=True)
    elif action in ("archive_item", "archive_claim"):
        result = E.op_archive_item(ctx, params["item_type"], params["id"])
    elif action == "revert_skill":
        result = E.op_revert_skill(ctx, params["skill_id"], int(params["version"]))
    else:
        return OmnibarReceipt(False, f"`{action}` cannot be undone.", error_reason="not_undoable")

    if not result.ok:
        return OmnibarReceipt(False, result.error_message, error_reason=result.error_reason)
    if action == "revert_skill":
        return OmnibarReceipt(True, f"Reverted to v{params['version']}")
    return OmnibarReceipt(True, "Undone")
