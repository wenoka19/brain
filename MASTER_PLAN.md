# BRAIN — MASTER PLAN

Full build specification · v1 · 29 October 2026

Supersedes BRAIN_SPEC.md.

---

> ## HOW THIS FILE IS USED IN THIS REPOSITORY
>
> This file is the **design law** for every frame of the web surface. It was
> handed over on 29 October 2026, when the repository already carried phases
> 1–7f of a working product (see `PROGRESS.md`).
>
> **Before creating or implementing any frame, read, in this order:**
> 1. `§0` — rules of engagement.
> 2. `STACK_AUDIT.md` — the plan-vs-codebase reconciliation. A frame is never
>    built on an unresolved row of that table.
> 3. `§2` — the token layer. No value is invented.
> 4. the frame's own section in `§7`.
> 5. `§15` — the frame ledger: what is built, what is pending, which reference
>    image has been supplied.
>
> **Reference images.** The founder supplies a maximum of **5 frame images per
> request**. Ask for the images of the frames about to be implemented — never
> implement a visual frame from memory of the prose alone. `§15` records which
> images have arrived.
>
> **What this file is for, ruled by the founder on 29 October 2026.** The
> product was finished; the **web app design** was what did not satisfy. This
> plan was written with a second agent **that never saw this codebase**.
>
> So **this file supplies the web app design, and only that.** Everything in it
> that is not design — the data model (§8), the backend contract (§9), the MCP
> tool surface (§9.2) and the stack choices in §1.1 — was written without
> knowledge of what already exists, and is a **proposal, not a requirement**.
> The shipped engine, schema, 8 MCP tools, OAuth layer and security headers
> stay. `STACK_AUDIT.md` §9.1 records how each conflict resolves.
>
> **Where the design cannot work on the shipped backend, the change it needs is
> presented to the founder and validated before anything is modified or
> removed.** Nothing is silently replaced. The running list is
> `STACK_AUDIT.md` §9.2.
>
> **Relationship to `CLAUDE.md`.** `CLAUDE.md` remains the specification of the
> engine, the security architecture and the MCP tool surface, and it still
> governs them. This file governs what the person **sees and does**: screens,
> tokens, atoms, molecules, copy and interaction.

---

## 0. HOW TO USE THIS DOCUMENT

You are building **Brain**, a personal memory server with a web interface.

This document is the single source of truth. The screenshots accompanying it show
the main frames; they are reference, this document is law.

Rules of engagement:

1. **Never invent a design value.** Every colour, space, radius, duration and type
   size in this app comes from a token declared in §2. If you need a value that is
   not there, stop and ask.
2. **Build the token layer first**, then atoms, then molecules, then frames. Do not
   start a frame before its atoms exist.
3. **Where this document and a screenshot disagree, this document wins.**
4. Anything marked `OPEN` in §13 must be answered before that part ships.
5. UI copy is **English-native**. Every string goes through the i18n dictionary (§11).

---

## 1. WHAT BRAIN IS

A person gives Brain links, documents and images. Brain reads them, produces a
transcript, a summary, extracted rules and drafted skills, and exposes all of it
to the person's AI tools through a single MCP server.

The promise, in the product's own words:

> Paste a link or drop a file. Brain reads it, keeps what matters, and hands it to
> every AI you work with.

Three source types only: **Link**, **Document**, **Image**. There is no video
upload — a video reaches Brain through a link.

Five content types come out: **Transcript**, **Summary**, **Source**, **Rule**,
**Skill**.

### 1.1 Stack

| Layer | Choice |
|---|---|
| Server | FastAPI + Uvicorn |
| Templating | Jinja2, returning `HTMLResponse` |
| CSS | Tailwind via CDN, **but every value resolves from a CSS custom property** |
| JS | Vanilla. No framework, no npm, no build step |
| MCP | Streamable HTTP, protocol 2026-07-28 |

Hard prohibitions: no `@apply`, no hex literals outside `:root`, no Tailwind
arbitrary values (`p-[13px]`, `bg-[#eee]`), no `transition: all`, no inline styles.

---

## 2. TOKENS

Two layers. Primitives hold values. Semantics hold references. Only
`primitives` may contain a literal.

### 2.1 Primitives

```
/* colour */
--grey-100  #f5f5f5      --ink-900   #343a40
--grey-150  #f0f0f0      --blue-600  #118ab2
--grey-200  #d9d9d9      --blue-200  #a2d2ff
--white     #ffffff
--amber-100 #fbeec9      --amber-700 #7a5500
--green-100 #d9f2e4      --green-700 #14663f
--red-100   #fbe4e1      --red-600   #b3372b    --red-700 #8a2a22

/* space */
--space-1 4    --space-2 8    --space-3 12   --space-4 16
--space-5 20   --space-6 24   --space-8 32   --space-10 40

/* radius */
--radius-sm 6   --radius-md 8   --radius-lg 24   --radius-full 999

/* border */
--border-width 1px

/* type */
--font-sans  "Inter","Segoe UI",system-ui,-apple-system,sans-serif
--font-mono  ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace
--text-xs 11   --text-sm 13    --text-base 14   --text-lg 15
--text-code 13 --text-read 16  --text-lead 19   --text-title 20
--text-stat 22 --text-display 30
--weight-regular 400  --weight-medium 500  --weight-semibold 600
--leading-tight 1.3   --leading-normal 1.5  --leading-read 1.65

/* motion */
--duration-fast 120ms  --duration-base 160ms  --duration-slow 280ms
--ease-standard cubic-bezier(.2,0,.2,1)

/* dimension */
--sidebar-width 240   --content-max 880    --read-max 680
--nav-item-height 34  --card-height 64     --icon-size 16
--slot-size 20        --switch-w 36        --switch-h 21
--meter-h 4
```

### 2.2 Semantics

```
--bg-base        → grey-200     sidebar only
--bg-canvas      → grey-150     everything right of the sidebar
--bg-card        → white        cards sitting on the canvas
--bg-code        → grey-150     code blocks and read-only fields
--bg-hover       → grey-100     hover on nav items
--bg-selected    → white        current page in the nav
--bg-field       → white        inputs
--bg-overlay     → ink-900 @ 32%  modal scrim

--text-primary   → ink-900      titles, values, menu paths
--text-secondary → ink-900 @ 62%  every explanatory sentence
--text-muted     → ink-900 @ 45%  labels, metadata, placeholders ONLY

--border-subtle  → ink-900 @ 9%   dividers, card edges (decorative, exempt from 3:1)
--border-control → ink-900 @ 16%  input and control edges (MUST pass 3:1)

--icon-default   → ink-900
--icon-muted     → ink-900 @ 55%

--accent         → blue-600     primary actions, focus ring, meters
--accent-soft    → blue-200     avatars, secondary fills
--focus-ring     → blue-600

--status-active-bg/-text   → green-100 / green-700   "Connected", "Added", "OK"
--status-pending-bg/-text  → amber-100 / amber-700   "Processing", "Needs you", "Failed"
--danger / --danger-soft   → red-600 / red-100       destructive actions only

--switch-off     → ink-900 @ 22%
--meter-track    → ink-900 @ 10%
```

Opacities use `color-mix(in srgb, var(--ink-900) N%, transparent)`.

### 2.3 Three themes

Light is specified above. Dark and Warm sunset redefine the **semantic layer only**;
primitives never change. Theme is an attribute on `<html>`:

```
<html data-theme="light|dark|sunset">
```

Resolution order, server-side, before first byte:
account preference → `prefers-color-scheme` → light.
If "switch in the evening" is on, `sunset` applies from 18:00 to sunrise, overriding
the stored choice for that window only.

`OPEN-1`: dark and sunset palettes are not yet specified. Do not invent them.

---

## 3. ATOMS

### 3.1 Icon slot — the alignment contract

Every glyph in the sidebar — brand mark, nav icons, avatar — sits inside a
`--slot-size` (20px) box, `display:grid; place-items:center`, SVG 16px inside.
**Never set an icon width directly.** This is what guarantees one vertical axis
from the logo to the profile row.

### 3.2 Nav item

Height 34, padding `0 --space-2`, gap `--space-2`, radius `--radius-md`,
transparent at rest. Hover → `--bg-hover`. Current page → `--bg-selected` +
`--weight-medium`, icon becomes `--icon-default`. Focus-visible → 2px
`--focus-ring`, offset 1. No indentation for children; the icon carries grouping.
Optional trailing count, `--text-xs`, `--text-muted`, tabular numerals.
Alert variant: amber pill background, used only for items needing attention.

### 3.3 Button

`height:36`, `padding:0 --space-4`, `radius --radius-md`, `--text-sm` medium,
**`white-space:nowrap` and `flex:0 0 auto`** — without these a button in a flex
toolbar compresses and its label wraps.
Variants: `primary` (accent fill), `secondary` (inset border), `ghost`
(transparent), `danger` (red inset border, red text), `danger-solid` (red fill).
Size `sm`: height 30, padding `0 --space-3`, `--text-xs`.
In a setting row: `min-width:148px`, full width on mobile.

### 3.4 Icon button

32×32 (30×30 inside the omnibar), radius `--radius-sm` (full in the omnibar),
transparent, `--icon-muted`. Hover → ink @ 7% background, `--icon-default`.

### 3.5 Input

Height 36, padding `0 --space-3`, `--bg-field`, 1px `--border-control`,
radius `--radius-md`. Hover → border ink @ 28%. Focus → border accent + 2px
outline accent @ 30%, offset -1. Readonly → `--bg-canvas` background,
`--text-secondary`.

