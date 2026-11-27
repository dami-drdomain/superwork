# SuperWork (RAG Knowledge Base System) — User Guide

> English version of 使用说明.md. SuperWork consists of two major modules: the **📚 Knowledge Base** (local, offline hybrid retrieval, built on the RAG Knowledge Base System key technology specification v3.0) and the **✍️ SuperWork Writer** (intelligent work-document writing grounded in knowledge-base materials).

## 📌 Current Version: V1.8.5 (Freshness-Weighted Retrieval & Writer Date Pass-Through)

**V1.8.5** (2026-11-27): Minimal closed loop for "latest content first" in the knowledge base — resolves the problem where, when the same knowledge point exists in multiple versions, old and new content competed with equal weight: ① **soft time weighting** added to retrieval ranking (+0.10 × freshness in the lightweight tier; ≤+0.05 × freshness in the reranking tier; freshness = 0.5^(document age / 180 days), neutral 0.5 when mtime is missing), which only breaks ties between close scores without changing the relevance backbone; ② writer material lines are now labeled "Updated: YYYY-MM-DD" with document dates passed through, and the material header gains timeliness and conflict rules (prefer the newly updated version when multiple versions exist; annotate conflicting data with "as of YYYY-MM"; never fabricate); ③ About page author changed to "Dami" and the bottom note removed. Parameters are centralized in `rag_core.py` (`LW_W_TIME` / `RERANK_TIME_BONUS` / `TIME_DECAY_HALF_LIFE_DAYS`) — setting them to zero rolls back to V1.8.4 ranking. See the companion technical design "Freshness-Aware Retrieval and Citation for the SuperWork Knowledge Base" for details.

**V1.8.4** (2026-11-27): The writing workbench gained **Revision Mode** — after the first draft, a revision area appears in the result card; enter revision comments or additional requirements and click "🔄 Regenerate per feedback" to rewrite based on the previous draft, supporting **chained multi-round revision**: ① backend `WriteRequest` adds `previous_text / feedback`; `build_messages` takes the revision branch (original task + template structure requirements + previous draft in full + revision comments → complete new draft); the revision round **skips knowledge-base retrieval and outline generation**, which is faster and introduces no noise from new materials; ② the frontend retains the first-round task parameters (skill / expert / workflow / sample / topic) and reuses them in revision rounds; ③ Word template imitation re-runs layout-cloning export after revision, producing a new file with a new timestamp that does not overwrite the old draft for comparison; ④ if a revision round fails or is aborted with no new output, the previous draft is restored automatically for immediate retry; ⑤ HTML/PPT/Excel structured skills can also be revised (rewritten from the source draft and re-persisted).

**V1.8.3** (2026-11-27): Word template imitation export upgraded to **template layout cloning** — instead of generic export, each paragraph samples the template's formatting (page setup plus four paragraph styles: title / section / body / sign-off) and applies it to the generated draft, producing a Word document whose layout matches the template; the exported filename now derives from the level-1 heading of the generated body, inheriting the template's "Notice on XXX" naming style.

**V1.8.2** (2026-11-27): English branding set to **SuperWork**, embedded in the window title, header logo subtitle, About page, and application name row; document-writing skills plus Word template imitation (.docx/.doc) now **auto-export a Word file** on completion (no longer stopping at Markdown), falling back to Markdown with a manual export button if auto-export fails.

**V1.8.1** (2026-11-23): The writing workbench skill menu was adjusted to list **only built-in (including overrides) and "My Skills"**; the 25 TeleAgent-imported skills no longer appear in the menu. Imported skills become selectable in the workbench after clicking "**Solidify**" in Skill Management (a clear path of import → solidify → use). The writing API remains compatible with imported skill ids from historical calls; the Skill Management UI notes "not in the workbench; solidify first".

**V1.8.0** (2026-11-23): Skill system restructured into a **three-layer skill library** — the "Skill Source" section of the Model Configuration page became "**Skill Management**":
① **Built-in (factory)**: the 5 factory skills support in-app editing (name / icon / description / output type / prompt); changes are written to the local override layer `writer_skills.json`, never touch factory code, survive upgrades, and support one-click "restore factory defaults". ② **My Skills**: create from scratch via "New", or one-click "**solidify**" any TeleAgent-imported skill (full prompt persisted, no longer depends on the TeleAgent source directory, still editable), with an assignable output type (**Markdown / HTML / PPT / Excel**) for any skill — when a structured output is selected, the full-document / strict-JSON format requirements are automatically appended by the engine. ③ **TeleAgent Imports**: remains a SKILL.md snapshot; refresh only syncs this layer and never overwrites My Skills or built-in edits. Data upgrade: `writer_skills.json` migrated to v2 (imported / custom / overrides sections), with automatic legacy migration and atomic writes. The workbench skill menu became a **three-layer grouping with search** (quick location among 30+ skills), and the output badge indicates Markdown-exportable-to-Word / HTML / PPT / XLSX. Verified with 42 unit tests plus full-flow testing (including zero regression on the 5 built-in skill prompts). Two fixes made during testing: ① custom skills generating HTML/PPT/Excel did not persist files — persistence had been keyed on the skill identifier (built-in skills only worked by coincidence); it is now keyed on the skill's declared output type, so any skill with structured output goes through the corresponding persistence pipeline; ② HTML files retained the model's opening remarks and code fences — a document-body extractor was added (slicing from the first DOCTYPE/<html> and ending at </html>), leaving clean file boundaries, which also benefits the built-in HTML skill.

