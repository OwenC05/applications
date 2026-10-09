# API and module contract — Application Copilot

The sections below describe the preserved Node MVP. The additive Python evidence
API uses `/api/evidence`; its workbench is separate. Python workspace data
services use `/api/workspace`, documented below. Research, drafting, durable
jobs and browser capabilities are separate implementation gates.

All JSON API requests except GET /api/status require X-Copilot-Token from status. Mutations require application/json. Bind 127.0.0.1; host/origin checks; no CORS; no provider keys in client responses. Envelope: success JSON data directly; failure {error,message?} with status 400/403/404/409/413/422/502/503 as applicable.

## Store module (src/store.mjs)
Export class BrainStore, constructor({directory}); synchronous methods and serialized atomic writes. ID lookup checks profile ownership for every nested resource. Persist data in directory/state.json; no dependencies/secrets. Return deep-cloned objects. Errors carry status.

Profile: {id,name,sectors:[tech|finance],isDemo:false,cloudConsent:false,writingPreferences:'',revision,brainRevision,createdAt,updatedAt,memories:[],interview:{answers:[],skippedQuestionIds:[],completed:false},applications:[]}.
Memory: {id,category,label,content,status:pending|verified|superseded|rejected,source:{kind,id?,label?},supersedes:null|id,createdAt,confirmedAt:null|string}. Pending cannot silently overwrite verified memory. Only verified changes increment brainRevision. Source and extracted content are untrusted text.
Application: {id,company,role,sector,location,url,companyUrl,jobDescription,questions:[{id,text,maxWords:null|number,maxChars:null|number}],status:draft|reviewed|submitted,inputRevision,createdAt,updatedAt,research:null|object,draft:null|object,history:[]}.
Draft provider shape: {mode:offline|cloud,coverLetter,answers:[{questionId,text,evidenceIds:[],researchIds:[]}],evidenceIds:[],researchIds:[],missingFacts:[],warnings:[],quality?:null|object}. Store adds id,brainRevision,inputRevision,researchId,createdAt,stale:boolean,humanEdited:boolean. Research shape described below; store adds id,inputRevision. Sources and research are public company info, not personal facts.

Optional quality: {version:1,status:assessed|needs_work|stale,checkedAt:ISO,rewriteCount:0|1,summary,questionPlans:[{questionId,kind:factual|motivation|behavioral|technical|commercial|other,approach,evidenceIds:[],researchIds:[],missingFacts:[]}],issues:[{target:coverLetter|questionId,category:truthfulness|specificity|role_fit|question_coverage|clarity|format|voice,severity:must_fix|improve,detail,evidenceIds:[],researchIds:[]}]}. Strict schema/enums, exact current question coverage and known verified/research refs when saving. Assessed requires no unresolved issues, missing facts or limit violations and is an advisory editorial pass only. Legacy/offline absent/null remains valid. Prose/input/brain/research changes stale the assessment; clients cannot supply or edit quality. History retains its immutable assessment snapshot; historical/stale reference IDs are structurally validated because replaced research is not retained.