### 3.6 Select

Same box as input, chevron drawn as an inline SVG background, `appearance:none`.

### 3.7 Switch

36×21, radius full, `--switch-off` → `--accent` when checked, 17px white knob.
`role="switch"` + `aria-checked`. Used in Profile only.

### 3.8 Status pill

`padding:2px --space-2`, radius full, `--text-xs` medium, with a 6px dot of
`currentColor` before the label. Three states, and only three:

| Pill | Tokens | Meaning |
|---|---|---|
| Active / Connected / OK / Added / Ready | green | done and in use |
| Processing / Needs you / Failed / Needs review | amber | Brain is working, or Brain needs you |
| Archived | grey | set aside |

**Red is never a pill.** Red is reserved for destructive buttons.

### 3.9 Meter

4px tall, radius full, `--meter-track` background, `--accent` fill.

### 3.10 Kbd

`padding:2px --space-2`, 1px `--border-subtle`, radius `--radius-sm`,
`--text-xs` medium, `--text-muted`. Label is **`Ctrl K`** (Windows-first).
Never shown inside the control the shortcut focuses.

### 3.11 Divider

1px `--border-subtle`. Horizontal `.divider`, vertical `.v-rule` (18px tall).

### 3.12 Action link

Inline flex, gap `--space-2`, `--text-base`, `--text-secondary`.
Hover → `--text-primary` + underline, `text-underline-offset:3px`.

### 3.13 Code block

`--bg-code`, radius `--radius-sm`, `--font-mono` at `--text-code`,
`overflow-x:auto`, with a Copy button pinned top-right.
A value the user must replace is wrapped in `<mark class="ph">` — amber
background, semibold, `UPPERCASE_WITH_UNDERSCORES`.

---

## 4. MOLECULES

### 4.1 Item card — the workhorse

One component, reused on Transcripts, Summaries, Sources and Archive.

```
[36px type icon] Title                      [pill] [actions]
                 Type · detail · detail
```

- min-height `--card-height` (64), padding `--space-3 --space-4`, gap `--space-3`
- `--bg-card`, radius `--radius-md`, inset 1px `--border-subtle`
- hover → inset 1px `--border-control`
- **`.item-text` must be `display:flex; flex-direction:column`** and the title
  `display:block` — an inline span will not truncate and the title collides with
  the pill
- pill and actions are `flex:0 0 auto`
- the whole row opens the item; buttons inside call `stopPropagation()`
- **actions are invisible at rest**, appear on `:hover` and on `:focus-within`,
  and are always visible on mobile

Per-surface variation is limited to: the icon, the three metadata fragments, and
the pill. Nothing else changes.

### 4.2 Expandable card — Rules, Skills, Archive

A header button plus a detail panel, `data-open="true|false"`, caret rotates -90°
when closed. Collapsed shows one clamped line. Used when the content is short
enough that a page navigation would cost more than it gives.

### 4.3 Omnibar

`[+] [input, flex:1] [mic]`, padding `--space-2 --space-3`, gap `--space-2`,
radius `--radius-lg`, 1px `--border-control`.
Focus-within → accent border + 2px outline accent @ 30%, offset -1.
**No `Ctrl K` hint inside it.**

### 4.4 Work block

The ingestion step list. Six steps, each `pending → running → done`.
Marker: 6px dot → 14px spinner → green check. A finished step reveals its result
on the right of its name; a running step reveals nothing. Collapsible; collapses
itself on completion to `Processed in 2m 14s · 6 steps`.

### 4.5 Message

User: right-aligned, `--bg-card` bubble, radius `--radius-lg`, max 84%.
Brain: left-aligned, no bubble, max 68ch.
Entrance: 6px rise + fade over `--duration-slow`. Nothing else animates.

### 4.6 Setting row

Label and help text left, control right, **always**. Padding `--space-4`,
separated by 1px `--border-subtle`. Stacks vertically below 720px.

### 4.7 Overflow menu

Right-aligned, min-width 180–210, `--bg-card`, inset border + drop shadow.
Items are `--text-sm` with a 16px muted icon. A separator precedes destructive
items. Closes on outside click and on Escape.

### 4.8 Confirm dialog

Scrim `--bg-overlay`, centred panel max 460px, radius `--radius-md`,
padding `--space-5`.
The question names the object: `Delete "Run a cycle planning session"?`
Escape and outside click cancel. Cancel is `ghost`, confirm is `danger-solid`.

### 4.9 Toast

Fixed bottom-centre, `--ink-900` background, white text, radius `--radius-md`.
Carries an Undo action when the action is reversible. Auto-dismiss after 5s.

### 4.10 Guided steps

Numbered `<ol>` with a 26px circular counter. Used only for genuine sequences:
Connections setup, and the "what it does" body of a skill.

---

## 5. TEMPLATE

```
┌──────────┬──────────────────────────────────┐
│ sidebar  │  content                         │
│  240px   │  canvas, scrolls independently   │
│ bg-base  │  bg-canvas                       │
└──────────┴──────────────────────────────────┘
```

Sidebar: padding `--space-3`, `border-right: 1px --border-subtle`, own scroll.

```
Brain                        ← brand, 20px accent square + name
──────────
Home

Library                      ← section label, --text-xs, --text-muted
Transcripts        24
Summaries          24
Sources            31

Knowledge
Rules              47
Skills              2        ← amber pill when items need review
                               otherwise plain count

System
Activity
Connections
Archive            12

(margin-top:auto)
──────────
E  Profile
```

**Counts live in the navigation and nowhere else.** No page header prints a count.

Content: `padding --space-8`, inner `max-width --content-max` (880) centred.
Reading surfaces (Transcript, Summary) use `--read-max` (680) instead.

Routes:

```
/                      Home
/library/transcripts   Transcripts
/transcripts/:id       Transcript
/library/summaries     Summaries
/summaries/:id         Summary
/library/sources       Sources
/sources/:id           Source detail
/rules                 Rules
/skills                Skills
/activity              Activity
/connections           Connections
/archive               Archive
/profile               Profile
/signin                Sign in
```

---

## 6. GLOBAL RULES

These apply to every frame. A violation anywhere is a bug.

1. **Actions sit on the right.** Of a card, of a row, of a dialog. Never left.
2. **One action, one place.** If Rename is in the overflow menu, there is no
   pencil beside it.
3. **Counts in the nav only.**
4. **Vertical rhythm.** `--space-8` between sections, `--space-1` between a
   section title and its note, `--space-4` between the note and its content.
5. **Containers use `--space-4` of horizontal padding** — search fields, cards,
   rows. This is what puts every icon on one axis.
6. **`--text-muted` is for labels and metadata only.** Any full sentence uses
   `--text-secondary` at minimum; menu paths and values use `--text-primary`.
7. **Destructive actions confirm.** Always. See §9.4.
8. **Nothing is deleted by an agent.** Agents archive. Only the person deletes,
   and only from Archive or Profile.
9. **Transitions name their property.** `background-color`, `opacity`,
   `box-shadow`. Never `all`.
10. **Every interactive style is doubled** for testing:
    `.x:hover, .x[data-force~="hover"]`.
11. **An unavailable control says what is missing and offers to fix it**, rather
    than greying out. Clicking Copy with nothing to copy answers
    "Generate your link first" for 2s, then returns to "Copy".
12. **Empty states are invitations.** They say what will appear here and give the
    button that makes it happen. They never say "no items".
13. **A section with zero items hides itself**, title included.
14. **Dates group lists.** `Today`, `Yesterday`, then `October 26`.
15. `prefers-reduced-motion: reduce` collapses every transition and animation to
    0.01ms and stops the typewriter.

---

## 7. FRAME BY FRAME

### 7.1 `/` HOME — empty state

**Purpose.** One job: get the first source in.

**Layout.** Content flex-centred, inner stack `max-width 720`, `margin-top:-6vh`
(true centring sits too low once the sidebar anchors the eye).

```
Good afternoon, Expedit.          --text-display, semibold, -.02em
Paste a link or drop a file…      --text-lg, --text-secondary, max 56ch

[ + ][ omnibar input ][ mic ]

  Teach Brain                     action links, padding-left --space-5
  Add a skill                     so they align with the omnibar text
  Write a rule
```

Single viewport at 1512×982. No scroll.

**Greeting** switches on local hour: <12 morning, <18 afternoon, else evening.

**Cycling placeholder.** Typewriter through three strings in order:
1. `Ask Brain anything…`
2. `Paste a YouTube link and Brain will watch it…`
3. `Drop a document or a screenshot…`

Timing: 50ms per character, 26ms deleting, 2200ms hold, 240ms between phrases.
Stops **permanently** on first `focus` or `input`. Never restarts. Does not start
at all under reduced motion — string 1 shows statically.

**Actions**

| Control | Result |
|---|---|
| `+` | file picker: documents and images |
| input | accepts a pasted link, typed text, or both |
| mic | records, transcribes, submits the text as the instruction |
| drag onto the page | same as `+`; the whole content area is a drop target |
| `Ctrl K` | focuses the omnibar from any page |
| Teach Brain | focuses the omnibar and opens the file picker |
| Add a skill | `/skills` with the create panel open |
| Write a rule | `/rules` with the create panel open |

**Submit** creates a source, starts ingestion, and turns Home into a thread.

---

### 7.2 `/` HOME — thread state

**Purpose.** Ingestion is a conversation, not a progress bar. The person must be
able to interrupt while Brain works.