**V1.7.17** (2026-11-23): Sample scope for the PPT skill widened — **PPT (.ppt) / PPTX (.pptx) / PDF** are all selectable, no longer pptx-only. Legacy binary `.ppt` is handled by a new dependency-free atomic text parser in `rag_core.py` (slide-master placeholder filtering, dedup with order preservation, millisecond-level for 20MB files); 10 real .ppt files tested with 0.97–1.00 readable rate, all passing the writing gate. Exported PDF decks can likewise serve as PPT imitation samples. Document skills remain Word/PDF/TXT/MD, HTML skills HTML, Excel skills Excel/CSV.
**V1.7.16** (2026-11-23): Template imitation gained **sample format constraints** — the sample must be in the same format family as the skill's output: document skills (Document Writing / Deep Research / imported skills, which output Markdown exportable to Word) accept only **Word (doc/docx) / PDF / TXT / MD**; HTML skills only **HTML/HTM**; PPT skills only **PPTX**; Excel skills only **XLSX/XLS/CSV**. Three layers of validation: ① the "select sample" dialog shows only compatible formats for the current skill; ② automatic validation after selection and on skill switching, rejecting/clearing mismatches with guidance; ③ double backstops before sending and at the backend API, erroring out directly on format mismatch — eliminating invalid imitations such as "using an Excel file as a Word sample".
**V1.7.15** (2026-11-23): Toolbar adjustment — ① buttons reordered to **Skill (◎) → Expert (🎓) → Workflow (⇄) → Sample (📄) → Model**; ② **sample–workflow linkage**: the "select sample" button is clickable only when the workflow is "Template Imitation" (disabled elsewhere with hover explanation), replacing the old reverse logic of auto-switching to template imitation after sample selection; ③ leaving Template Imitation hides the sample chip but retains the data (no re-selection on return); ④ selecting Template Imitation toasts guidance to pick a sample.
**V1.7.14** (2026-11-21): Three workbench UX improvements — ① **right-click edit menu** in inputs/textareas: Cut/Copy/Paste/Select All (the app's WKWebView lacks the system edit menu); cut/copy with fallbacks; paste uses a degradation chain (system command → clipboard API → 2.5s timeout → prompt for Cmd+V); clicking outside dismisses the menu. ② **One-click clear**: a 🧹 toolbar button clears the current input and generation results (documents already saved to Writer Documents are unaffected; in-flight generation is aborted), resolving stale-task residue when switching pages. ③ **Fixed an HTTP caching hazard**: when responses lack cache headers, the WebView reuses cached stale pages during the "heuristic freshness period" (the UI might not update for a long time after an upgrade) — a middleware now sets `Cache-Control: no-store` on all responses (full local responses return in milliseconds, eliminating stale-page issues).
**V1.7.13** (2026-11-21): Fixed "system health verification reporting mass failures" — standard/evening checks failed 6 of 9 items while the service was actually healthy (dual-channel 124,213 chunks consistent). Root cause: the verifier used the 200ms performance threshold simultaneously as the liveness-probe timeout; under heavy memory pressure (swapping), /health routinely took 250ms, one probe timeout marked the service "unreachable", cascading into 5 failed items and skipping function checks. Five fixes: ① **decoupled liveness probing from the performance threshold** (probe checks status code only with a relaxed timeout; response time evaluated separately); ② health threshold 200ms → **500ms** (measured normal load on the 8GB machine is 250ms; the old threshold flagged normal states as abnormal; >500ms still reports red honestly with a load-check hint); ③ memory threshold 2048MB → **3072MB** (2.3GB measured steady state after growing to 124k chunks is normal linear growth); ④ daemon check **marked N/A in app mode** (that component targets standalone server deployments; it was permanently red and misleading when running as an app); ⑤ fixed the build_date default-value defect in the new-file retrieval check (1900 existing files were treated as "created today" and pushed into retrieval sampling, and short numeric names naturally missed, causing swathes of false positives) + backfilled build_date in the index state (previously never recorded, silently disabling that check). After the fix: standard 25/25, evening 16/16 all green.
**V1.7.12** (2026-11-21): Fixed "build progress frozen on a stale frame" — user observed: a 168-file build had long completed (42 minutes) yet the page displayed "progress 4/168 files (2.4%)" for 4 hours. Root cause: the progress file was written non-atomically; when the frontend's 5-second polling collided with per-file writes from the build thread, the API read an empty/half JSON, took the exception branch, and **falsely reported building_now=False**, after which frontend polling stopped permanently and progress froze on the last frame. Fixes: ① progress writes switched to **temp file + atomic replace** (same mechanism as the state file); ② on read failure the API falls back to the **in-memory running flag** — never falsely reporting "build ended". This also clarified the earlier "only 4 files in 2 hours" perception — the build had been proceeding normally; only the progress display was dead.
**V1.7.11** (2026-11-21): Index-build experience improvements — following the investigation of "168-file build feeling extremely slow": engine speed was normal (168 files / 7,951 chunks finished in 42 minutes, consistent with historical batches; the perceived slowness came from rerunning interrupted builds + swap thrashing under memory pressure, with only 1.5GB available at the time). Improvements: ① **memory-pressure warning at build start** (below 1.5GB free: logs and the progress bar suggest closing memory-heavy apps like WPS/browsers); ② **faster incremental scanning**: unchanged files skipped by size + mtime (previously every round re-read 1,700+ files from disk to compute MD5, wasting 10–15 seconds per round); only suspected changes get an MD5 recheck; ③ **explicit end/interrupt feedback**: the completion summary is persisted to the progress file and the frontend toasts "build complete (about X minutes)" when polling detects the end; interruptions (app restart/crash) prompt "progress saved; incremental rebuild resumes from the checkpoint" — previously builds ended silently and users had no way to know their status.
**V1.7.10** (2026-11-21): Fixed irrelevant results dominating smart search — in testing, the top two results for "my pension-care conference speech" were winter-vacation homework documents. Two root causes: ① **the keyword-hit criterion for the BM25-only boost could be triggered by padding with document-genre generic words like "conference/speech"** (with the subject word "pension care" missing, the +10 overwhelming boost still applied); ② **the lightweight reranker's RRF normalization baseline was inflated by boosted candidates** (10 vs the normal 0.03), squashing genuine semantic hits (the speech's normalized score was only 0.004) to below the boosted documents. Fixes: ① boost triggering now **counts only non-genre words** (a genre-word list of 30+ generic office-document terms added), requiring a subject-word hit for boosting; ② the normalization baseline **excludes boosted candidates** (real candidate scores restored with relative order unchanged; boosted candidates keep their positions; top-5 composition unchanged). After the fix the top three results are all pension-care speeches, with the homework gone; evaluation baseline **96.9% (31/32), zero regression**; BM25 recall, filename matching, and context assembly are unaffected.
**V1.7.9** (2026-11-20): Model Configuration LLM endpoint examples updated — ① the first available-service example switched to **Ctelecom Xirang (天翼云息壤)** (`https://wishub-x1.ctyun.cn/v1`, the OpenAI-compatible inference endpoint of the Xirang model hub; keys obtained on the Ctelecom cloud portal); ② the model-name example changed from deepseek-chat to **deepseek-flash**; the workbench's unconfigured hint synced accordingly.
**V1.7.8** (2026-11-20): Smart search upgrade — ① **10-result cap removed**: all relevant results return at once (engine cap 100), with the total shown on the badge; ② added **5/10/20 per page (default 5)** and a **bottom pager** (page numbers, prev/next, middle pages auto-elided when many); ③ result entries: **click the filename to open with the system default app**, plus **📂 Reveal in Finder**; ④ engine candidate pool scales with the requested count (default query behavior unchanged, zero retrieval-quality regression).
**V1.7.7** (2026-11-20): Full-text search results upgraded — **click a filename to open the document with the system default app** (md → editor, docx → Word/WPS, pdf → Preview, etc.), with a new **📂 Reveal in Finder** button; result entries now include absolute paths on the backend (reusing /api/write_reveal with unchanged path boundary validation).
**V1.7.6** (2026-11-20): The Writer module's "My Documents" page renamed to "**Writer Documents**" — nav button, page title, save button, space dropdown, save/export toasts, and this guide all synced, distinguishing it clearly from the Knowledge Base's "Document Library" (the index inventory of stored materials); storage location writer_docs/ and existing list data unchanged.
**V1.7.5** (2026-11-20): ① md-to-Word exports now **save to the same directory as other formats and appear in the list** (no longer an exports subdirectory; historical exports migrated to the main directory); ② removed the "View" / "📂 Reveal" buttons — **clicking a document title opens it with the machine's default app** (md → editor, docx → Word/WPS, pptx/xlsx/html likewise); the in-app preview card removed as well.
**V1.7.4** (2026-11-19): "My Documents" refactored — ① unified listing: **Markdown / PPT / Excel / HTML all shown** (previously only md/Word); ② "Export to Word" shown only for Markdown documents; ③ the header's "Word export directory" became **📂 Documents directory** (direct to the writer_docs main directory); ④ the "Type" column became a **Format** column (Markdown / PPT deck / Excel table / HTML document); the 10 legacy document types no longer appear; ⑤ new "📂 Reveal" button (locate any format in Finder, double-click to open); the stats row shows per-format counts.
**V1.7.3** (2026-11-19): Fixed "Excel/PPT come out empty" — the root cause was the model occasionally emitting syntactically invalid JSON (unescaped quotes, thousands separators, truncation); the old logic fell back to text recovery and salvaged only 1 row. Now a **three-tier defense**: local regex repair → **ask the model to auto-repair the JSON (temperature 0)** → structured fallback (text fallback supports super-long single-line splitting), with head/tail fragments of bad JSON logged for diagnosis.
**V1.7.2** (2026-11-19): Excel generation upgraded to a **design engine** (`rag_xlsx.py`) — each worksheet auto-generates a title band, a caliber-note row, themed headers (frozen panes), zebra striping with fine borders, number formats (thousands/percent), **formula total rows (=SUM)**, data bars on key columns, and **native bar charts**; invalid model JSON falls back to rebuilding from markdown tables.
**V1.7.1** (2026-11-19): ① PPT/PPTX limits for auto-collection and indexing raised from 100MB to **200MB**; ② the PPT layout engine added 5 graphic layouts — **timeline, layered hierarchy, four-quadrant matrix, hub-and-spoke, big-number stat** — with bullet pages upgraded to numbered line layout; the prompt now requires graphic pages first (pure bullet pages at most half of content pages).
**V1.7.0** (2026-11-19): PPT generation rebuilt as a **design system + layout engine** (`rag_pptx.py`) — 10 layouts (cover / section / bullets / KPI cards / native charts / process / comparison / table / quote / closing), 5 themes (government red-gold, business blue, tech dark, warm, minimal, auto-matched per expert), automatic text fitting and column splitting, footers with page numbers and speaker notes; output is native editable objects, not images.
**V1.6.3** (2026-11-19): Per-file limit for regular documents raised from **10MB to 20MB**; PPT/PPTX unchanged at 100MB (index scanning and auto-collection in sync).
**V1.6.2** (2026-11-18): Document Library table sortable by **filename / type / size / modified time / chunks** — click a header to switch field (click again to toggle asc/desc) with ▲▼⇅ indicators; the title bar shows the current sort. Default: modified time descending.
**V1.6.1** (2026-11-18): Removed the "fully automatic" button (the "Outline First" workflow still achieves the same result explicitly); the model dropdown moved into its place; unified output-result wording — only Markdown-outputting skills show "exportable to Word"; HTML/PPT/Excel skills show only their respective file types; imported skills are labeled "no file generated".
**V1.6.0** (2026-11-18): Output deliverables now **determined by skill** — HTML/PPT/Excel skills each produce their corresponding file; Document Writing, Deep Research, and TeleAgent-imported skills default to **Markdown directly exportable to Word**; removed the entire "Advanced Settings" block (document type / length / style / material retrieval / imitation template); the old "add attachment" became **📄 select sample**, letting you pick a local document as an imitation sample (drafting follows its format and section architecture).
**V1.5.9** (2026-11-18): Added **TeleAgent skill import** — the Model Configuration page can read the local TeleAgent skill directory (name and body of each `SKILL.md`) in one click; imported skills then appear in the workbench's skill menu, tagged "prompt-only". Importing reads only the description files, **does not require TeleAgent to run**, and copies no scripts; the 5 built-in file-producing skills are unaffected. Supports refresh (add/update/remove) and per-skill removal.
**V1.5.8** (2026-11-18): Abolished the abstract "flagship/standard/turbo" tiers in favor of **model selection** — the workbench dropdown lists configured LLMs directly (list as many as configured, switch anytime); the Model Configuration page became a model list (add/remove multiple models, each with its own endpoint and key) plus **aliases**: if set, the dropdown shows the alias; otherwise the model name. Legacy V1.5.7 tier settings auto-migrate into the model list (deduplicated by actual model).
**V1.5.7** (2026-11-18): Model tiers upgraded to **real model tiers** — the workbench's five tiers (Flagship / Smart / Turbo / Light / Swift) each can be assigned a specific model on the Model Configuration page (endpoint and key can also differ); unassigned tiers inherit the common configuration. Switching tiers switches models, no longer merely adjusting output length. Legacy tier labels (Standard / Turbo) map automatically.
**V1.5.6** (2026-11-18): Workbench redesigned as "conversational input box + toolbar" — supports **Skills**: Document Writing / HTML Documents / PPT Decks / Excel Tables / Deep Research; **Experts**: Official Documents / Government Solutions / Technical Proposals / Bidding Documents / Data Analysis / Product Operations; plus workflows (Standard / Outline First / Template Imitation), model tiers, local attachments, fully automatic execution (outline then draft); HTML/PPT/Excel skills persist locally with preview and reveal-in-Finder.
**V1.5.5** (2026-11-18): Removed the "Knowledge Wiki" subsystem (entries, bidirectional links, graphs, and auto-entry generation all offline), streamlined navigation accordingly; the workbench's "save as entry" and the library's "convert to entry" buttons removed. Includes fixes accumulated across V1.5.x: single-instance protection (rejects double-launch), atomic index-state writes, PPT/PPTX limit raised to 100MB.
The last version containing the full Wiki is **v5.3.5** (preserved in `超脑工作搭子-源码包-v5.3.5.zip`); when needed, that package can restore Wiki features and data (data backed up in .temp/wiki-backup-v5.3.5/).

