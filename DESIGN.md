# Design

## Source of truth
Status: Active. Date: 2026-10-11. Surfaces: preserved Node dashboard, Python evidence workbench, and the unified Python workspace preview. Evidence: existing public/ and backend/ui/ assets, API.md, docs/plans/prd-researched-applications.md, and reviewed local drafting UI contract. Existing brand and tokens are retained. Preview implementation is not release qualification.

## Brand
Application Copilot. Calm, capable, evidence-led; appropriate for both engineering and finance. Trust signals: explicit profile, verified/pending badges, source links, timestamps, real offline/error states. Avoid magic-brain claims, hiring-manager mind-reading, gamified pressure and mass-apply imagery.

## Product goals
Help a person turn real experience and real employer research into strong reviewable applications. Non-goals: forced hour-long gate, hosted accounts, unattended submission, unverified personal claims. Milestone A unifies onboarding/evidence/research/grounded drafting; later B permits separately consented supported-form filling, and C permits one exact approved final attempt. Those later controls stay unavailable until their independent gates pass.

## Personas and jobs
Primary: UK student applying in technology/finance. Friends use their own local installation/profile. Need fast job shortlist, deep evidence discovery, career-story reuse, employer-specific reasoning, privacy/control and reliable status tracking.

## Information architecture
Persistent sidebar: Overview, Opportunities, Interview, Brain, Applications, Settings. Main header: current profile, sector filter/context and provider/offline badge. Overview shows next action and truthful counters. Opportunities includes manual role entry with real questions and research-ready company URL/JD. Brain has verified evidence and pending inbox. Application detail combines employer research, evidence match, draft answers and review/history. Settings holds consent, provider configuration instructions, export/delete scope.

## Design principles
Evidence before persuasion. Progressive disclosure over huge forms. Hour-long target, not coercion: save/resume/skip visible. Per-profile ownership never ambiguous. Company research and personal brain are different knowledge domains. Loading/error/empty states remain honest. Historical prose is inspectable, never automatically factual evidence.