**Layout.** Greeting and pitch disappear. The thread fills the space and scrolls;
the composer pins to the bottom with a gradient mask above it. Placeholder
becomes `Reply to Brain…`.

**Sequence**

```
[user]   source chip + instruction
[brain]  "On it. I'll watch the whole thing and keep only the planning material."
[brain]  work block, running
         ↳ hint under the composer:
           "Brain is working. You can keep typing, or leave — Activity will
            show when it lands."
[user]   "Actually keep the hiring bits too."      ← mid-flight, not blocked
[brain]  "Noted — I'll leave them in. Still on the summary, so nothing is lost."
[brain]  work block collapses → "Processed in 2m 14s · 6 steps"
[brain]  result table + [Open the source] [Review the skills]
[brain]  two suggestion chips
```

**The result table**

```
Summary      Ready          green pill
3 rules      Added          green pill
2 skills     Need review    amber pill
```

**Hard requirements**

- The composer is **never disabled** while Brain works.
- An interjection is acknowledged without restarting or stopping the pipeline.
- Leaving the page does not cancel anything.
- Reopening Home later returns to the empty state. The thread is reachable from
  the source it produced.

---

### 7.3 `/sources/:id` SOURCE DETAIL

**Purpose.** One source and everything it produced. Two states of the same page:
`processing` and `done`. There is no separate loading screen.

**Layout**

```
← Sources
[132×74 thumb]  How Linear builds software
                Link · youtube.com · 42:18 · added just now
                [Processing] or [In memory]

Your instruction
Pull out how they run planning cycles. Skip the hiring parts.

┌ Working on it / Done in 2m 14s              [6 steps] ┐
│  ● Reading the source          youtube.com/watch?v=…  │
│  ● Transcribing audio          8,412 words captured   │
│  ● Writing the summary         following your…        │
│  ● Extracting rules            3 found                │
│  ● Drafting skills             2 drafted, waiting…    │
│  ● Updating memory             available to every…    │
└───────────────────────────────────────────────────────┘

You can leave this page. Brain keeps working and Activity will show when it lands.

What came out of it                        [Open transcript]
┌ Summary          [copy][download] ┐
┌ Rules            3 added          ┐  each with its timestamp
┌ Skills           2 need review    ┐  each with an Approve button
```

**The six steps are the pipeline.** They are fixed and ordered. A step reveals its
finding only once done.

**Actions**: Open transcript · Copy summary · Download summary · Approve a skill ·
overflow (Rename, Re-process, Archive).

---

### 7.4 `/library/transcripts` TRANSCRIPTS

**Layout**: title, search + sort toolbar, date groups, item cards.

**Card**: icon by content type · title · `Type · duration · word count` · hover
actions `Copy`, `Download`, `⋯`.

**Actions**: row click opens the transcript · Copy · Download · overflow (Rename,
View the source, Generate a summary, Archive).
**No Delete here.** Archive only.

**Empty**: "No transcripts yet. Give Brain a video or a recording and the full text
lands here, searchable and ready to copy." + `Teach Brain`.

---

### 7.5 `/transcripts/:id` TRANSCRIPT

**Purpose.** Reading, searching, and getting the text out.

**Layout**: `--read-max` 680. Back link, title, meta
(`Source · duration · word count · speakers · added`), a derived strip

> **OVERRIDE R-1 (founder, 29 Oct 2026): `speakers` is struck from this meta
> line, and the body renders turns without speaker attribution.** Knowing which
> speaker is talking was judged not important, and nothing in the ingestion
> pipeline records it. See `STACK_AUDIT.md` §9.2 row 4.
(`Summary · 3 rules · 2 skills · Original source`), a **sticky action bar**, then
the body.

**Body**: `grid-template-columns: 64px 1fr`. Timestamps in the gutter, speaker name
only when the speaker changes, text at `--text-read` / `--leading-read`.

**Sticky bar**: in-page search with live `<mark>` highlight and a match count,
`Copy`, and `Download ▾` with `.txt`, `.md`, `.pdf`, `.srt`.
The bar gains its bottom border only once stuck.

**Actions**: a timestamp opens the source at that second · search · copy · download.

---

### 7.6 `/library/summaries` + `/summaries/:id` SUMMARIES

List: identical to Transcripts. Icon = lines. Meta = `Source · source duration ·
read time`.

Detail: `--read-max` 680.
- The instruction that produced it, in a note box.
- A **Brief / Full** segmented control. Brief = the gist plus 5 grounded points.
  Full adds further points and a chapter walkthrough with time ranges. The read
  time in the meta updates with the control — the control must change something real.
- **Every point carries its timestamp**, right-aligned, clickable through to the
  transcript. This is what makes a generated summary trustworthy.
- Copy and Download `.txt` / `.md` / `.pdf`.

---

### 7.7 `/library/sources` SOURCES

Same list, same card. **No grid, no previews.**

**Meta always begins with the type** — `Link`, `Document`, `Image` — then what
matters for that type: a domain and a duration for a link, a format and page count
for a document, dimensions for an image. **Never a platform name as the type.**

**The yield**, in `--status-active-text`, closes the meta line: `3 rules · 2 skills`.
A source with nothing green produced nothing. This is the most useful column on
the page.

**States**: none (in memory) · `Processing` · `Needs you`.

`Needs you` replaces an error state. Real causes: a private or deleted post, a
scanned PDF with no text layer, a rate-limited platform. It carries a **Fix this**
button whose tooltip states the remedy — "Make it public, then retry", "No text
layer — retype or re-scan". Brain did not fail; Brain needs the person.

**Toolbar** carries the `Teach Brain` primary button. No type filter in v1.

---

### 7.8 `/rules` RULES

**Two groups**: `My rules` (written by the person or carried from their own AI
chats) and `From your sources` (extracted by Brain).

**Expandable card.** Collapsed: one clamped line plus the provenance in grey.
Expanded: the full rule with its reasoning, then provenance chips, then
`Edit` / `Archive`.

**Provenance is the point of this page.** An extracted rule shows the source title
and the timestamp as a link. A written rule shows the tool — `Claude Code`,
`ChatGPT`, `Written here` — and the date.

A rule without its reasoning gets deleted by someone who no longer remembers why
it exists. Always store both the short form and the full form.

---

### 7.9 `/skills` MY SKILLS

**Title is `My skills`.** Three sections, each with a subtitle and a plain
explanatory line below it — no dash, no inline count:

```
From your sources
Drafted by Brain. Not in use until you approve them.

Active
Available to every connection.

Inactive
Kept, but your AI cannot reach them.
```

**Card**: icon, name, one-line description, and on the right —
- proposed → a **`Review` button**. Not a pill. A state the person cannot act on
  is a dead end.
- approved → **`Edit`** and **`⋯`**.

**Overflow menu**: Edit · `Activate` or `Deactivate` depending on state ·
`Export .md` · separator · `Delete` (confirms).

**There is no toggle switch on a skill card.** Activation lives in the menu.

**Expanded body**: `When to use it` (the trigger, without which a model never
reaches for the skill) and `What it does` (numbered steps — the only numbered
list in the app outside guided setup), then provenance.

**Review flow**: `Approve` moves the card into Active. `Edit` opens it for
correction first. `Dismiss` removes it. Counts and the sidebar amber pill
recompute on every change; when the queue empties, the pill becomes a plain count
and the section disappears.

---

### 7.10 `/activity` ACTIVITY

**Two sections, because they answer two questions.**

**Usage** — three meter cards: Calls (`1,284 of 5,000`), Sources (`31 of 100`),
Storage (`2.1 GB of 10 GB`). Each with a 4px meter and a context line.

**Calls** — the MCP log. Rows are **40px, one line**, denser than an item card
because a log is scanned, not browsed.

```
14:02  ● Claude Desktop  search_rules   "planning cycle"   180 ms  [OK]
09:05  ● ChatGPT         search_rules   "em dash"          —       [Failed]
```

Columns: time · connection · tool name in `--font-mono` on `--bg-canvas` ·
target · latency · status.

Click expands: `Asked for`, `Returned`, `Payload`, `Connection`.

A failure says what to do: `Token expired. Reconnect ChatGPT to resume.`

**The connection column is the link to §7.11.** Revoking a key is only meaningful
once you can see what it did.

---

### 7.11 `/connections` CONNECTIONS

**One server. Two ways to authenticate. This is verified behaviour, not a choice.**

- Chat apps (Claude, ChatGPT) accept **only OAuth or no auth** in their custom
  connector dialog. There is no header field.
- Editors, CLIs and agent frameworks accept **a bearer token in a header**,
  because they cannot run a browser sign-in.

**The solution that serves both and is simplest for a non-technical person: put
the key in the URL.**

```
https://brain.app/mcp?key=sk-brain-7Qd2xV9mTb4f2a
```

The chat app treats it as an opaque URL. The server reads the key from the query
string. One link, pasted once, nothing else to copy. The server must **also**
accept `Authorization: Bearer <key>` — two entry points, one validation function.

**First run — this is the most important empty state in the app.**

```
┌ [Not connected]                                        ┐
│ Brain is not reachable by anything yet                  │
│ Generate your link, then paste it once into the AI you  │
│ use. That link is the whole setup — it carries your     │
│ key, so there is nothing else to copy.                  │
│ [ Your link appears here        ] [Generate my link]    │
└─────────────────────────────────────────────────────────┘
```

Accent-bordered, first on the page. The `Connected` section and its sentence about
revocation **do not render** while the list is empty — that sentence means nothing
before there is anything to revoke.

**Two tiles, visibly clickable** — a chevron that rotates on open, and a
`Show me how` label.