## ⚠️ Important: Put Documents into the `docs/` Subdirectory

**The knowledge-base document directory is `rag-kb/docs/`.** Files placed directly in the `rag-kb` root (e.g., PPT/PDF dropped into the root) **will not be indexed**. Put all documents to be indexed into `docs/` (subdirectories allowed for classification), then run an incremental build under "Index Management" in the app.

- Supported formats (11): docx, doc, pdf, xlsx, xls, pptx, md, txt, csv, html, htm
- Files larger than **20MB** are skipped (raised from 10MB in V1.6.3); split the document if you need it indexed. **Exception: PPT/PPTX, limit 200MB** (100MB since v5.3.4, relaxed in V1.7.1)

## 🖥️ Desktop App (Recommended)

**Double-click `rag-kb/超脑工作搭子.app` to launch**; all features are available in the GUI:

- **Single-instance restriction (v5.3.5)**: launching again while the app is running does not open a second window; a notice pops up and the new instance exits — two instances concurrently writing the same knowledge base would cause mutual index deletion / database corruption (root cause of the v5.3.4 incident). Click the Dock icon to switch back to the open window; if the old instance is unresponsive, quit the Python process in Activity Monitor first, then relaunch.

| Page | Function |
|------|----------|
| Dashboard | Engine status overview (chunks / BM25 / memory / document count / latency), quick actions |
| Smart Search | Queries + advanced options (time filter / directory filter / rewriting / reranking / context assembly / legacy documents); **all results returned + 5/10/20 per page + bottom pager**; click a filename to open in the default app, with adjacent "📂 Reveal in Finder" |
| Index Management | Incremental build / full rebuild / BM25 rebuild / full-text cache update (BM25 auto-rebuilds after vector builds) |
| Evaluation Center | One-click 32-case evaluation (refreshed v5.2.6, dropping 3 obsolete cases), hit rate / top-1 accuracy / latency + per-case detail |
| Verification Report | Morning / evening / standard three-mode four-dimension verification, with degradation detection and PDF reports |
| Backup & Restore | Incremental/full backups, snapshot list, expiration cleanup |
| Full-text Search | Keyword (AND/OR) / regex / grep line context / directory filter; **click a filename to open in the default app**, with "📂 Reveal in Finder"; with grep checked: hit lines listed per line (up to 30, with line numbers) plus context lines; unchecked: snippets near the first keyword occurrence (up to 3 per file) |
| Document Library | Inventory of docs/ files; sortable by **filename / type / size / modified time / chunks** (click again to toggle order) |
| Auto Collection | Automatically senses documents opened by WPS/Mail etc. and ingests them (see the next section) |
| Writing Workbench | Conversational input: skill × expert × workflow, citations from the knowledge base, streaming generation, producing Word/HTML/PPTX/XLSX; toolbar order **Skill → Expert → Workflow → Sample → Model**, grouped skill menu with search (built-in and My Skills only; imported skills appear after solidifying — see Skill Management); samples available only under Template Imitation and **must match the skill's output format** (Word/PDF/TXT/MD, HTML, PPT/PPTX/PDF, Excel each forming a family); **right-click in inputs: cut/copy/paste/select all**; **🧹 one-click clear** (Writer module) |
| Writer Documents | Unified list of all generated documents (Markdown/Word/PPT/Excel/HTML); click a title to open with the default app; Markdown converts to Word; deletable |
| Model Configuration | Common configuration: knowledge-base retrieval model (local, offline) + writer LLM endpoints (user-configured) |