## Visual language
Warm off-white canvas (#f6f7f5), white panels, deep navy sidebar (#14212b), teal accent (#127b70), muted graphite text (#28343d), amber pending, red error. CSS variable tokens own palette/spacing. System sans-serif stack; 14–16px body and 30–36px principal title. Eight-pixel spacing rhythm, 12px panel radii, subtle borders/shadows. Text-first cards and lightweight inline SVG icons; no raster assets/external fonts. Motion minimal 120–180ms and optional.

## Components
Sidebar/navigation with active state; profile select; provider/sector/status badges; statistic cards; next-action card; labelled inputs/textareas; primary/secondary/danger buttons; modal or inline forms; memory card with provenance/actions; source list; claim/inference tags; question progress and answer composer; application cards/detail; warning banners; copy feedback. Python integration adds question packets, exact-text support ledgers, sourced rubrics, jobs/reservations, migration dry-run and cleanup states. Each handles disabled/busy/error/empty states. One visual contract; a small tested token duplication between independently runnable Node/Python surfaces is preferable to a styling rewrite.

## Accessibility
Target WCAG 2.2 AA behavior; semantic headings/landmarks, actual buttons/labels, keyboard navigation, visible focus, status aria-live, adequate contrast, minimum 44px touch controls where practical. Never use color alone for verification. Focus moves appropriately after dialogs/actions; errors linked to affected fields. Respect prefers-reduced-motion.

## Responsive behavior
Desktop sidebar ~230px, main max-width ~1250px; 2–3 card columns. Under 900px use compact top navigation and 1–2 columns; under 600px stack forms/cards/detail, maintain readable 16px inputs and no horizontal scroll. Touch does not depend on hover.

## Interaction states
Initial empty profile prompts create profile or clearly synthetic demo. No provider is an offline state with setup instructions, not fake cloud success. Research button reports source retrieval/loading/error and missing official URL. Draft blocked/error if cloud research incomplete; offline templates explicitly labelled. Pending memory needs confirmation; stale draft warns and blocks review. Record reviewed and mark submitted are distinct actions. Every save persists server-side and shows failure if persistence fails.

## Content voice
Plain, encouraging and specific. Prefer Confirmed by you, Pending confirmation, Company research, Inferred priority, Integrity valid, Review-ready and Assessment stale. Distinguish receipt-confirmed from user-reported submission and unknown outcomes. Say 'What the employer publicly emphasizes', not 'What the hiring manager secretly wants'. Describe gaps as questions, never confident invented answers. Explain raw local plaintext and cloud retention accurately.

## Editorial quality
Application drafts display an advisory assessment, actionable issues and expandable per-question example/angle plans. No numeric quality/hiring score. Distinguish Editorially assessed, Needs work, and Stale assessment from human Reviewed. Absent/offline assessments are explicitly unassessed. Unsaved prose edits immediately stale the visible assessment; reverting to the exact saved wording restores it. Expired/mismatched company research suppresses a fresh quality badge. Explain 3–5 cloud calls, latency and usage before generation. Final rewritten prose is critiqued anew; unsupported experience remains a question for the applicant.

Python assessment additionally shows identified exact claim spans, their separate
personal/employer/inference kind, citation integrity and fallible semantic status.
Unsupported, contradicted, unassessed or needs-confirmation claims block review;
blocked wording remains editable and exportable with warnings. Only authoritative
server readiness enables review; neither relevance scores nor a client hash is
permission. Save/reassess is explicit and revision guarded. An assessment is not
a claim that automated factual inventory is exhaustive.

## Implementation constraints
Preserve dependency-free Node24 and its public/ assets. Python/FastAPI uses the
same vanilla HTML/CSS/browser ES-module approach and one SQLite authority. Its
new workspace lives under backend/ui/workspace/ at a preview path; the current
evidence page remains the default until parity and S1–S5 integration pass.
No frontend framework, external font or generated asset is needed. Local API
contract: API.md. Python source/model text uses DOM textContent/text nodes, never
innerHTML or a markdown interpreter; CSP remains self-hosted. LocalStorage may
hold a selected profile ID only, never brain/secrets. Keys stay server-side.

Each asynchronous pane captures profile/application/context epoch **and** its
own request sequence: discard stale successes and errors, including same-profile
selection races. Stop old polling and clear prior-owner content immediately.
Polling must preserve edit buffers, caret and focus; aborting fetch is not job
cancellation. A 409 retains local edits for reconciliation. Unicode spans and
character limits use code points, not UTF-16 indexes. Keyboard flows, hostile
synthetic fixtures and 375/768/1280px screenshots are implementation gates,
not evidence already supplied by this design document.

### Saved writing preview — S6-A

Connect saved packet/question inspection, immutable local draft edits and saved-text
export after the independent application activity pane. This increment does not
launch paid generation/reassessment, approve human review, resolve manual fields,
fill forms or submit. Full S6 and release gates remain separate.

Create labelled selectors and explicit refresh controls once; preserve chosen IDs.
Packet, draft, activity and history request slots retire independently. A save
invalidates earlier detail/export reads, and accepted baseline adoption advances
an editor generation even when the selected draft ID would otherwise be unchanged.
Same-ID refresh must not replace dirty, saving, uncertain or conflicted writing.
Only authoritative frozen writing_targets and publication hashes bind local edit
requests. Empty configured cover letters remain targets; untouched Unicode and
CRLF bytes are retained until actual input on that target.

Show saved assessment, exact source IDs/excerpts and code-point ledger spans in
separate previews; unsaved text never inherits old support highlights. Warning
limits do not prevent local saving. Saved integrity/currentness is not semantic
certainty, review approval or browser authority. Historical/blocked prose stays
inspectable and exportable, never silently promoted to confirmed memory.

Saving captures one immutable key/body and the exact form edit generation. Newer
typing remains dirty and cannot be silently reparented. Only correlated scope,
parent/request/target and output-only revision responses acknowledge that exact
generation. Post-commit errors, including 409/5xx and malformed/lost responses,
retain the operation: explicit reconciliation replays only the same key/body,
never retries automatically or claims rollback. Downloads contain only the
selected saved revision, use a fixed filename and are outside managed forgetting.
While saving or unresolved, ordinary internal navigation/profile/application
switching and explicit data reload are blocked; exact reconciliation remains in
place. Explicit privacy deletion remains available and only a confirmed successful
scope revocation may retire the operation. Failed deletion retains it. The
captured operation is in memory only: beforeunload is a warning, not a hard-close
guarantee. A hard reload/close loses it and requires explicit server-history and
current-revision inspection, never automatic replay or an assumed rollback.
A permanent stale-parent/lineage conflict must not trap the whole workspace:
after a request settles uncertain, explicit read-only handoff retains defensive
copies of the exact operation and effective unsaved target strings in a root
owner/application-scoped memory map. Application inputs/history and new writing
saves remain locked for the page session, including after remount and successful
exact replay. Inspection/export/navigation/privacy deletion remain available;
same-operation reconciliation is available on return with no inherited form
acknowledgement ticket. The lock is never cleared by list absence or another 409.
Successful application/profile deletion or source/proposal forgetting
conservatively purges that owner's capsules (linked feedback facts can inform
other applications). Failed deletion
retains them. No localStorage, log or automatic download receives captured prose.
Privacy success cleanup captures its owner before dispatch and runs independently
of stale UI completion tickets. It also retires/clears active same-owner writing;
another owner's visible pane and dirty forms are untouched. Read-only saved
inspection/export selection remains separate from the retained recovery baseline.
Retired/changed selections cannot trigger a download. Root applies accepted
publication callbacks monotonically to the same application's output counter
only; unrelated forms and input/profile revisions remain untouched.

