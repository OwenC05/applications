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
- Job and static-research routes are described below. Later slices add draft/review/form/grant/submission routes against
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
Application activity connects existing job/status/cancel/usage and static research
history/run/source GET routes with explicit refresh, not background polling.
Activity updates do not reload profile data, replace input/history forms or
acknowledge edits. Current-head eligibility and historical completeness remain
separate; canonical source text is bounded and stored hashes do not establish
semantic support. There are no research-launch, paid-retry, draft, review or
browser controls, and no full workflow qualification is inferred.

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
| `POST /profiles/{id}/applications/{application_id}/research/{run_id}/indexes` | `{schema_version:1,idempotency_key,expected_input_revision,expected_research_revision}`; `202` with the exact scoped employer index job. Expected research is the published counter, not the run's pre-publication capture. |
| `POST /profiles/{id}/applications/{application_id}/research/{run_id}/packets` | Strict packet request described below; `202` with the exact unpaid local packet job. Both current corpus indexes must already exist. |
| `GET /profiles/{id}/applications/{application_id}/packets` | `{schema_version:1,batches:[...]}`; immutable stored batches with dynamic canonical-currentness reports. |
| `GET .../applications/{application_id}/packets/{batch_id}` | Batch, publication/report digests, currentness and safe reason codes; no retrieval/model/provider execution. |
| `GET .../packets/{batch_id}/questions/{question_id}` | Exact stored packet or explicit manual requirement, with currentness and support limitations. |
| `GET /profiles/{id}/jobs` | `{schema_version:1,jobs:[...]}`; content-free summaries, not parameters, stage text or idempotency keys. |
| `GET /profiles/{id}/jobs/{job_id}` | Same summary; another owner's job returns `404`. |
| `POST .../jobs/{job_id}/cancel` | `{schema_version:1}`; cancellation request and current durable state. Already transmitted work is not recalled. |
| `GET /profiles/{id}/usage` | `{schema_version:1,usage}`; reserved/reported/indeterminate accounting, `monetary_cost:null` and unknown pricing, not an invented zero price. |
| `GET .../jobs/{job_id}/provider-attempts` | Content-free attempt states/IDs, lineage and the duplicate-charge warning; `automatic_retry:false`, `retry_execution_available:false`. |
| `POST .../jobs/{job_id}/retry` | `{schema_version:1,prior_attempt_id,idempotency_key,acknowledge_duplicate_charge:true,warning_version:"duplicate_charge_possible_v1"}`; `202` with a distinct durable job, `retry_of`, `original_outcome:"indeterminate"` and `execution_available:false`. |

Warned retry is currently **queue-only**, not an enabled paid handler. The worker
rejects retry-lineage jobs with `HANDLER_UNAVAILABLE` before static research can
run. Only an owned indeterminate research/draft/assessment attempt is eligible;
the server copies its original parameters and captured inputs, rather than
accepting a new client payload or rebasing stale inputs. Current purpose consent,
unchanged dependencies and configured daily and retry-family limits are required.
Queuing does not read a provider key or transmit a request. Repeating an identical
retry request/key returns its job; changing that request conflicts.

The warning acknowledges that the original attempt may already have been billed.
Retry does **not** resolve its outcome, release its reservation or reconcile
billing. Original and sibling retry reservations continue to count against
limits; any future paid execution must separately authorize and reserve a new
attempt. Provider compatibility and paid drafting remain unqualified.

Summaries expose job ID/owner/application/kind/state, bounded stage, captured
revisions, fence/attempt count, cancellation flag, heartbeat/lease and generation
cleanup-pending flag. The `index`, static `research` and unpaid `packets` handlers are available. Workspace capability
flags do not imply researched-drafting or browser readiness.

Employer indexing requires the owned current complete, unexpired research head,
matching application inputs, company/exact-role coverage and original-source
ownership/integrity. Repeating the exact original request/key returns its job
before current-revision checks; changed scope or payload conflicts. Enqueue does
not initialize models/Chroma or launch research. The existing worker builds the
registered employer generation; failed/stale producers cannot publish over a
current eligible generation. Raw personal documents are not employer evidence.

The internal `EvidenceService.search_scoped` consumes one exact fact or employer
generation for all bounded query variants. Scope checks precede dense and BM25
ranking, followed by within-corpus fusion, reranking and canonical re-resolution.
Returned binding and full-canonical-hash references prove association/integrity,
not semantic support. This is not a new HTTP GET that runs model retrieval, nor
a drafting capability. Stored packet execution is a separate explicit job below.

### Unpaid local evidence packets