Methods:
- listProfiles() -> metadata array (no memories/answers/applications).
- createProfile({name,sectors,isDemo?:false}) -> full profile. Only internal server demo creation sets isDemo:true; ordinary create-profile endpoint never accepts synthetic facts or this flag.
- getProfile(profileId) -> full profile.
- updateProfile(profileId,{name?,sectors?,cloudConsent?,writingPreferences?}) -> full profile. Ignore/reject arbitrary fields; changing generation settings makes derived drafts stale.
- deleteProfile(profileId) -> {deleted:true}; erase all app-held profile content.
- addMemory(profileId,{category,label,content,source?,supersedes?}) -> full profile; ALWAYS pending.
- reviewMemory(profileId,memoryId,{action:confirm|reject,content?}) -> full profile. Confirm corrected item explicitly supersedes referenced old item and invalidates drafts.
- deleteMemory(profileId,memoryId) -> full profile. Forget removes active/pending item, its directly linked interview source answer, and clears derived drafts/approved history content referencing it. Document scope; external downloads/cloud retention excluded. Other verified items are not silently edited.
- saveInterviewAnswer(profileId,{questionId,question,section,answer}) -> full profile. Persist answer (id + timestamps) and pending memory proposal only; no auto-verification. Progress derives answered/skipped IDs. Re-answer creates a new pending version, never replaces verified content silently.
- setInterviewProgress(profileId,{skippedQuestionIds?,completed?}) -> full profile.
- createApplication(profileId,{company,role,sector,location?,url?,companyUrl?,jobDescription,questions?}) -> full profile.
- updateApplication(profileId,applicationId,patch) -> full profile; patch only input fields or editable draft {coverLetter?,answers?}. Editing role/JD/questions/domain increments inputRevision and invalidates research/draft. Editing generated prose preserves reference IDs but marks human edits; never promotes embedded facts.
- saveResearch(profileId,applicationId,research,{expectedInputRevision}) -> full profile; reject 409 if input changed during async request; stale prior draft on new research.
- saveDraft(profileId,applicationId,draft,{expectedBrainRevision,expectedInputRevision,expectedResearchId,expectedDraftId,expectedHistoryLength}) -> full profile; reject 409 on concurrent memory/input/research OR draft/history change (null draft ID means no previous draft; history length starts at 0). These REQUIRED guards prevent in-flight generation overwriting a newer human edit, review or submission. Record current input/brain revisions. Every save/edit gives the draft a new id/version and clears current-version review approval.
- recordApplication(profileId,applicationId,{status:reviewed|submitted,eventId,expectedDraftId}) -> full profile. Require expectedDraftId to equal current draft ID (409 if stale UI), and current nonstale draft; submitted requires prior review; dedupe eventId. Capture approved wording/history separately from facts; never feed history prose to AI as factual evidence. On review create PENDING application-answer memory candidates for edited/new answer prose (not verified). Never treat unconfirmed candidates as writing-example facts. Preserve idempotency. Editable-draft PATCH also carries expectedDraftId and rejects mismatch rather than overwriting unseen prose.

History entries are immutable snapshots: eventId, status, timestamp, draftId/version, approved coverLetter/answers, evidenceIds. Submitted must reference the EXACT current reviewed draft snapshot; an edit clears review and cannot inherit old approval. Application input edits invalidate derived data. Only a single app server may own a data directory; second process must fail explicitly, not race writes. Failed persistence must not update live in-memory state; corrupt/malformed storage must fail visibly, never reset to an empty brain. Atomic writes use a private temp file, persist before acknowledgement, and retain prior valid state on failure. Test write failure/concurrent requests/restart.

## Interview module (src/interview.mjs)
Export QUESTION_BANK (minimum 40 substantive items spanning tech/finance/common), getInterviewQuestions(sectors), getInterviewProgress(profile), getNextQuestion(profile). Question {id,section,prompt,hint,sector:common|tech|finance}. 45–60 min target, no enforced timer or completion barrier. All output from user's own profile.

