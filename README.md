# IUG Chatbot — Islamic University of Gaza Assistant

**العربية: [README.ar.md](README.ar.md)**

An Arabic university assistant built on RAG (retrieval-augmented generation): it
reads the university's knowledge from MongoDB, searches it with hybrid retrieval
(Jina semantic embeddings + BM25 lexical), and answers through an OpenAI-compatible
LLM (currently OpenRouter) — with JWT authentication, roles
(guest / student / employee / admin) that isolate data structurally *before* it
ever reaches the model, and a full admin panel for content and permissions.

**Status**: 316 automated tests ✅ · golden eval passing ✅ · 99-question live
test · 9-point security review addressed · deployable on Render.

---

## Highlights

| Capability | How |
|---|---|
| **Answers only from university knowledge** | Hybrid retrieval (semantic + BM25 merged via RRF), generation constrained to retrieved chunks |
| **Roles & permissions** | guest/student/employee/admin — files are filtered **before** search; an "internal staff" file never even enters a student's ranking |
| **Student-data linking** | "My department head? My GPA? Field training?" answered from the student's own record and major automatically |
| **Semantic conversation memory** | Last 5 turns with stored embeddings; only relevant turns injected; "and for the master's?" understands its predecessor |
| **Guest follow-ups** | Guests have no server sessions (by design) — the browser carries the last 5 turns and sends them per request; nothing is stored |
| **Colloquial Arabic** | "كيف اجل الفصل" matches "تأجيل الدراسة" in the data |
| **Admission questions see the full cutoff table** | "Which majors accept my 81%?" is aggregative — the whole cutoffs file is sent, not just the nearest chunks |
| **Newest wins** | When two files conflict, the most recently updated file's version is preferred |
| **Hallucination control** | Procedural details come from chunks only; out-of-scope people/politics questions politely refused |
| **Admin panel** | JSON upload with instant publish, per-file classifications and roles, delete, versions & rollback, audit log |
| **Robustness** | Privacy-safe cache, rate limits, uniform errors, unique indexes, fail-closed secrets |

---

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in the keys (see the environment table below)
python server.py          # → http://127.0.0.1:8000/app (UI) and /docs (API docs)
```

- Student UI: `http://127.0.0.1:8000/app/`
- Admin panel: `http://127.0.0.1:8000/app/admin.html` (requires an admin account)
- Employee portal: logging in with an employee ID routes to the employee UI

### Tests & evaluation

```bash
PYTHONIOENCODING=utf-8 python -m pytest tests/ -q     # 316 tests
PYTHONIOENCODING=utf-8 python eval/run_eval.py        # live golden eval (needs .env)
```

> `PYTHONIOENCODING` is needed on Windows because of emoji in the output.

---

## Environment variables (.env)

