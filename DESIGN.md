# Design

## Source of truth
Status: Active. Date: 2026-10-08. Surfaces: preserved Node dashboard, Python evidence workbench, and the planned unified Python workspace preview. Evidence: existing public/ and backend/ui/ assets, API.md, and docs/plans/prd-researched-applications.md. Existing brand and tokens are retained. Preview implementation is not release qualification.

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

### Bounded application-activity preview

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
- [ ] S2–S5 service envelopes and authoritative blocked reasons/editorial DTO; owner API/storage; freeze before consuming production UI controls.
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