## Provider/context modules
src/context.mjs export buildContext(profile,application): role + researched company brief + relevant active verified evidence + explicit writingPreferences only. NEVER include pending/rejected/superseded facts, raw interview history, other profiles, approved factual prose or secrets.
src/ai.mjs export createAI({apiKey,model,fetchImpl?,clock?,timeoutMs?}) -> {status(),research(application,{consent,checkpoint?}),draft(profile,application,{consent,offline?,checkpoint?}),followup(profile,{question,answer,section},{consent,checkpoint?})}. status -> {configured,model}. Optional checkpoint runs before EACH cloud transmission; its status-bearing errors propagate unchanged. Server supplies a persisted-consent/ownership/version guard. Offline paths make zero requests.
Cloud opt-in/key gates apply BEFORE any network request. research payload has no personal profile. With no key/consent research returns honest {mode:'offline',status:'not_researched',summary,companyFacts:[],requirements:[],hiringPriorities:[],sources:[],unknowns:[],warnings:[],retrievedAt:null}; must not pretend company research occurred. Cloud draft REQUIRES valid current cloud research; no unsourced fallback labelled cloud/tailored. Offline draft uses exact verified evidence and pasted role data, visibly generic/template mode. followup offline uses substantive deterministic deepening question and PENDING proposals, not invented facts.
Research cloud output: {mode:'cloud',status:'researched',summary,companyFacts:[{id,text,sourceUrls:[]}],requirements:[{id,text,sourceUrls:[]}],hiringPriorities:[{id,text,sourceUrls:[],inference:true}],sources:[{url,title}],unknowns:[],warnings:[],retrievedAt:ISO}. Actual tool-returned source URLs + declared official employer/ATS domains; invented citations rejected. Two calls (sourced web search then strict structured normalization) acceptable and preferred if combined schema support not runtime verified. Age >7 days requires refresh; edits invalidate immediately. Only employer source domains and trusted ATS domains relevant to provided role permitted. No arbitrary server URL fetch.
Each researched claim also retains a supportExcerpt tied to the sourced research response; inferred priorities explicitly identify supporting claims. A consulted URL alone is not a proof of support. Require nonempty relevant company AND role coverage before status researched/tailored; otherwise status incomplete with unknowns and block cloud drafting rather than label generic content tailored. Retain warnings that model-supported semantic accuracy still needs human review. URLs require exact hostname/subdomain matching, not substring checks. Research remains bound to application inputRevision and company/role/domain. Root re-reads persisted profile ownership/consent and revisions after EVERY async AI call before saving; revoked consent/deleted profile means discard result and do not store it. No historical approved prose enters provider context.
Cloud draft pipeline: strict per-question/example plan -> draft -> independent critique using ORIGINAL evidence/research. Any issues trigger at most one full rewrite, then a NEW critique of that exact final text (3 calls minimum, 5 maximum). Every stage validates question/reference IDs. Missing facts and final answer/length gaps add deterministic must_fix issues even if the critic reports none. Unresolved gaps persist conservatively rather than being invented away. Factual answers do not need employer praise; employer-specific motivation uses concrete sourced details. All stages succeed before saving; failure preserves the old draft/history. Retrieval includes researched requirements/priorities as well as pasted role/question terms and remains bounded to relevant verified memories.

All schema output/error/refusal/incomplete handling validated; model-produced evidence IDs must exist in supplied verified context. Unknown question IDs rejected. Declared word/character limits validated: do not silently truncate meaning; return warning and block review when too long. Auth/quota errors not retried as transient; each request timeout bounded to at most45 seconds; no personal logs. Error messages never leak apiKey. Before every transmission the server re-reads persisted consent/ownership, application inputs and (for personal generation) brain/writing preferences; draft calls additionally guard research/draft/history versions. Final revalidation and required store save guards remain in place.
src/listings.mjs optional export parseListings(data) -> normalized application-input candidates with provenance; import fixed licensed tech repo only on user action, max 100 entries, UK filtering and actual source schema validation. No Trackr scraping. Finance supports manual entry equally.