Packet POST accepts `schema_version:1`, a bounded `idempotency_key`,
`expected_facts_revision`, `expected_input_revision`, `expected_research_revision`,
optional `selections` (at most 100 `{source_id,unit_id,start,end}` code-point ranges),
optional explicit `cover_letter_target`, `per_packet_budget` (1–6,000, default
6,000) and `aggregate_budget` (1–24,000, default 24,000). No caller-supplied quote,
criterion text, generation identity or inferred hiring priority is accepted.
Cover-letter targets are writing questions with the reserved `cover_letter` ID,
an explicit question/constraint origin and a word limit (default 400).

Enqueue captures actual registered fact/employer generations without initializing
models. Exact original POST replay precedes current-revision checks. The worker
registers a database-only intent before composition, uses the fixed scoped hybrid
indexes and local tokenizer, and publishes the immutable batch, integrity-bound
compiler/binder omission report and job completion in one fenced transaction.
Exact selected quotes not fully contained in any registered chunk are visibly
omitted, never cropped into different requirements. No application-output revision is advanced.

Inspection reports `current` for canonical dependency currentness only—not loaded
model/Chroma readiness or semantic support. Intact historical batches can remain
inspectable with `current:false`; corrupt publication associations fail closed.
Always `semantic_support:"not_assessed"`, `review_eligible:false` and
`browser_eligible:false`. GET does not build, query, tokenize, initialize retrieval
services or call a provider. New facts/input/research or replacement generations
can make a batch stale; metadata, consent, documents and output are not packet
dependencies. Fact/source revocation conservatively forgets all owned packet
derivatives; application deletion forgets only that application's packet content.
A rejected packet operation can terminalize as `failed` / `packet_failed` under
its exact still-current lease without reauthorizing stale inputs. Cancellation,
expiry, reclamation, deleted scope or uncertain provider attempts cannot be
overwritten. This failure-only path grants no publication or paid-call authority.
These APIs do not enable paid drafting, semantic answer release or browser actions.

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

### Durable static research — bounded preview, not general discovery

Base: `/api/workspace/profiles/{id}/applications/{application_id}/research`.
The same boot token, owned application lookup and strict JSON rules apply.

| Route relative to base | Input / result |
| --- | --- |
| `POST /` | `{schema_version:1,idempotency_key,expected_input_revision,expected_output_revision,supporting_urls?:[...]}`; `202` with `{schema_version:1,job}`. At most four supporting URLs. |
| `GET /` | `{schema_version:1,current_run_id,runs}`; immutable history and current head, not a fallback to an older complete run. |
| `GET /{run_id}` | `{schema_version:1,run,eligibility:{eligible},sources}`; current-head/input/research-counter/freshness checks remain separate from historical run state. |
| `GET /{run_id}/sources/{source_id}` | `{schema_version:1,source,unit,identity_spans}`; canonical text, provenance hashes and Unicode code-point identity spans. |
| `GET /{run_id}/sources/{source_id}/original` | Hash-verified original bytes as a no-store, nosniff attachment, not executable source HTML. |

Repeat the exact original owned POST/key to retrieve its existing job, including
after successful publication advances output/research revisions. Changed request
data under that key conflicts. A new request must use current input/output
revisions. Poll/cancel through the profile job routes above. No model or Chroma
initialization is needed to acquire, inspect or delete static research.

The worker registers the attempt before acquisition and each original blob before
writing. All source producer locks remain held through the final fenced SQLite
publication of immutable runs, source metadata, canonical units and resulting
research/output heads. Failed or cancelled producers cannot publish later.
Application/profile deletion revokes access and captures exact employer-blob
cleanup obligations without adding employer text to personal evidence indexes.
Destructive cleanup validates the original owner/application/run/intent/job/fence
registration and device/inode receipt, not the current lease. Missing historical
proof stays pending; recovery never invents ownership or silently adopts old blobs.
Remaining source-linked employer text also prevents a cleanup-complete receipt.

`copilot.research.broker.research` accepts validated, server-captured non-personal
`ResearchInput` fields and a mandatory current-authority callback. It anonymously
acquires only explicit company/vacancy/supporting URLs on exact confirmed hosts.
Original bytes, canonical text/hashes and Unicode identity spans are persisted
by the fenced handler; research publication does not automatically index them,
and their text is never treated as instructions.
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
the stored current job/fence/revision checks. There is no discovery/search agent,
renderer, general ATS identity adapter or application drafting integration yet.
Employer indexing is available through the separate explicit route above. This static preview does not complete S3 or enable tailored
answer generation. Unsupported layouts remain visibly incomplete.