**An AI app** → three steps, no generation inside them:
1. *Open the connector settings in your AI.*
   `Claude` — Settings → Connectors → **Add custom connector**
   `ChatGPT` — Settings → Apps & Connectors → **Advanced**. If no custom option
   appears, turn on developer mode under Settings → Security and login.
   `Others` — look for **MCP**, **Connectors** or **Custom server**
2. *Paste your link and name it Brain.* Leave every other field alone.
3. *Brain is live.* Ask it "what rules do I have about planning?".

**An agent** → two steps, one thing to copy. Not JSON — an instruction:

```
Add this MCP server to this project.

  name:      brain
  url:       PASTE_YOUR_GENERATED_LINK_HERE
  transport: streamable HTTP

The key is already inside the URL, so it needs no auth
header and no environment variable. Put it wherever this
project configures MCP servers, then list the tools to
confirm it answers.
```

The placeholder is an amber `<mark>`; it fills with the real link once generated.
Clicking **Copy** before generating answers `Generate your link first` in amber
for 2s. The button always reads `Copy`.

Works with: Cursor, Claude Code, Codex, n8n, LangGraph, CrewAI, the OpenAI and
Vercel agent SDKs — every current framework ships an MCP client.

**Connected list**: name · `Signed in` or `Key` · last call · added · status pill ·
`⋯` with Rename, **Roll the key**, Revoke (confirms).
**One credential per connection.** Revoking one leaves the others working.

---

### 7.12 `/archive` ARCHIVE

**Purpose.** Out of the way, still yours. Nothing here is reachable by the AI.

**Order**: by archive date, most recent first. Not creation date — people come
here to undo something they just did.

**Type filter pills** across the top: All, Source, Transcript, Summary, Rule,
Skill. The filter exists here and nowhere else, because this is the only
heterogeneous list in the app.

**Card meta**: `Type` in `--text-secondary` medium · `archived by you` or
**`archived by Brain`** in amber · original date.
The attribution matters: agents archive. When something vanishes from the library
untouched, this page says who.

**Expanding** shows content by type — a rule's full text, a skill's steps, a
source's URL or file, a transcript's first lines, a summary's gist — then
`Restore` (primary), `Edit`, `Delete permanently`.

**Restore** shows a toast with **Undo** for 5s. **Delete permanently** confirms.
This is the only place in the app where content is destroyed.

---

### 7.13 `/profile` PROFILE

Four sections. `--content-max` is 720 here.

**Account** — avatar with Change photo, Name (editable), Email (readonly, with
"Your library is keyed to this address"), Sign out.

**Appearance** — a theme picker where **each option shows itself**: a 58×40 swatch
reproducing that theme's sidebar/canvas split. Then "Switch to warm sunset in the
evening" (switch, 18:00). Then Language: `Follow my browser` / `English` /
`Français`, which writes the account preference that outranks `Accept-Language`.

**What Brain does with new sources** — the ingestion defaults: Write a summary,
Extract rules, Draft skills (three switches), and "Write them in"
(`The language of the source` / `English` / `Français`). Without this section
these choices get asked on every upload.

**Your data** — Export everything (Markdown in a zip), Delete my library, Delete
my account. The last two are `danger` buttons, red-bordered.

**Every button in a setting row is `min-width:148px`** so the column has one left
edge.

**Destructive confirmation is two steps** (§9.4).

---

### 7.14 `/signin` SIGN IN

Single centred card, `max-width ~380`, on the canvas.

```
◆
Sign in to Brain
Your library is keyed to your email. The same address next
time signs you back into the same library; a new one creates it.

Email  [ you@example.com ]
       [ Continue ]
──────────
✓ Lands you in a workspace with three editable demo items.
✓ Connects to Claude, ChatGPT or an agent with one link.
✓ Agents can archive, never permanently delete.
```

The third line is a promise the rest of the app keeps. Do not break it.

**The card must be width-capped and centred.** The known bug: as a flex child with
no `max-width`, the column collapses to ~240px and the email placeholder truncates
mid-word.

---

> **OVERRIDE R-2 (founder, 29 Oct 2026): expert disagreement is not surfaced in
> the web app.** The shipped product detects contradictions between sources and
> stores them in `tension_links`. This plan has no frame for them, and that is
> now deliberate: they surface where they are useful — through the host LLM or
> agent that reads the library over MCP — and not to the person browsing the web
> app. No conflict card, no conflict pill, no conflict accent in any frame.
>
> The feature itself stays: the table, the hybrid-search join, the MCP envelope
> and its Checkpoint-A wording are untouched.

---

## 8. DATA MODEL

```
user          id, email, name, avatar_url, locale, theme, theme_auto_evening,
              created_at
source        id, user_id, kind(link|document|image), url|file_key, title,
              instruction, status(processing|ready|blocked|archived),
              blocked_reason, meta(json), created_at, archived_at, archived_by
transcript    id, source_id, word_count, speaker_count, duration_s,
              segments(json: [{t_ms, speaker, text}]), created_at
summary       id, source_id, gist, points(json: [{text, t_ms}]),
              chapters(json: [{title, from_ms, to_ms, text}]), created_at
rule          id, user_id, source_id?, t_ms?, short, full,
              origin(written|claude-code|chatgpt|extracted),
              status(active|archived), created_at
skill         id, user_id, source_id?, t_ms?, name, description, trigger,
              steps(json), status(proposed|active|inactive|archived), created_at
connection    id, user_id, name, auth(oauth|key), key_hash, key_last4,
              created_at, last_used_at, revoked_at
call_log      id, user_id, connection_id, tool, args_digest, outcome(ok|failed),
              error_code?, latency_ms, payload_bytes, created_at
job           id, source_id, step, state(pending|running|done|failed),
              detail, started_at, finished_at
```

**Deletion is a user-only operation.** Agents and background jobs may set
`archived_at` and never issue a `DELETE`.

---

## 9. BACKEND CONTRACT

### 9.1 Ingestion pipeline — the six steps are the contract

| # | Step | Produces | Failure |
|---|---|---|---|
| 1 | Reading the source | fetched bytes or HTML | `blocked`: private, deleted, rate-limited |
| 2 | Transcribing audio | transcript + segments | `blocked`: no text layer, no captions |
| 3 | Writing the summary | gist, points, chapters | retry once, then `blocked` |
| 4 | Extracting rules | rule rows, each with `t_ms` | may produce zero |
| 5 | Drafting skills | skill rows, `status=proposed` | may produce zero |
| 6 | Updating memory | index entries | retry with backoff |

Each transition writes a `job` row. The UI polls or streams from those rows — the
work block is a view of real state, never a fake animation. A step shows its result
only when `done`.

Steps 3–5 honour the person's ingestion defaults and their per-source instruction.
An instruction supplied mid-flight (§7.2) updates the job record and applies to
every step not yet started.

### 9.2 MCP surface

Streamable HTTP at `/mcp`, protocol `2026-07-28`.

| Tool | Reads |
|---|---|
| `search_rules(query, limit)` | active rules, ranked |
| `search_sources(query, limit)` | sources with their summaries |
| `search_transcripts(query, limit)` | transcript segments with timestamps |
| `get_transcript(id)` | one full transcript |
| `get_summary(id)` | one summary |
| `list_skills()` | **active** skills only |
| `get_skill(id)` | one active skill |
| `add_source(url\|file, instruction)` | starts the pipeline |
| `archive(type, id)` | sets `archived_at` |

**Never exposed**: anything archived, any inactive skill, any proposed skill, and
any destructive operation. There is no `delete` tool. This is enforced server-side,
not by prompt.

Every call writes a `call_log` row with its `connection_id`.

### 9.3 Auth

Two credential paths, one validation function.

```
read key from  ?key=…   or   Authorization: Bearer …
→ hash → look up connection → check not revoked → attach user
```

- Keys are **random 32 bytes**, shown once, stored as a hash. `key_last4` is kept
  for display.
- OAuth 2.1 for chat apps: publish RFC 9728 metadata at
  `/.well-known/oauth-protected-resource` and RFC 8414 at
  `/.well-known/oauth-authorization-server`. Unauthenticated requests return
  `401` with a `WWW-Authenticate` header pointing at the metadata. PKCE required.
  Validate that the token's audience is this server.
- Accept these redirect URIs: `https://claude.ai/api/mcp/auth_callback`,
  `https://chatgpt.com/connector/oauth/{callback_id}`, and loopback
  `http://localhost:*/callback`.
- **Roll the key** issues a new key for one connection and invalidates the old one
  without touching the others.

### 9.4 Destructive actions

| Action | Gate |
|---|---|
| Archive anything | none, reversible |
| Restore | none, Undo toast |
| Revoke a connection | confirm dialog |
| Delete one archived item | confirm dialog |
| Delete the library | step 1 type `DELETE`, step 2 identity check |
| Delete the account | step 1 type `DELETE`, step 2 identity check |

The dialog shows a 2-step indicator and a Back control. The confirm button is
disabled until the field validates.

`OPEN-2` — the identity check. Sign-in is email-based, so there may be no password
to ask for. Three candidates:
(a) a six-digit code emailed at that moment — consistent with the sign-in model,
(b) a password, which means adding one at signup for an action that happens once,
(c) typing `DELETE` alone for the library, and an emailed code only for the account.
**Recommendation: (c).** Friction should be proportional to what is lost.
The current screen implements a password field as a placeholder for whichever
is chosen.

### 9.5 Other endpoints