## HTTP endpoints (server owned by Main)
GET /api/status -> {token,provider:{configured,model},version}; no secrets.
GET /api/profiles -> metadata[]
POST /api/profiles {name,sectors} -> full profile
GET /api/profiles/:id -> full profile
PATCH /api/profiles/:id {name?,sectors?,cloudConsent?,writingPreferences?} -> full profile
DELETE /api/profiles/:id -> {deleted:true}
GET /api/profiles/:id/interview -> {questions,progress,nextQuestion}
POST /api/profiles/:id/interview/answer {questionId,answer} -> full profile (server derives canonical question/section; no arbitrary question identity)
PATCH /api/profiles/:id/interview {skippedQuestionIds?,completed?} -> full profile
POST /api/profiles/:id/interview/followup {questionId,answer} -> {question,rationale,proposals:[]} (proposal review explicit; root may persist proposals pending).
POST /api/profiles/:id/memories {category,label,content,supersedes?} -> full profile
POST /api/profiles/:id/memories/:memoryId/review {action,content?} -> full profile
DELETE /api/profiles/:id/memories/:memoryId -> full profile
POST /api/profiles/:id/applications {company,role,sector,location?,url?,companyUrl?,jobDescription,questions?} -> full profile
PATCH /api/profiles/:id/applications/:appId -> full profile
POST /api/profiles/:id/applications/:appId/research {} -> full profile
POST /api/profiles/:id/applications/:appId/draft {offline?:boolean} -> full profile
POST /api/profiles/:id/applications/:appId/record {status,eventId,expectedDraftId} -> full profile
POST /api/demo {} -> a NEW separately marked synthetic profile with example tech+finance role entries and verified synthetic facts; never add synthetic memories to an existing real profile. No /profiles/:id/demo endpoint exists.
GET /api/profiles/:id/export -> JSON with all app-held profile records; no secrets; explicit UI download action.
Optional POST /api/profiles/:id/import-listings {} -> {profile,imported,skipped,warnings}; fixed public feed only.

## Frontend flows
Sidebar: Overview / Opportunities / Interview / Brain / Applications / Settings. Profile selector always explicit; blank profiles by default, separate clearly labelled demo. Every mutation uses token; render untrusted text via textContent/escaping. State persists on server, selected profile may be localStorage identifier only. Manual role form accepts both sectors and multi-line real application questions, user-supplied company URL/JD; user confirms official company domain. Research panel shows timestamp, source links and inferred labels. Draft screen shows evidence/gaps/limit counters, edit/copy/export, review before explicit mark submitted. Brain pending inbox confirm/reject/conflict/edit/forget. Onboarding save+continue, skip, resume, adaptive followup opt-in; never force hour. Settings explains plaintext local storage/provider retention/consent/key config and full delete/export scope. No fake cloud success or auto-submit buttons.

## Python workspace contract — frozen S0 interfaces

Authoritative schemas: `backend/copilot/domain/contracts.py`; strict version1,
unknown fields rejected, UTC-aware times, code-point spans, canonical UTF-8 hashes.
Separate metadata/facts/documents/consent/application-input/research/output revisions
prevent display edits from retiring evidence indexes. Schema validation is not
ownership, source eligibility, semantic truth or execution authorization.

The Python workspace uses **`/api/workspace`**, distinct from preserved Node routes
and `/api/evidence`. Responses use snake_case versioned models; S6 adapts the UI
explicitly rather than silently serving Node's full-profile envelope. One Python
SQLite authority and shared owner IDs; no second profile database or dual writes.
Existing evidence routes remain compatible. Host/Origin, loopback and boot-session
checks apply to all workspace routes. Mutations use the existing `X-Evidence-Token`
and JSON content type (except explicitly bounded file imports); a profile selector
is not authentication against another local OS user.

Frozen route groups (data routes implemented in S1; later capabilities remain gated):
- `GET /status`: WorkspaceStatus; boot token, version, configured-key boolean and
  qualified capabilities. Never return the key; default capabilities false.
- `GET /profiles`: versioned ProfileList `{schema_version,profiles}` envelope.
  `POST /profiles`: name and sectors;
  returns ProfileDetail. `GET /profiles/{id}`: ProfileDetail with profile,
  consent, interview progress/answers, proposals, typed values and applications.
- `PATCH /profiles/{id}`: explicit metadata/writing-preference fields only;
  `DELETE /profiles/{id}`: revoked access plus cleanup status. Cloud consent is
  not accepted in a general metadata patch.