| Key | Description | Notes |
|---|---|---|
| `MONGO_URI` | MongoDB Atlas connection string | **secret** |
| `MONGO_DB_NAME` / `UPLOADED_DB_NAME` | main DB / uploaded-files DB | `iug_chatbot` / `uploaded_files` |
| `CHAT_API_URL` / `CHAT_API_KEY` / `CHAT_API_MODEL` | chat provider (OpenAI-compatible) | OpenRouter, e.g. `openai/gpt-oss-120b` |
| `EMBED_API_URL` / `EMBED_API_KEY` / `EMBED_MODEL` | embeddings provider | Jina `jina-embeddings-v3` |
| `JWT_SECRET` | token signing | **secret, ≥32 chars** — production refuses to boot with a weak secret |
| `ADMIN_API_KEY` | key for legacy admin routes | **secret** |
| `ADMIN_BOOTSTRAP_ID` / `_PASSWORD` / `_NAME` | first admin account (created at boot if missing) | remove after first login |
| `API_ENV` | `development` / `production` | production hides `/docs` and tightens security |
| `INDEX_BACKEND` | `disk` / `mongo` | `mongo` for deployment (Render's disk is ephemeral) |
| `SESSION_BACKEND` | `mongo` / `memory` | `mongo` by default |
| `ALLOW_PUBLIC_REGISTRATION` | `true` for trials | `false` once wired to the admission system |
| `LEGACY_UNCATALOGUED_FILES_PUBLIC` | expose files predating the catalog | `true` for trials; `false` after classifying them in the panel |
| `ADMISSION_TABLE_MAX_CHUNKS` | cap for sending the full cutoff table | 150 |
| `RATE_LIMIT_CHAT_PER_MIN` / `RATE_LIMIT_LOGIN_PER_MIN` | rate limits | 30 / 10 |
| `MEMORY_MIN_SIM` | conversation-memory relevance threshold | 0.45 |
| `API_CORS_ORIGINS` | CORS origins | empty when the UI is served by the same server |

---

## Deploying on Render

[`render.yaml`](render.yaml) is a ready Blueprint. Full steps:

1. **Push the repo to GitHub** (without `.env` — it is already in `.gitignore`).
2. In [MongoDB Atlas](https://cloud.mongodb.com): **Network Access ➜ Add IP ➜ `0.0.0.0/0`**
   (Render's free plan has no static IP — protection comes from the other layers:
   a strong Mongo user password + TLS).
3. In [Render](https://dashboard.render.com): **New ➜ Blueprint ➜** pick the repo —
   it reads `render.yaml` and creates the `iug-chatbot` service.
4. **Environment**: fill in the secrets marked `sync: false`
   (`MONGO_URI`, chat & embedding keys, `ADMIN_BOOTSTRAP_ID/PASSWORD`).
   `JWT_SECRET` and `ADMIN_API_KEY` are auto-generated by Render.
5. **Deploy** and wait for the build (~2–4 min). First boot builds/loads the
   vector index from Mongo.
6. Verify: `https://<service>.onrender.com/health` → `status: ok`, then try `/app`.
7. Log in with the bootstrap admin account ➜ **delete `ADMIN_BOOTSTRAP_PASSWORD`
   from the dashboard**.

**Free-plan notes**: the service sleeps after ~15 idle minutes (the first request
after sleep takes ~30–60 s) · 750 h/month · disk is ephemeral — which is why the
index and sessions live in Mongo. Every `git push` to `main` redeploys automatically.

---

## Main API endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | service readiness and stats |
| `POST /api/auth/login` · `/register` · `GET /api/auth/me` | JWT auth (bcrypt) |
| `POST /api/chat` | authenticated chat (identity and role from the token) |
| `POST /api/chat/guest` | guest chat (public files only, IP rate limit; accepts optional browser-side `history`) |
| `POST /api/chat/student` | student chat with their academic record merged in |
| `POST /api/chat/student/stream` | token-by-token streaming for any authenticated role |
| `GET/DELETE /api/sessions/me/history` | the token owner's own history only |
| `GET /api/admin/files` · `POST` · `PATCH …/access` · `DELETE` | file management (admin) |
| `POST /api/admin/files/{id}/process` · `/publish` · `/rollback/{v}` | publish cycle & versions |
| `GET/POST/PATCH /api/admin/employees` | employee accounts |
| `GET /api/admin/audit` | audit log |
| `GET /api/cache/stats` · `POST /api/cache/clear` | cache monitoring (admin) |

All errors share one shape:
`{"success": false, "error": {code, message, details, timestamp, path}}`
with correct status codes (401/403/404/409/422/429/502/503) — never 200 on failure.

---

## A question's journey through the engine

```
student question
 ├─ privacy guard (asking about another student? → instant refusal, no LLM)
 ├─ search-query building (pure string work — zero extra LLM calls):
 │    "my department/college/training" ← + the student's major
 │    vague follow-up ← inherits the previous topic (chained, freshness-gated)
 │    colloquial ← + the official term ("اجل" ← "تأجيل الدراسة")
 ├─ permission filtering: only the asker's role's files enter ranking (structural isolation)
 ├─ hybrid retrieval (semantic + BM25 via RRF) + "newest wins" note on conflicts
 │    admission intent → the full cutoffs table is included, not just top-K
 ├─ semantic memory: only relevant previous turns (stored vectors, safe fallback)
 └─ LLM: literal question + student record + chunks + memory — under anti-hallucination rules
```

## Project layout

```
app/
├── config.py            ← all settings (the only place .env is read)
├── prompts.py           ← Arabic prompt templates (their text IS the bot's behavior)
├── db.py                ← unified MongoDB connection (two logical DBs)
├── chunking.py          ← documents → chunks (+ a "[ملف: name]" header)
├── text_norm.py / lexical.py / retrieval.py ← Arabic normalization + BM25 + RRF merge
├── embeddings.py / index_store.py ← Jina client + index persistence (disk/mongo)
├── query_rewrite.py     ← query expansion: self-reference / context / official terms
├── sessions.py          ← semantic conversation memory (5-turn FIFO with vectors)
├── cache.py / ratelimit.py ← privacy-safe TTL+LRU cache + rate limits
├── llm.py               ← chat client (retries + clear provider-named errors)
├── auth.py / tokens.py / rbac.py ← bcrypt + JWT + roles
├── privacy.py / context_builder.py ← privacy guard + per-role private context
├── file_catalog.py      ← file registry: classifications/roles/owner/versions/legacy adoption
├── admissions.py / audit.py ← structured admission answers + audit log
├── knowledge_base.py / uploaded_files.py / chatbot.py ← overall orchestration
└── api/                 ← FastAPI: schemas/deps/middleware/errors/routers
frontend/                ← lightweight RTL HTML/CSS/JS UIs:
                           index (login/register) · chat · admin · employee
eval/run_eval.py         ← golden eval (run it after any prompt change)
tests/                   ← 316 tests (security, roles, memory, retrieval, API…)
docs/                    ← improvement and test reports
```

## Developer rules

- Only `config.py` reads `.env` — no `os.getenv` anywhere else.
- The text of `prompts.py` is behavior, not detail: any edit ⇒ run
  `eval/run_eval.py` and compare.
- Editing `chunking.py` requires reindexing (the index fingerprint handles it
  automatically).
- Students'/employees' personal data is never indexed into RAG
  (`_ALWAYS_EXCLUDE_FROM_RAG`) — it reaches the model only as private context
  for its authenticated owner.
- Test accounts on the live DB: create them, then **delete them** (auth + session).