App notes:
- On startup the window opens first; the engine loads in the background (about 10–20 seconds).
- The app is fully API-compatible with `rag_server` (port 18800); CLI tools can target the running app directly.
- Logs go to `rag-kb/app.log`; check this file first for startup issues (build progress is also written live for troubleshooting).
- **Index builds use 4 parallel extraction processes (v4.8)**: extraction and encoding are pipelined, so large files never block others (consumed as completed); live progress shown (X/Y files and ETA); closing the app mid-build resumes from the checkpoint on the next incremental build — already-processed files are not re-encoded. BM25 rebuilds use batched pulls (v5.2.1), so 100k+ chunk corpora no longer hit database variable limits.
- Garbled text (e.g., legacy OLE .doc misreads) is skipped automatically, wasting no encoding time and polluting no search results; clicking build during a build prompts "a build is already in progress".
- **Do not move the `rag-kb` directory** (the .venv depends on absolute paths); create an app alias on the Desktop instead.

### 📥 Auto Collection (v5.2)

Automatically bring documents "opened by" other local software into the knowledge base — no manual copying:

- **Principle**: macOS records which application last opened each file (LaunchServices usage records). Files opened by WPS, Preview, browser downloads, or any other app are marked by the system; the app periodically queries those records (Spotlight) and automatically copies supported formats into `docs/自动收集/YYYY-MM/`, triggering an incremental build.
- **Mail / WeChat / DingTalk channels**: these apps store files in fixed directories under `~/Library` (excluded from the global Spotlight channel by default); the system performs targeted queries of "recently opened" records there. **Documents received in chats but never opened are not collected** (received ≠ opened); the mail-attachment directory adds a fallback based on on-disk time.
- **All supported channels**: any app via recent-use (WPS etc.), Apple Mail, NetEase Mail Master, WeChat, DingTalk. Adding clients requires one line in the `APP_CHANNELS` list of `rag_autocollect.py`.
- **On by default**: scans every 10 minutes, looking back 2 days; adjustable on the Auto Collection page (interval, lookback days, size limit — default ≤20MB, aligned with the indexing limit; **PPT/PPTX dedicated limit 200MB (V1.7.1)**, adjustable via `max_mb_ppt` in `autocollect_config.json` and `rag_core.MAX_FILE_SIZE_PPT`), or disable entirely.
- **Dedup**: files with identical name and size are not re-collected; a modified file (size changed) is re-ingested as a timestamped copy. System directories, Trash, app bundles, and the knowledge base's own files are excluded automatically.
- **Config file**: `rag-kb/autocollect_config.json`; collection state: `docs/.rag_db_embed/.autocollect_state.json`.
- Standalone test command: `cd rag-kb && .venv/bin/python rag_autocollect.py`