- `POST /profiles/{id}/consent`: explicit provider/purposes/granted disclosure;
  server creates confirmation time/revision. No consent inferred from API-key presence.
- `GET /profiles/{id}/interview`: canonical sector-filtered question bank and
  progress. `POST .../interview/answers`: question_id/answer; server supplies the
  canonical question/section and creates a pending proposal. `PATCH .../interview`:
  skipped_question_ids/completed. Unknown questions and coercible booleans rejected.
- `POST /profiles/{id}/proposals`: pending proposed wording/provenance;
  `POST .../proposals/{proposal_id}/review`: explicit confirm/reject plus expected
  facts revision. Corrections create immutable versions and supersede explicitly.
  `DELETE .../proposals/{proposal_id}`: conservatively remove derived content.
- `POST /profiles/{id}/applications`: ApplicationRecord input fields, server IDs,
  revision/time. `PATCH .../applications/{application_id}`: validated input edits
  with expected input revision. Stale concurrent writes return409.
- S2 job routes are described below. Later slices add research/draft/review/form/grant/submission routes against
  frozen models; their exact HTTP shapes are committed before each consuming UI
  slice. A model/grant supplied by a client never establishes stored permission.

Errors reuse the evidence envelope `{error:{code,message}}` with safe messages;
never include request content, provider secrets or tracebacks. Stored owner lookup,
current revisions and transactional guards precede every write and transmission.
JSON input is validated as JSON, not by coercing Python tuples/datetimes manually.

Profile views carry profile-owned metadata/facts/documents/consent revisions;
application-input/research/output counters belong to one application. A job's
captured vector composes those scopes server-side. Draft edits, review events and
submission/history changes increment output: publication compares it in the same
transaction as the worker fence, so a valid worker cannot overwrite newer user
work. Typed-value changes increment metadata and invalidate affected form maps
and grants; they never enter story indexes. Feedback proposals require explicit
manual confirmation for factual reuse and retain their original proposal origin.

Browser SemanticPayload.destination means an actual exact request endpoint, not
host-only permission. Initial completed multi-step payloads require one endpoint;
multi-endpoint forms/uploads remain manual until separately scoped contracts and
adapter qualification support them. S2 reconciles existing nested sparse paths
through canonical manifests, without inventing legacy worker leases/fences.

### Implemented S1 data routes and concurrency

Base path: `/api/workspace`. JSON mutations require `application/json`, the
boot-session `X-Evidence-Token`, and strict versioned input models. A `409` means
reload current revisions; do not automatically replay a stale write. Status
enables profile management, interview, proposals and application management only.
No research/draft/fill/submit readiness is inferred from storing an application.

| Route | Input / result |
| --- | --- |
| `GET /profiles/{id}/interview` | `{schema_version,questions,progress,answers}`; canonical sector-filtered bank. |
| `POST .../interview/answers` | `expected_metadata_revision,question_id,answer`; canonical prompt and a pending proposal, never automatic confirmation. |
| `PATCH .../interview` | `expected_metadata_revision,skipped_question_ids,completed`; resume/skip/finish early. |
| `POST .../consent` | `expected_consent_revision,provider,purposes,granted`; explicit research/drafting/assessment purposes and server timestamp. |
| `GET .../facts` | Versioned canonical facts plus their confirmation metadata; same authority as `/api/evidence`. |
| `POST .../proposals` | `expected_metadata_revision,text,source_spans?,supersedes_fact_id?`; pending only. |
| `POST .../proposals/{proposal_id}/review` | `expected_metadata_revision,expected_facts_revision,action,confirmed?,text?`; confirmation requires affirmative `confirmed:true`. |
| `DELETE .../proposals/{proposal_id}` | Expected metadata and facts revisions; forget linked raw origins and confirmed derivatives, returning cleanup status. |
| `POST .../typed-values` | Expected metadata revision, kind/field/value/purpose, optional jurisdiction/application, and `explicitly_confirmed:true`; excluded from story retrieval. |
| `DELETE .../typed-values/{record_id}` | Expected metadata revision. |
| `POST .../applications` | Expected metadata revision plus company/role/sector/vacancy URL and optional location/company URL/JD/questions/official domains. |
| `GET .../applications/{application_id}` | Stored application input and input/output revisions. |
| `PATCH .../applications/{application_id}` | Expected input **and output** revisions plus actual input edits. |
| `DELETE .../applications/{application_id}` | Expected input and output revisions; revoke dependent feedback facts rather than orphan their provenance. |
| `GET .../applications/{application_id}/history` | Immutable feedback/user-reported outcome snapshots. |
| `POST .../applications/{application_id}/history` | Expected input/output revisions, event UUID, text and optional `feedback`/`user_reported_submitted` kind; creates a pending memory proposal. Not an observed employer receipt. |
| `GET /profiles/{id}/export` | Explicit private download of owned domain/evidence records and archives; no credentials. |