```
POST /api/sources                create + start ingestion
GET  /api/sources/:id/job        step states, for the work block
POST /api/sources/:id/instruct   mid-flight instruction
POST /api/skills/:id/approve     proposed → active
POST /api/skills/:id/state       active ↔ inactive
POST /api/<type>/:id/archive     sets archived_at
POST /api/<type>/:id/restore     clears archived_at
DELETE /api/<type>/:id           requires the §9.4 gate
POST /api/connections            creates a key, returns it once
POST /api/connections/:id/roll   new key, old invalidated
DELETE /api/connections/:id      revoke
GET  /api/export                 zip of Markdown
```

---

## 10. SECURITY — 2026 BASELINE

1. **Keys**: 32 random bytes, stored hashed (Argon2id or scrypt), displayed once,
   masked everywhere afterwards (`sk-brain-••••••••••••4f2a`), with a Reveal
   control. Never logged. `call_log` stores a token digest, never the token.
2. **Key in URL**: the convenience has a cost — URLs land in history and in copied
   links. Mitigate: never log full query strings, never put the key in a redirect,
   scope the key to MCP reads only, and make Roll the key a one-click action.
3. **Transport**: HTTPS only, HSTS, no mixed content.
4. **Headers**: strict CSP, `X-Content-Type-Options: nosniff`,
   `Referrer-Policy: strict-origin-when-cross-origin`, `frame-ancestors 'none'`.
5. **Sessions**: `HttpOnly`, `Secure`, `SameSite=Lax` cookies. Rotate the session
   id on sign-in. Sign out revokes everywhere.
6. **CSRF**: token on every state-changing form; MCP is exempt because it is
   bearer-authenticated and not cookie-authenticated.
7. **Tenant isolation**: every query filters on `user_id`. Never trust an id from
   the client without that filter. This is the single most likely data-leak bug in
   this app.
8. **Uploads**: validate by content sniffing, not by extension. Cap size. Strip
   EXIF from images. Serve user files from a separate origin, never inline.
9. **SSRF**: ingesting a user-supplied link is a server-side fetch. Block private
   and link-local ranges, block redirects into them, resolve DNS before connecting
   and pin the resolved address, enforce a timeout and a byte cap.
10. **Prompt injection**: content ingested from the web can contain instructions
    aimed at the model. Treat every fetched document as data. Summaries, rules and
    skills derived from a source are **proposals**, never auto-executed. This is
    why skills require approval — it is a security control, not a UX nicety.
11. **Rate limits**: per connection and per user, on MCP calls and on ingestion.
    A limit breach returns a typed error the UI renders as `Needs you`.
12. **Audit**: `call_log` is append-only and visible to the person at `/activity`.
    Transparency is the feature.
13. **Export and delete** satisfy GDPR portability and erasure. Deletion is a real
    deletion, including derived rows and stored files, within a stated window.
14. **Dependencies**: pin every version. Prefer a release at least two weeks old.
15. **Secrets** in environment variables, never in the repo, never in client JS.

---

## 11. I18N

English is the source of truth. Every visible string carries `data-i18n="key"`,
every aria-label `data-i18n-aria="key"`. No hardcoded text in a template.

Resolution, **server-side, before the first byte**:
account preference → `Accept-Language` → English.
Resolving in the browser produces a visible flash of English. `?lang=xx` forces a
locale for testing.

French ships at launch. Translation never changes layout tokens — French runs
roughly 20% longer, so no fixed-width text containers.

---

## 12. RESPONSIVE

| Width | Behaviour |
|---|---|
| ≥ 1200 | as specified |
| 960–1199 | meter grid becomes one column; content max-width still applies |
| 720–959 | sidebar persists; log rows drop the connection and latency columns |
| < 720 | sidebar becomes a top bar with a menu button; content padding drops to `--space-4`; setting rows stack; card actions are permanently visible since there is no hover; the thread composer sits above the keyboard |

Viewport and safe areas:

```html
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
```

`:root` carries `padding-top: env(safe-area-inset-top)` and the bottom equivalent.
A fixed bottom composer adds `env(safe-area-inset-bottom)` to its own padding.

---

## 13. OPEN DECISIONS

| # | Question | Blocking |
|---|---|---|
| ~~OPEN-1~~ | **CLOSED 29 Oct 2026 by two rulings.** Dark is specified and built. **Warm sunset is dropped** — this app has two themes (`R-10`). The palette was derived, not picked: the light theme's opacity ladder inverted over the dark surfaces this repository already shipped and audited in phase 7, then measured against §14.4 before being written down — 19 checks, zero failures | closed |
| OPEN-2 | Identity check for deletion: emailed code, password, or neither | §9.4 |
| OPEN-3 | Does Home keep a thread history in the sidebar, or does each thread resolve into its source? | §7.2 |
| OPEN-4 | Plan limits — the numbers in §7.10 are placeholders | Activity, billing |
| OPEN-5 | Audio retention is stated as 30 days on the transcript page; confirm | legal copy |
| ~~OPEN-6~~ | **RULED 29 Oct 2026.** §2.2's values could not satisfy §14.4. Four tokens raised to the smallest values that clear the bar, two left below it deliberately. See `R-5` | closed |

**`R-5` — the OPEN-6 ruling, as built.**

| Token | §2 said | Measured | Now | Measures | Why |
|---|---|---|---|---|---|
| `--blue-600` (→ `--accent`) | `#118ab2` | 3.96:1 white-on-fill | `#1080a6` | 4.51:1 | A white label on a filled primary button was under the bar. −7% lightness, imperceptible against the captures |
| `--border-control` | ink 16% | 1.33:1 | ink **56%** | 3.18:1 | §2.2 annotated this very token *"MUST pass 3:1"*. An input is a white field on a near-white canvas — 1.07:1 — so the border is the only thing locating the control |
| `--text-secondary` | ink 62% | 3.54:1 | ink **77%** | 4.58:1 worst | It carries *"every explanatory sentence"*, so it is body text |
| `--icon-muted` | ink 55% | 2.75:1 | ink **59%** | 3.01:1 worst | Set by the sidebar, the darkest surface an icon sits on |
| `--text-muted` | ink 45% | 2.36:1 | **unchanged** | 2.36:1 | Deliberate. §2.2 confines it to *"labels, metadata, placeholders ONLY"* and §6 rule 6 forbids a sentence in it. Raising it would flatten the quiet metadata the captures depend on |
| `--switch-off`, `--meter-track` | ink 22% / 10% | 1.49:1 / 1.19:1 | **unchanged** | — | Out of scope, not overlooked: they are fills under a knob and a bar. What carries the state is the knob's position and the accent fill, which clear their own bars |

**One constraint this creates.** Even at `#1080a6` the accent reaches only
3.95:1 as text on the canvas. Every capture uses it as a **fill** or a **ring**
— §2.2's own wording is *"primary actions, focus ring, meters"* — so this is
not a problem today. A frame that wants accent-coloured text must put it on a
card. `tests/test_redesign_tokens.py` asserts the limit so it cannot be
crossed by accident.

**`G-1` — two gaps in §2.2, found while building the atoms of §3.**

| What is missing | Where it bites | How it is handled for now |
|---|---|---|
| A **"text on accent"** semantic | The white label on a filled primary button (§3.3) and the switch knob (§3.7). §2.2 names no token for either | The rule reaches for the `--white` primitive, documented in `atoms.css` and pinned by a test that forbids a fourth such exception |
| **`--red-700`** is declared by §2.1 and assigned by §2.2 to nothing | The hover of `danger-solid` is the only plausible use for a darker red | Used for exactly that, and recorded here rather than left as a mystery token |

**`G-1` is closed.** The dark theme settled it rather than an opinion: its
accent has to be light enough to be read as text on a dark card, which leaves
it too light to carry a white label, so the label there is ink. One token
cannot be both, so `--on-accent` and `--on-danger` now exist and a theme flips
them. `--red-700` keeps its single use as the `danger-solid` hover, and
`--white` keeps one: the switch knob, which §3.7 names white and which is a
puck whose position is the signal, not a label.

---

## 14. ACCEPTANCE

The build is correct when:

1. A token lint finds zero hex literals, zero arbitrary values and zero inline
   styles outside `:root`.
2. Every computed colour, radius, spacing and duration traces to a declared token.
3. Adding a new atom requires touching one registry entry, not a new template.
4. Text contrast ≥ 4.5:1 and control borders ≥ 3:1 in all three themes.
5. Every interactive element has a visible keyboard focus state, and every flow
   completes with the keyboard alone.
6. `prefers-reduced-motion` stops the typewriter and collapses every transition.
7. The thread accepts input while the pipeline runs, and the pipeline survives a
   page close.
8. A connection generated from the Connections page works in Claude, ChatGPT and
   Cursor without further configuration.
9. No MCP tool can reach an archived item, an inactive skill or a proposed skill.
10. A user id swapped in any API call returns 404, never another person's data.
11. Deleting the library removes derived rows and stored files, verified.
12. The app renders correctly at 1512×982 with no scroll on Home, and at 390×844.

---

# APPENDIX — WORKING LEDGER (added by the build, not part of the handed-over plan)

## 15. FRAME LEDGER

Status values: `shipped-as-plan` · `shipped-divergent` · `partial` · `not-built`.
`img` is the reference image: `—` not supplied, `✓` supplied and on file.