## 🛡️ Anti-freeze Monitor (app_monitor.py)

An independent monitoring process that guards against the app freezing while building indexes or switching models:

```bash
cd rag-kb
.venv/bin/python app_monitor.py --verbose --auto-restart   # foreground monitoring + auto-restart on freeze
# Resident in the background:
nohup .venv/bin/python app_monitor.py --auto-restart >> monitor_run.log 2>&1 &
```

- One round every 10 seconds: process alive / HTTP health / **liveness detection** (CPU time + log growth, distinguishing "busy" from "dead")
- Stalled 3 minutes → desktop notification; stalled 5 minutes (--auto-restart) → automatic app restart
- Slow HTTP during large index builds is normal (the monitor judges "busy working", not frozen)
- Log: `rag-kb/monitor.log`

## 🤖 Model Configuration (Common Configuration)

**Knowledge-base retrieval models (models_config.json)**
- Defaults: embedding **BAAI/bge-base-zh-v1.5** (768-dim), reranker **BAAI/bge-reranker-base**, both loaded locally and offline
- On the Model Configuration page you can switch to models in the local `models/` directory (dimensions/size/type auto-detected), or enter a model name/path
- Switching the embedding model requires a full index rebuild (the UI prompts); failed loads roll back automatically
- Priority: `models_config.json` > environment variables RAG_EMBED_MODEL/RAG_RERANK_MODEL > defaults