Optional `schema_version` is exactly integer `1`. Application questions retain
their declared word/character limits. Typed values and imported/generated wording
do not acquire factual authority merely by being stored, reviewed or exported.

### Explicit selected-byte legacy import

`POST /import/legacy/dry-run` and `POST /import/legacy/commit` accept a bounded
multipart `file` containing **one selected Node profile export**, at most 1 MiB.
Commit additionally requires `X-Confirm-Legacy-Import: true` and
`X-Legacy-Source-Sha256` equal to the dry-run byte hash. No directory discovery,
automatic `.data` read, cross-profile merge, overwrite or inferred URL is allowed.
Collisions, repeat imports, malformed references and changed bytes fail before an
atomic commit. The original bytes remain in a private immutable archive until
forgetting affected content requires removal of that archive.

Legacy verification/provenance and valid interview/application source edges are
retained honestly. Pending corrections preserve their supersession relation but
cannot replace old facts before explicit confirmation. Cloud consent is reset;
old research, drafts and reviewed/submitted history are archives, not current
research/support approvals or receipt-confirmed submissions. Applications without
a valid vacancy URL remain archived. Node storage is never changed by import.

Deletion preserves unrelated question history, canonical facts and corpus
indexes. External cleanup targets captured generation identities, not an owner's
whole index directory. Cleanup may remain pending during Chroma failure; logical
revocation is not a promise of forensic erasure or deletion of external backups.

### Isolated Python data-workspace preview

`GET /ui/workspace/index.html` serves the additive S6 data preview, not a replacement
for `/` or the Node frontend. It uses the versioned workspace/evidence routes
above with the boot token, same-origin requests and explicit source selection.
No research/draft/job/browser controls or qualified capability are inferred.

Owner/view epochs and per-pane request sequences discard superseded read/error
responses. A successful non-destructive save acknowledges only its unchanged
submitted form buffer; newer edits and unrelated forms survive. Explicitly
confirmed scope deletion discards affected browser edits. Read-only export/import dry runs
never acknowledge edits. A `409` remains visible without automatic retry. Changing
interview question invalidates old UI callbacks, not a write already accepted by
the server; subsequent reads reconcile canonical state.

Question limits count Unicode code points, not UTF-16 units; input is validated,
not silently truncated. The preview preserves existing question IDs, type,
optionality and constraints, and refuses structural/text/order changes until an
ID-aware editor is implemented. Citation integrity is displayed separately from
semantic support. Source deletion distinguishes `pending`, `complete` and
unrecognized responses; pending means access revoked, not external cleanup done.

### Durable indexing, status and worker outcomes

These workspace routes use the same boot-session token, strict JSON validation
and profile ownership checks as the data routes. They do not initialize models
or Chroma merely to enqueue, read or cancel a job.