| § | Route | Frame | Status | img | Note |
|---|---|---|---|---|---|
| 7.1 | `/` | Home — empty state | **built** | ✓ | Shipped app opens on `Everything` (card list), no greeting/typewriter frame |
| 7.2 | `/` | Home — thread state | not-built | **✓** | Shipped ingestion is a job stepper in a pane, not a conversation thread |
| 7.3 | `/sources/:id` | Source detail | **built** | ✓ ×3 | Inspector pane shows a source's claims; no 6-step work block, no route |
| 7.4 | `/library/transcripts` | Transcripts list | **built** | ✓ | `source_content.full_text` exists; never surfaced as a transcript |
| 7.5 | `/transcripts/:id` | Transcript reader | **built** | ✓ | No timestamp-gutter reader, no in-page search, no export |
| 7.6 | `/library/summaries`, `/summaries/:id` | Summaries | **built** | ✓ ×2 | No `summary` entity in the engine at all |
| 7.7 | `/library/sources` | Sources list | **built** | ✓ | Sources appear inside `Everything`; no type-first meta, no yield column |
| 7.8 | `/rules` | Rules | **built** | ✓ ×2 | `Library` shows epistemic claims — richer than a rule, different card |
| 7.9 | `/skills` | My skills | **built** | ✓ ×3 | `Skills` view exists; no proposed/active/inactive tri-section, no Review flow |
| 7.10 | `/activity` | Activity | partial | **✓ ×2** | `audit_logs` + Profile block 4 show calls; no usage meters, no row expansion |
| 7.11 | `/connections` | Connections | partial | **✓ ×3** | Profile block 2 carries the `/mcp` link + live sessions; no key-in-URL, no tiles |
| 7.12 | `/archive` | Archive | **built** | ✓ ×2 | `Archived` view exists; no type filter pills, no `archived by Brain` attribution |
| 7.13 | `/profile` | Profile | **built** | ✓ ×3 | 6-block modal (Checkpoint B + Phase 6), not a 4-section page |
| 7.14 | `/signin` | Sign in | **built** | — | Email-keyed login exists in `/api/login`; no dedicated frame |

## 18. REFERENCE IMAGES — WHAT EACH CAPTURE ADDS

The prose is law; a capture resolves what the prose leaves open. This section
records what was actually seen, so an implementation months from now does not
re-derive it. Batch 1 arrived 29 October 2026.

### 18.0 The state switcher is not product

Every capture carries a small control bottom-right — `Processing | Done`,
`Empty | Thread | Replay`. It is the founder's capture harness for stepping
through design states. **It is never built into the product.** (This repository
already has the same idea on its `/preview` route, with layout and state
pickers — that is where an equivalent belongs, not in the app shell.)

### 18.1 The sidebar, seen in all five captures

* Brand: a filled accent rounded-square followed by `Brain` in semibold. Both
  inside the §3.1 20px slot, which is what puts the mark on the same vertical
  axis as every nav icon and the avatar below.
* Order confirmed: `Home` · **Library** → Transcripts, Summaries, Sources ·
  **Knowledge** → Rules, Skills · **System** → Activity, Connections, Archive.
  Then `margin-top:auto`, a divider, and the `E` avatar + `Profile`.
* Section labels (`Library`, `Knowledge`, `System`) are muted, small, iconless,
  and indented to the same left edge as the nav labels — not as the icons.
* The current item is a **white rounded rectangle spanning the sidebar's inner
  width**, label and icon both at full strength.
* At rest the label reads close to `--text-primary`, *not* muted — only the
  icon is `--icon-muted`. (The shipped `app.css` mutes the whole row; that is a
  behaviour the redesign changes.)
* Icons seen: house, document, stacked lines, link, framed list, star, pulse,
  plug, archive box, avatar circle. Ten, against the 13 inline symbols the
  current `index.html` carries — the symbol set is replaced, not extended.
* **No counts appear in batch 1**, because the captured library is empty. §5's
  rule that counts live in the nav is untested by these images.

### 18.2 §7.1 Home, empty state

* The pitch line is the §1 promise verbatim — *"Paste a link or drop a file.
  Brain reads it, keeps what matters, and hands it to every AI you work with."*
  — wrapping to two lines, not the shortened `Paste a link or drop a file…`.
* The omnibar is a tall full-radius pill: `+` on the left, placeholder, mic on
  the right. Both icons muted. No `Ctrl K` hint inside it, as §4.3 requires.
* The three action links sit below it with their icons aligned to the omnibar's
  `+`, confirming §7.1's `padding-left --space-5`.
* The greeting rendered as `Good evening, Expedit.` — the hour branch works.

### 18.3 §7.2 Home, thread state

* The source chip is a white card carrying the thumbnail, the title and
  `YouTube · 42:18`, right-aligned above the user's first bubble.
* **The work block collapses here**, to a single row:
  `✓ Processed in 2m 14s · 6 steps` with a chevron on the right, the duration
  and the step count emphasised. This is §4.4's collapse-on-completion.
* Brain's column is visibly narrower than the user's bubbles — §4.5's 68ch
  against 84%.
* The result table is a card of three rows: `Summary / Ready` green,
  `3 rules / Added` green, `2 skills / Need review` amber. Each row carries the
  type icon on the left.
* `Open the source` is primary, `Review the skills` is secondary, side by side.
* The two suggestion chips are full-radius outlined pills, not buttons.
* The composer is the omnibar again, placeholder `Reply to Brain…`, pinned
  below the thread.

### 18.4 §7.3 Source detail — and the contradiction §4.4 hides

The same page was captured in both states, which settles something the prose
left ambiguous.

* **Processing**: header pill amber `● Processing`; the work block titled
  `Working on it`; three steps checked with their results, one step showing a
  **ring spinner**, two still grey dots with muted labels and no result.
* **Done**: header pill green `● In memory`; the block titled `Done in 2m 14s`
  with a grey `● 6 steps` pill on its right; all six checked, each with its
  result.
* **So the block does NOT collapse on this page.** §4.4 says it collapses on
  completion; §7.3's own layout shows it expanded with a `Done in 2m 14s`
  header. The captures confirm the two surfaces differ: **collapse on Home,
  stay expanded on Source detail.** Treat §4.4's collapse rule as scoped to the
  thread.
* A finished step's result sits **immediately to the right of its name**, muted,
  not flushed to the card's right edge. The running step reveals nothing.
* The hint line sits *below* the card, muted:
  *"You can leave this page. Brain keeps working and Activity will show when it
  lands."*
* `What came out of it` is a section heading with `Open transcript` as a
  secondary button on its right.
* The Summary card shows prose with no visible controls at rest — §4.1's
  hover-only actions.
* The Rules card carries a green `● 3 added` pill and one row per rule, the
  rule left and its **timestamp right-aligned** (`14:02`, `21:47`, `33:10`).
* The Skills card carries an amber `● 2 need review` pill and one row per skill,
  each with its own **primary `Approve` button** on the right.

> **Q-1 — RESOLVED by batch 2, no founder decision needed.** The §7.3 meta
> reads `YouTube · 42:18 · added just now` rather than the plan's
> `Link · youtube.com · …`, which looked like it broke §7.7's *"never a platform
> name as the type"*. The Sources capture settles it: on **`/library/sources`
> only**, the meta begins with `Link` / `Image` / `Document`. Everywhere else —
> the source detail, the transcript, the summary, the Transcripts list — it
> begins with the platform or the format (`YouTube`, `Recording`, `PDF`). §7.7's
> rule is scoped to its own page, and the two captures agree.

### 18.5 Batch 2 — Transcripts, Summaries, Sources (29 October 2026)

**The sidebar now carries counts**, which batch 1 could not show: `Transcripts
24`, `Summaries 24`, `Sources 31`, `Rules 47`, `Skills 9`. `Home`, `Activity`,
`Connections` and `Archive` carry none. The counts are right-aligned, muted and
small, as §3.2 specifies. `Skills 9` renders as a **plain count**, so the amber
alert variant is reserved for a queue that actually has something in it.

#### §7.4 Transcripts list

* Title at display size, then one toolbar row: a search field that **carries the
  `Ctrl K` chip inside its right end**, then a `Newest` sort button.
  This does not break §3.10's *"never shown inside the control the shortcut
  focuses"* — `Ctrl K` focuses the omnibar, not this field.
* Date groups `Today` / `Yesterday` / `October 26`, muted, above their cards.
* Each card opens with a **36px rounded tile holding the type glyph** — a video
  frame, a microphone, a document — which §4.1 calls the type icon.
* Meta is platform-or-format first: `YouTube · 42:18 · 8,412 words`,
  `Recording · 27:41 · 5,109 words`, `PDF · 18 pages · 6,870 words`.
* One card carries an amber `● Processing` pill; the rest carry none.
* No actions are visible at rest, confirming §4.1's hover-only rule.

#### §7.5 Transcript reader

* Meta: `YouTube · 42:18 · 8,412 words · 2 speakers · added today`.
* The derived strip is **four outlined chips with icons**, not plain links:
  `Summary`, `3 rules`, `2 skills`, `Original video`.
* The action bar is search (`Search in this transcript`, flex-1) + `Copy` +
  `Download ▾`, all on one row.
* The body is the §7.5 grid: timestamps in the left gutter, the turn's text at
  reading size on the right, and the speaker name above the text **only when the
  speaker changes** — `Host` at 00:00, nothing at 00:42, `Guest` at 01:27,
  `Host` at 01:34. The capture demonstrates the rule exactly.

#### §7.6 Summary detail

* Meta: `YouTube · 42:18 of video · 2 min read · today`.
* The instruction sits in a bordered note box labelled **`Summarized with your
  instruction`** — not §7.3's `Your instruction` — and the instruction itself is
  rendered in `--text-secondary`.