**SuperWork Writer LLM endpoints (llm_config.json)**
- OpenAI-compatible APIs; users configure the endpoint / model name / API key, stored locally only — nothing is uploaded
- Compatible services: Ctelecom Cloud Xirang (`https://wishub-x1.ctyun.cn/v1`), DeepSeek, Qwen, Zhipu, Kimi, local Ollama — any OpenAI-compatible endpoint
- One-click connection test on the Model Configuration page; without configuration, knowledge-base functionality is entirely unaffected

**Model list (the models section of llm_config.json)**
- One model per row on the Model Configuration page; "➕ Add model" configures multiple; the workbench dropdown selects directly by model
- **Alias**: when set, the dropdown shows the alias (e.g., calling `deepseek-v4-pro` "Deep Thinking"); when blank, the model name is shown
- Each model can have its own endpoint and key (blank = inherit the default model's endpoint/key); the "Default" radio determines the initially selected model
- Each model can set its own temperature and max tokens (blank = 0.7 / 4096); each row has its own "🔌 Test"
- Note: model selection affects only writing calls, not knowledge-base retrieval

## 🧩 Skill Management (Three-Layer Skill Library, V1.8.0)

- Entry: Model Configuration page → "Skill Management". Three layers: **Built-in (factory)** · **My Skills** · **TeleAgent Imports**
- **Built-in skills are editable**: click "Edit" to modify name / icon / description / output type / prompt; saving writes to the local override layer (the overrides section of `writer_skills.json`), never touching factory code and surviving upgrades; one-click "restore factory defaults" if you break something
- **My Skills**: create from scratch with "➕ New Skill"; TeleAgent-imported skills can be "**solidified**" into native local skills — the full prompt is persisted, no longer depends on the TeleAgent source directory, and remains editable and deletable (an `origin` field preserves provenance)
- **Assignable output types**: any custom/solidified skill can choose Markdown (exportable to Word) / HTML / PPT / Excel; for the latter three, the skill prompt need not include full-document / strict-JSON format requirements — **the engine appends the output contract automatically** (same source as built-in prompts; not duplicated if the prompt already includes one)
- **TeleAgent Imports**: reads `~/.config/TeleAgent/users/<user>/skills/*/SKILL.md` (a global directory and the `TELEAGENT_SKILLS_DIR` environment variable also work); takes only name, description, and body (first 4,000 characters), **copies no scripts and does not require TeleAgent to run**; "Import / Refresh" syncs only this layer, never affecting My Skills or built-in edits; imported skills can be solidified or removed
- **Script-dependent skills**: skills whose SKILL.md depends on external scripts / CLI tools (e.g., PDF assistant, scheduler, WPS knowledge base) will not execute those scripts even after solidifying — they still write per the prompt; keeping such skills in the import layer is recommended
- Storage: entirely in `rag-kb/writer_skills.json` (v2: imported / custom / overrides sections, atomic writes, automatic legacy migration)
- Workbench skill menu: lists only the "Built-in / My Skills" groups (**the TeleAgent import layer never enters the workbench**), with a top search box filtering by name/description; solidified imported skills appear automatically

## ✍️ SuperWork Writer

- **Writing Workbench (redesigned V1.5.6)**: describe the document you need in the input box (topic / audience / key points / data / timing), then choose on the toolbar below:
  - **Skill** (grouped menu with search; **built-in and My Skills only**): 5 built-in — Document Writing (Markdown, exportable to Word), HTML Documents (single-file HTML with preview), PPT Decks (design-system layout engine, editable PPTX), Excel Tables (design engine, editable XLSX), Deep Research (Markdown, multi-round retrieval + source annotation); My Skills (created or solidified from imports, with assignable output types); TeleAgent imports (SKILL.md snapshots, editable after solidifying)
  - **Expert**: Official Documents / Government Solutions / Technical Proposals / Bidding Documents / Data Analysis / Product Operations (setting perspective and professional tone)
  - **Workflow**: Standard (one pass) / Outline First (outline then draft) / Template Imitation (follow a template's format and architecture)
  - **Model**: the dropdown selects directly among configured LLMs (alias or model name shown); switching changes models; for outline-then-draft, switch the workflow to "Outline First"
  - **📄 Sample**: pick a local document and imitate its format and section architecture (not persisted, no prior knowledge-base ingestion required); **the sample format must match the skill's output** — document skills take Word/PDF/TXT/MD, HTML skills HTML, PPT skills PPT/PPTX/PDF (legacy .ppt and PDF supported since V1.7.17), Excel skills Excel/CSV; the picker shows only compatible formats, and an incompatible sample is auto-cleared with a hint on skill switching; **📁 Space**: choose where generated files are saved
  - Outputs by skill type: HTML / PPT / Excel skills persist corresponding files (preview / reveal in Finder); Document Writing, Deep Research, and imported skills output Markdown — copy, download, **export to Word**, or save to Writer Documents
  - Note: Advanced Settings were removed; length and style default to "medium length + formal register", and you can state requirements directly in the prompt (e.g., "about 3,000 words, conversational")
- **Markdown skills** (Document Writing / Deep Research / imported skills) support after generation: copy all / download Markdown / **export to Word** (converted files save into the Writer Documents main directory and appear in the list) / save to Writer Documents; HTML / PPT / Excel skills generate standalone files with preview and reveal in Finder
- Configure an LLM endpoint under Model Configuration before use; knowledge-base materials are retrieved automatically by topic by default

## 📊 PPT Layout Engine (V1.7.0)

- Generation logic: the model only produces "content structure" (strict JSON); layout is rendered by the local engine — typesetting is never entrusted to the model
- Supported layouts (15): cover / section divider / bullets (numbered lines) / KPI cards / native charts (column, bar, line, pie, doughnut, area) / process / left-right comparison / table / quote / **timeline / layered hierarchy / four-quadrant matrix / hub-and-spoke / big-number stat** (the latter five added in V1.7.1)
- Themes: government red-gold `gov`, business blue `business`, tech dark `tech`, warm `warm`, minimal `minimal`; auto-matched by the selected expert (government → gov, technical → tech, bidding → business, data → minimal, product → warm; for the "general" expert, content decides)
- Readability guarantees: font size auto-downsizing by text volume, automatic two-column split when bullets overflow, unified margins, heading hierarchy, footers with page numbers
- Editable: everything is native PPTX text boxes, shapes, charts, and tables (editable in PowerPoint / WPS), with speaker notes on every slide
- Data constraint: chart and KPI numbers must come from the topic / key points / knowledge-base materials; absent numbers, the model should use bullet pages rather than fabricate charts

## 📈 Excel Generation Engine (V1.7.2)

- Same generation logic as the PPT engine: the model emits structured JSON only; rendering is done locally by `rag_xlsx.py`
- Fixed visual grammar per worksheet: title band (theme color, merged across columns) → caliber-note row → headers (theme colors + white text + frozen panes) → zebra-striped data rows with fine borders
- Numeric capabilities: automatic thousands/percent formats on number columns; columns declaring `total` auto-generate **=SUM formula total rows**; numeric columns named in `highlight_col` get **data bars**; with sufficient data (≥4 rows), a **native bar chart** is inserted to the right of the table
- Shares the five PPT color themes, auto-matched by expert
- Fault tolerance: invalid model JSON auto-rebuilds from markdown tables (`|`-delimited); the legacy `columns` string array still works
- Note: output is `.xlsx` (openable and editable in Office 2007+ / WPS); legacy binary `.xls` is not an output format

## Directory Structure

```
rag-kb/
├── docs/                    # ★ Knowledge-base document directory (put documents here)
│   └── .rag_db_embed/       # Vector store and index storage (auto-generated; do not edit manually)
├── models/                  # Local model directory (bge-base / bge-reranker / bge-small)
├── models_config.json       # Model configuration (read/written by the app UI)
├── 超脑工作搭子.app          # macOS application (double-click to launch)
├── app/index.html           # App GUI
├── rag_core.py              # Core: format routing / noise cleaning / structure-aware chunking
├── rag_engine.py            # Retrieval engine: query rewriting / RRF fusion / 3-tier reranking / context assembly
├── app_main.py              # Lightweight app entry (v4.8; spawns extraction subprocesses without reloading heavy deps)
├── rag_app.py               # Main app program (pywebview + FastAPI, all endpoints)
├── rag_writer.py            # SuperWork Writer (LLM access / templates / streaming / Word export)
├── llm_config.json          # LLM endpoint configuration (user-entered; local only)
├── writer_docs/             # Archive of generated documents (incl. exports/)
├── app_monitor.py           # Independent anti-freeze monitor
├── rag_build_embedding.py   # Vector index building (incremental/full; parallel extraction since v4.8)
├── rag_build_bm25.py        # BM25 keyword index building
├── rag_server.py            # Resident retrieval service (CLI mode)
├── rag_search.py            # CLI retrieval client (with local fallback)
├── rag_watchdog.py          # Service daemon (30s probes / auto-restart / resource alerts)
├── rag_verify.py            # Daily verification system (four-dimension checks + PDF reports)
├── rag_backup.py            # Backup & restore (incremental snapshots, 7-day retention)
├── build_text_cache.py      # Full-text cache search (keyword / regex / grep modes)
├── daily_ops.sh             # Daily automation orchestration (nightly builds / morning & evening checks)
└── requirements.txt
```

## Command-Line Usage (Optional)

```bash
cd rag-kb   # run all commands from this directory
.venv/bin/python rag_build_embedding.py          # incremental build
.venv/bin/python rag_build_bm25.py               # BM25 rebuild
nohup .venv/bin/python rag_server.py > server.log 2>&1 &   # service-only mode
.venv/bin/python rag_search.py "query text"      # CLI retrieval
.venv/bin/python rag_verify.py --mode morning    # four-dimension morning check
.venv/bin/python rag_backup.py backup            # incremental backup
```

## Scheduled Tasks (Optional)

```bash
crontab -e
0 22 * * * cd /path/to/rag-kb && bash daily_ops.sh nightly >> nightly.log 2>&1
0  8 * * * cd /path/to/rag-kb && bash daily_ops.sh morning >> morning.log 2>&1
*/5 * * * * cd /path/to/rag-kb && .venv/bin/python rag_watchdog.py --once --quiet
```

## Key Parameter Locations (Customization)

| Customization | Location |
|---------------|----------|
| Model selection | `models_config.json` (or the app configuration page) |
| Synonym dictionary | `rag_engine.py` → `SYNONYM_DICT` |
| Evaluation cases | `rag_engine.py` → `EVAL_CASES` |
| Chunking parameters | `rag_core.py` → `MAX_LEN / MIN_LEN / OVERLAP` |
| Auto-collection config | `autocollect_config.json` (or the Auto Collection page; channel directories in `rag_autocollect.py` → `APP_CHANNELS`) |
| Service port | environment variable `RAG_SERVER_PORT` (default 18800) |
| Document directory | environment variable `RAG_DOCS_DIR` (default `rag-kb/docs`) |

> AI-generated

> AI生成