Actual browser checks on a synthetic local API/SQLite/Chroma fixture exercised
keyboard selection/save, in-flight newer typing, focus/caret preservation,
literal hostile-text rendering, exact saved-byte downloads and read-only
post-commit conflict recovery. Screenshots at 375/768/1280px showed no horizontal
overflow; a bounded axe scan reported no violations. This is not WCAG certification,
cross-browser coverage, full S6 or release qualification. Controller/source unit
tests remain separate evidence, not a substitute for those browser checks.

### Read-only selected-data disclosures — S6-B1

After saved writing, offer explicit generation and saved-edit assessment disclosure
inspection with stable labelled model/output-ceiling controls. No acknowledgement,
queue, budget configuration, key input or paid launch is enabled by this increment.
Use the server's fresh seven-dimension vector for generation; assessment remains
bound to a current unassessed local edit's exact saved bytes/vector/three hashes.
Never reconstruct authority from profile/packet counters or silently rebase text.
Dirty, saving, unresolved and quarantined writing blocks inspection; explain why.

Render selected questions, confirmed personal excerpts, employer excerpts,
criteria/inferences, gaps and exact edited text as literal text in expandable
sections. Show server hashes/versions without claiming browser verification.
Say selected-data disclosure, not exact future outgoing request: later generation
stages may transmit intermediate generated text. Explain 3–5 calls/at most one
rewrite versus one critique/no rewrite, conservative input-byte/output-ceiling
reservation units, unknown money/not zero and provider retention. Show recorded
provider and applicable OpenAI purpose separately; another provider's grant is
not OpenAI permission. Missing permission does not prevent read-only inspection.
Preview success does not certify keys, budget admission or transmission readiness.

Selection, effective typing, save, configuration or domain-input changes invalidate
both displayed disclosure and pending responses. A defensive read-only saved-writing
capture observer cannot interrupt editing or privacy cleanup. Retirement clears
private disclosure content; no domain form is acknowledged by inspection. Optional
assessment permission should be explicitly granted before generation if wanted;
adding it later stales existing writing. Paid launch/recovery and its browser gates
remain the separate S6-B2 increment, not implied by these controls.

Bounded actual-browser B1 checks exercised both disclosure kinds against a real
synthetic local API/SQLite/Chroma fixture: exact saved Unicode/CRLF, no durable
job/attempt/budget changes, pending-GET invalidation before POST, stable writing
focus/caret and private detached DOM clearing. Tested 375/768/1280px widths had
no horizontal overflow; a scoped axe scan found no violations. This is not full
accessibility conformance or paid-provider/release evidence. The backend fixture
predates the narrow strict-counter repair, which has separate fresh HTTP tests.

### Bounded application-activity preview (earlier increment)

The published job/status/cancel/usage and static-research inspection contracts
may be connected before drafting. Add separate read-only activity containers
inside application detail; refreshing them must not reload the profile, replace
input/history forms or acknowledge edits. Prefer explicit refresh controls for
this bounded increment over background polling. Retire independent jobs, usage,
run and source request slots on context or selection changes; suppress stale
successes and errors. Preserve keyboard focus and active edit buffers.

Show durable cancellation requests separately from terminal cancellation, and
retain warnings about already transmitted work. Profile-wide reserved/reported/
unknown usage is not a per-application monetary price. Static source history
separates current-head eligibility from a historical complete state, displays
provenance/hashes/freshness/gaps, and renders bounded canonical text safely.
Neither a stored hash nor an eligible research run establishes semantic support.
No research-launch, paid retry, drafting, review or browser action is introduced
by this inspection increment. Unqualified capabilities stay unavailable.

## Open questions
- [ ] Later browser autofill platform coverage; owner product; out of current milestone.
- [ ] Later authenticated hosted product; owner product; no impact on local-first implementation.
- [ ] Real user's provider credentials/model access; owner user at local setup; no effect on offline/test implementation, live verification remains conditional.
- [ ] Paid drafting/reassessment UI and exact human-review envelopes; owner API/storage; separate next increments, not authorized by local edit/export UI.
- [ ] Docker, quality and independent friend/companion install evidence; owner delivery/verifier; preview must not imply qualified release.

## Python workspace integration — S0 contract boundary

The Node MVP and additive evidence workbench remain separate working experiences.
The reviewed next product has one Python/SQLite authority and `/api/workspace`
snake_case envelopes; it does not silently merge profile IDs or auto-import legacy
private data. Preserve Node until explicit migration and parity tests pass.

S0 adds strict domain contracts and acceptance tests only. It does not add paid
research, generated answers, semantic certification or browser actions. Future S6
reuses this visual system while combining onboarding, evidence, applications,
research and exact review; unsupported/unfinished capabilities remain clearly
labelled, not decorated as successful automation. Profile selection remains a
local convenience, not hosted account security. See the reviewed plans in
`docs/plans/` for release gates and separate fill versus final-submit approvals.