* One row carries the **`Brief | Full` segmented control on the left** (active
  segment is an accent-filled pill with white text, inactive is transparent) and
  **`Copy` + `Download ▾` on the right**.
* The gist is a single paragraph at lead size with generous leading, visually
  larger than the key points.
* `Key points` is a muted label; each point is text on the left with its
  **timestamp right-aligned**, rows divided by hairlines.
* The same four derived chips close the page, below a divider.

#### §7.7 Sources list

* Toolbar: search + `Ctrl K` + `Newest` + the primary **`+ Teach Brain`**
  button, as §7.7 requires.
* **The type leads the meta, and the yield closes it in green**:
  `Link · youtube.com · 42:18 · 3 rules · 2 skills`,
  `Image · 1512 × 982 · 1 rule`,
  `Document · PDF · 18 pages · 5 rules`.
* A source that produced nothing shows nothing green — the two that carry
  `● Processing` and `● Needs you` have no yield at all, which is precisely
  §7.7's *"a source with nothing green produced nothing"*.
* `Needs you` renders as an amber pill like `Processing`; the **`Fix this`**
  button §7.7 describes is not visible at rest, so it belongs with the
  hover actions.

> **R-1 vs the capture — RULED 29 Oct 2026: the override stands, the capture
> does not.** The transcript capture shows `2 speakers` in the meta and
> `Host` / `Guest` above every speaker change. Put to the founder with the cost,
> the answer was to drop speakers entirely. So: **no speaker label anywhere, and
> `2 speakers` leaves the meta line.** The body keeps its grid — timestamps in
> the gutter, text at reading size — as a sequence of timestamped blocks without
> attribution. This is the one place where a capture is knowingly not followed.

### 18.6 Batch 3 — Rules and Skills (29 October 2026)

#### The sidebar's amber pill, finally demonstrated

Two states were captured and together they settle §5 and §7.9:

* On the Skills page, with two drafts waiting: **`Skills 2` in an amber pill**.
  The number is the **review queue**, not the library.
* On the Rules page, with nothing waiting: **`Skills 9` as a plain count** —
  the total.

So the pill does not decorate the count, it **replaces** it: a queue count in
amber while the queue has something in it, the plain total when it empties.
That is §7.9's *"when the queue empties, the pill becomes a plain count"*, and
it is the only nav item that behaves this way.

#### §7.9 My skills

* Toolbar: search + `Ctrl K` + the primary **`+ Add a skill`**.
* Three sections, each a bold heading with a plain muted line beneath it and
  **no count and no dash**, exactly as §7.9 demands: `From your sources` /
  *Drafted by Brain. Not in use until you approve them.* · `Active` /
  *Available to every connection.* · `Inactive` / *Kept, but your AI cannot
  reach them.*
* **A proposed card is outlined in amber.** Not stated anywhere in §7.9 — the
  two cards under `From your sources` carry a warm border the Active cards do
  not. It is how the queue reads as a queue without a pill on every row.
* Proposed → a **`Review` button** on the right, outlined, not a pill.
* Approved → the capture shows a **pencil** and a **`⋯`** icon button, both
  **visible at rest**, on every card at once. Resting visibility does not break
  §4.1, whose hover rule governs the item card; a skill is an expandable card
  (§4.2). The pencil itself is dropped by `R-7` — `⋯` alone ships.
* **Inactive cards are dimmed** — name, description and icon all lighter than
  an Active card. Nothing in §7.9 says so; it is how "your AI cannot reach
  them" reads at a glance.
* No toggle switch on any card, as §7.9 requires.
* Expanded, a proposed card shows: `When to use it` (muted label, trigger at
  reading size), `What it does` (muted label, a **numbered list** — the only
  one in the app outside guided setup), then **provenance chips** —
  `🔗 How Linear builds software · 02:24` and `🕐 Drafted Oct 29`, grey,
  full-radius, each with its icon — then **`Approve`** (primary), **`Edit`**
  (secondary) and **`Dismiss`** (ghost).

#### §7.8 Rules

* Toolbar: search + `Ctrl K` + the primary **`+ Write a rule`**.
* A rule card collapsed: the type icon, the **short form** of the rule in
  near-primary text, the provenance beneath it in muted — `Claude Code · Oct 24`,
  `ChatGPT · Oct 21`, `Written here · Oct 18` for written rules, and
  `How Linear builds software · 14:02 · Oct 29` for extracted ones — with the
  caret on the right, pointing right when closed and down when open (§4.2).
* Expanded: the **full form** at reading size, then provenance chips
  (`🔗 Claude Code`, `🕐 Added Oct 24`), then **`Edit`** (secondary) and
  **`Archive`** (ghost).
* The capture demonstrates §7.8's *"always store both the short form and the
  full form"* precisely: collapsed reads *"Always answer in the language I wrote
  in."*; expanded adds *"If I mix two languages in one message, follow the one I
  used for the actual question, not the one used for quoted material."*
* No hover actions on a rule card — everything lives inside the expansion.

> **Q-2 — RULED 29 Oct 2026: the text wins, `R-6`.** The Rules captures print
> `My rules  12  —` and `From your sources  35  —`, against §5, §6 rule 3 and
> §7.9's *"no dash, no inline count"*. **Rules takes the Skills style**: a bold
> group heading with a plain explanatory line beneath it, no count, no dash. So
> `My rules` / *written by you, or pulled from your own AI chats* and
> `From your sources` / *extracted by Brain, traceable to where they were said*.
> Counts stay in the navigation, with no exception anywhere in the app.

> **Q-3 — RULED 29 Oct 2026: the pencil goes, `R-7`.** The capture puts a
> pencil on the card while §7.9 also lists `Edit` in the overflow menu, which
> §6 rule 2 forbids. **An approved skill card carries `⋯` alone**, and `Edit`
> stays where §7.9 put it, at the top of that menu. One action, one place, with
> no exception — the rule survives intact.

### 18.7 Batch 4 — Connections and Activity (29 October 2026)

#### §7.11 Connections

* **The first-run card is accent-bordered and first on the page**, carrying an
  amber `● Not connected` pill, the heading *"Brain is not reachable by anything
  yet"*, the two-line explanation, a **monospace readonly field** reading
  *"Your link appears here"* and a secondary `Generate my link` button.
* **The `Connected` section does not render at all while the list is empty** —
  §7.11 asked for exactly that, and the capture proves it: the first-run frame
  goes straight from the accent card to `Add a connection`.
* `Add a connection` / *Pick what you are connecting. Brain walks you through
  it.* Then the **two tiles side by side**. A tile shows its icon, its title,
  its one-line audience, and a **`Show me how` label in accent** — which is the
  one place an accent-coloured text label appears, and it sits on a card, where
  the accent clears 4.5:1. The chevron points right when closed and down when
  open, and **the open tile gains an accent border**.
* The agent panel is one numbered step: *Hand this to your coding agent* /
  *Cursor, Claude Code, Codex — paste it as a message. It wires Brain in by
  itself.* Then the §3.13 code block, `Copy` pinned top-right, and
  `PASTE_YOUR_GENERATED_LINK_HERE` in the amber `<mark class="ph">`.
* The AI-app panel is three numbered steps (§4.10). Step 1 is a **two-column
  definition list** — `Claude` / `ChatGPT` / `Others` muted on the left, the
  menu path in primary on the right with the final action in bold, joined by
  `→`.
* **The capture's copy is fuller than §7.11's and does not contradict it**, so
  the capture is the source of truth for these strings. Step 2 adds *"If it asks
  about authentication, keep whatever it picked."*; step 3 adds *"The connection
  shows up at the top of this page from its first call."*
* The connected list: a 36px type tile, the name, then
  `Signed in` or `Key` · `last call …` · `added …`, a status pill, and `⋯`
  visible at rest. `ChatGPT` carries an amber **`Key expired`** pill — a new
  amber label, inside §3.8's three states, not a fourth state.
* Its section line is *"Each one holds its own credential. Revoking one leaves
  the others untouched."*

#### §7.10 Activity

* `Usage` / *What your plan covers this month.* Three meter cards: the label in
  secondary, the figure at stat size in semibold with ` of N` muted beside it,
  the 4px meter, and a muted context line — `Resets November 1`,
  *Archived sources do not count*, *Transcripts, documents and images*.
* `Calls` / *Every time one of your connections reached into Brain. Click a row
  to see what it asked for.* Then search + an `All connections` filter button.
* Rows are **one line and dense**, inside a single card divided by hairlines,
  grouped under `Today`: time · an accent dot · the connection name · **the tool
  name in a grey monospace chip** · the target · the latency · the status pill.
* Expanded, a row reveals a definition list on the canvas, not in a card:
  `Asked for` (monospace), `Returned`, `Payload`, `Connection`.

> **The tool names in this capture are illustrative, and `R-4` already settles
> it.** The rows read `search_rules`, `get_skill`, `search_sources`,
> `get_transcript` — the plan's proposed surface. The shipped surface is the
> eight tools of `CLAUDE.md` §5, so the log will read `search`, `fetch`,
> `get_or_synthesize_skill` and the rest. The frame is unchanged; only the
> strings inside the chips differ.

### 18.8 Batch 5 — Archive, Profile, and the two-step dialog (29 October 2026)

#### §7.12 Archive

* Heading, then the purpose line — the capture's is fuller than §7.12's and
  does not contradict it, so it is the string: *"Out of the way, still yours.
  Nothing here is reachable by your AI until you restore it."*