| Route | Input / result |
| --- | --- |
| `POST /profiles/{id}/indexes` | `{schema_version:1,corpus:"facts"\|"documents",idempotency_key}`; `202` with `{schema_version:1,job}`. Repeating the same owned request returns its durable job. |
| `GET /profiles/{id}/jobs` | `{schema_version:1,jobs:[...]}`; content-free summaries, not parameters, stage text or idempotency keys. |
| `GET /profiles/{id}/jobs/{job_id}` | Same summary; another owner's job returns `404`. |
| `POST .../jobs/{job_id}/cancel` | `{schema_version:1}`; cancellation request and current durable state. Already transmitted work is not recalled. |
| `GET /profiles/{id}/usage` | `{schema_version:1,usage}`; reserved/reported/indeterminate accounting, `monetary_cost:null` and unknown pricing, not an invented zero price. |

Summaries expose job ID/owner/application/kind/state, bounded stage, captured
revisions, fence/attempt count, cancellation flag, heartbeat/lease and generation
cleanup-pending flag. Only the `index` handler is available. Workspace capability
flags do not imply researched-drafting or browser readiness.

`GET /api/evidence/status` reports `model_readiness:"not_checked_in_request"`;
legacy `models_ready:false` must not be interpreted as a failed cache check.
Model validation occurs during actual indexing/search. API startup and logical
deletion do not wait for model loading or external cleanup. Evidence deletion
returns `202` with pending state; workspace deletion uses its versioned
`DeleteResult` cleanup flag. The worker reconciles registered artifacts later.

The worker renews job leases during outside-transaction work. Expected authority
loss and infrastructure heartbeat failure are distinguished. Daemon outcomes are
bounded JSON on stderr; `worker --once` stdout is
`{schema_version:1,job,heartbeat,recovery,claim?}` with a nonzero exit on a failed
job/claim/heartbeat/recovery or pending recovery. No raw exception, uploaded text,
provider key or private idempotency value belongs in those diagnostics.

Upload staging records exclusive-open file acquisition separately from a reserved
identity. Cleanup requires the recorded device/inode identity and preserves an
unknown colliding/replacement file. A crash before the acquisition receipt can
remain pending even if the file is currently absent. Terminal, absent uploads
do not cause repeated cleanup writes. Parsing stays bounded synchronous work
outside broad API/storage locks; this is not a parser-job capability.

Generation recovery retains ambiguous dispatched dense writes. Neither local
producer-lock release nor current Chroma absence can clear their tickets without
the recorded completed write envelope. Cleanup is exact-generation and anchored-
path scoped; unknown artifacts are preserved. Worker/upload integration does not
turn logical deletion into a qualified forensic deletion guarantee.

### Standalone static research broker — no HTTP route yet

`copilot.research.broker.research` accepts validated, server-captured non-personal
`ResearchInput` fields and a mandatory current-authority callback. It anonymously
acquires only explicit company/vacancy/supporting URLs on exact confirmed hosts.
Original bytes, canonical text/hashes and Unicode identity spans are returned as
capsules; they are not automatically stored, indexed or treated as instructions.
Private facts, typed values, cookies and user-pasted JD are not input channels.

The initial adapter requires unique exact `Company:`, `Employer:`, `Role:`,
optional `Vacancy ID:` and `Applications: open|closed` lines. Coverage is not
semantic entailment of employer claims. Redirected or unsupported identity,
access restrictions and conflicting role availability remain incomplete. The
oldest acquired source determines a maximum seven-day freshness window.

Six selected sources, ten requests, 120 seconds and an 8 MiB cumulative bounded
body-read allowance constrain a run. Read allowance is reserved before I/O and
refunded only for successfully returned unused bytes, so failed/partial reads
cannot bypass it. This conservative charge excludes TLS, headers, framing and
socket-buffer traffic; it is not a whole-network byte measurement.

Authority is checked before/after acquisitions and before return. In-flight
anonymous GETs cannot be recalled; accepting/publishing results still requires
the caller's stored current job/fence/revision checks. No research job handler,
research HTTP endpoint, discovery agent, renderer or persistence integration is
enabled by this component.