* A full-width `Search the archive` field with **no `Ctrl K` chip** — the other
  lists carry one, this one does not.
* **Type filter pills** on their own row: `All` accent-filled and active, then
  `Source`, `Transcript`, `Summary`, `Rule`, `Skill` outlined. Full radius.
  This filter exists here and nowhere else.
* Date groups read **`Archived today`** and **`Archived yesterday`**, not
  `Today` / `Yesterday`. §6 rule 14 gives the shape; the prefix says *which*
  date is being grouped on, which matters because §7.12 orders by archive date
  and not by creation date.
* A card: the 36px type tile, the title, then
  `Type · archived by you · added …` — with **`archived by Brain` in amber**,
  which is the attribution §7.12 calls the point of the page.
* Collapsed cards show the caret alone. The expanded one shows, in its header,
  a secondary `↺ Restore` and `⋯`; in its body, the content by type
  (`Original link` and the URL in a readonly field), then `Restore` (primary),
  `Edit` and `Delete permanently`.

> **`R-9` — `Restore` appeared twice on the expanded card, and now appears
> once.** The capture puts a secondary `Restore` in the header *and* a primary
> `Restore` in the body. §6 rule 2 forbids that, and `R-7` already settled the
> identical shape on a skill card by deleting the duplicate.
>
> It is kept **in the header**, not in the body, which is the opposite of what
> §7.12's prose lists. The reason is §7.12's own: *"people come here to undo
> something they just did"* — so the page's whole job must be one click from
> the list, never behind an expand. The body keeps `Edit` and
> `Delete permanently`. One word flips it back.

#### §7.13 Profile

* Four sections, each a heading with a muted line: `Account` / *Who Brain thinks
  you are.* · `Appearance` / *How Brain looks and which language it speaks to
  you in.* · the ingestion defaults · `Your data` / *Everything Brain holds is
  yours to take or to remove.*
* Every row is §4.6: label and help text left, control right, **always**, with
  the control column sharing one left edge — which is what §3.3's
  `min-width: 148px` buys.
* `Account`: an identity row (accent-soft avatar, name, email, `Change photo`),
  then `Name` (editable input), `Email` (**readonly**, greyed), `Sign out`.
* `Appearance`: **three theme swatches** showing themselves, labelled `Light`,
  `Dark`, **`Warm sunset`**, the selected one ringed in accent.
* The ingestion defaults are three switches — `Write a summary`, `Extract
  rules`, `Draft skills` — and a `Write them in` select defaulting to
  *The language of the source*.
* `Your data`: `Export` (secondary), then `Delete library` and `Delete account`
  as **outlined danger** buttons, red border and red text, never filled.

> **`OPEN-1` is all but answered by this capture.** The theme is labelled
> **`Warm sunset`**, which is the name this codebase already persists in
> `users.theme_mode` as `warm_sunset` — the plan's `sunset` was the outlier, so
> **there is no data migration**. The swatches show a near-black dark and a
> dark-brown sunset, which is what the two shipped palettes already are. What
> still needs a decision is narrower than it looked: the shipped palettes were
> built on an indigo accent and this one is teal.

#### §9.4 The two-step destructive dialog

* Scrim over the page, centred white panel, modest padding.
* **A step indicator across the top**: step 1 is a filled **danger** circle with
  white `1`, a rule, then a grey `2`. The current step is red, not accent —
  the dialog is coloured by what it does.
* `Delete your account?` names the object, as §4.8 requires.
* Body: *"Your library, your connections and your keys are destroyed. Export
  first if you want to keep anything. This cannot be undone."*
* `Type DELETE to continue`, with **`DELETE` in danger, semibold**, above the
  field.
* Footer right-aligned: `Cancel` (ghost) and `Continue` (**outlined danger**).
  §4.8 says the confirm is `danger-solid`; at step 1 of 2 it is outlined,
  because step 1 does not delete anything. The filled red belongs to the final
  confirm at step 2, which no capture shows and which is built from that logic.

### 18.9 Captures are complete

All five batches are in. Frames and states that were never captured — the
collapsed work block mid-flight, the overflow menus, the toast, step 2 of the
dialog, the empty states, `/signin` — are built from §3, §4, §6 and the logic
of the frames that were captured, and every such decision is recorded as an
`R-n` here rather than made silently.

## 17. OVERRIDES TO THE PLAN BODY

The plan is law, but the founder outranks it. Every deviation from the text
above is listed here and annotated at the point it applies, so the law file
never disagrees with what is being built.

| id | Section | Override | Date |
|---|---|---|---|
| `R-1` | §7.5 | `speakers` struck from the transcript meta line; the body renders turns without speaker attribution | 29 Oct 2026 |
| `R-2` | §7.8, §7.12, all cards | Expert disagreement (`tension_links`) is not surfaced in the web app at all. It stays on the MCP side, where a host LLM or agent reads it | 29 Oct 2026 |
| `R-3` | §1.1 | Jinja2 and Tailwind-via-CDN are not adopted. The 14 screens are real URLs behind a `pushState` router on the existing static shell; CSS stays hand-written and tokenised | 29 Oct 2026 |
| `R-4` | §9.2 | The MCP tool surface keeps the shipped 8 names. §7.10's call log shows those, not the plan's illustrative `search_rules` | 29 Oct 2026 |
| `R-5` | §2.1, §2.2 | Four token values raised so §14.4 can be met; two left below it deliberately. Table in §13 | 29 Oct 2026 |
| `R-6` | §7.8 | Rules group headings take the Skills style — no inline count, no dash. Counts stay in the nav, app-wide, no exception | 29 Oct 2026 |
| `R-7` | §7.9 | No pencil on an approved skill card. `⋯` alone; `Edit` stays at the top of that menu | 29 Oct 2026 |
| `R-8` | §3.5 | Input hover border follows `R-5`: the plan's rest→hover step was ink 16%→28%, and rest is now 56%, so hover is **68%** — the same +12-point step. Derived, not invented; confirm when convenient | 29 Oct 2026 |
| `R-9` | §7.12 | `Restore` lives in the Archive card header, not in the expanded body, so the page's one job is one click from the list. §6 rule 2, applied as `R-7` was | 29 Oct 2026 |
| `R-10` | §2.3, §7.13 | **No warm sunset.** Two themes, Light and Dark. The Profile picker shows two swatches, and the "switch to warm sunset in the evening" row of §7.13 goes with it | 29 Oct 2026 |
| `R-11` | §2.1, §2.3 | §2.3's *"primitives never change"* cannot be met — §2.1 holds no dark surface. The set **grows** by 13 and no existing value moves. The dark theme redefines semantics only, which is the half of the rule that can be kept | 29 Oct 2026 |
| `R-12` | §2.2, §3.3, §3.7 | `--on-accent` and `--on-danger` added, closing `G-1`. Optional while only light existed, where both are white; the dark theme makes them load bearing, because its lifted accent cannot carry a white label | 29 Oct 2026 |

Everything in `STACK_AUDIT.md` §9.2 is **approved** and is being built.

## 19. WHERE THE BUILD IS

| Layer | §  | State |
|---|---|---|
| 1 — tokens | §2 | **built**, two themes, `tokens.css`, 122 tests |
| 2 — atoms | §3 | **built**, all thirteen, `atoms.css` |
| 3 — molecules | §4 | **built**, ten plus three the frames need, `molecules.css` |
| 4 — template and router | §5, §12 | **built**, fourteen routes, `shell.html` + `frames.js` + `frames.css` |
| 4 — frames | §7 | §7.1 built; the other thirteen hold their route and say so |

The surface is mounted at **`/next`** while the shipped control plane still
owns `/`. It is gated exactly like `/preview`: on in tests, on with
`BRAIN_NEXT_UI=1`, off otherwise. At cutover `shell.html`'s `data-base`
becomes `""` and `/` changes hands.

> **`R-13` — the Home stack is 720, not the ~900 the capture shows.** §7.1
> writes *"inner stack `max-width 720`"* and §0 rule 3 gives it to the
> document. The pitch wraps identically either way, because it is capped at
> 56ch; only the omnibar is narrower. Say the word and it becomes 900.

## 16. WORKING PROTOCOL FOR THIS REPOSITORY

1. **Never build a frame on an unresolved row of `STACK_AUDIT.md`.** The audit
   names, per item, whether the plan supersedes the shipped behaviour or the
   shipped behaviour supersedes the plan. An unresolved row is a question for the
   founder, not a judgement call for the build.
2. **Ask for the reference images of the frames about to be built**, at most five
   at a time, and record their arrival in §15 before writing markup.
3. **Token layer before atoms, atoms before molecules, molecules before frames**
   (§0 rule 2). The existing `app/static/app.css` carries a semantic-only token
   set under different names; the audit holds the migration decision.
4. **Every phase ends in `PROGRESS.md` and a commit**, per `CLAUDE.md` §0 rule 2.
5. **`CLAUDE.md` Rule 1 still governs**: user- and agent-facing touchpoints are
   co-designed, never assumed. This plan supplies most of those answers in prose;
   where it does not, stop and present options.
6. **Design wins on design; the codebase wins on everything else.** A sentence
   in this plan about a table, a tool name, a dependency or a header is a
   proposal from an agent that could not see the code. A sentence about what the
   person sees, reads, clicks or expects is law.
7. **Backend changes are proposed, never taken.** When a frame cannot be built
   honestly on what exists, add the row to `STACK_AUDIT.md` §9.2, present it,
   and wait. Deleting or rewriting shipped behaviour to fit a frame is never the
   build's call — including when the plan's own text appears to authorise it.
