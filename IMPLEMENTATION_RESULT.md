# Adaptive RAG v2 — Implementation Result

- Unified prompt builder: implemented.
- ConversationFrame and QueryPlan: implemented without an extra LLM call.
- Structured evidence routing: implemented; it never returns the final answer.
- EvidenceContract and one bounded missing-field retry: implemented.
- Contextual parent/child chunking: implemented.
- Score-preserving Dense + BM25 + RRF candidates: implemented.
- Selective reranker with timeout, fail-open, and circuit breaker: implemented.
- Trusted facts, structured admission, privacy refusals, missing-file paths, blocking and streaming paths: all reach the LLM.
- Final-answer cache: disabled by default; embedding cache remains available.

Test command:

```bash
pytest -q
```

Result:

```text
411 passed, 101 warnings
```

The warnings are PyJWT test warnings caused by intentionally short test secrets; production already has a secure-secret assertion.

The live 408-question API evaluation was not executed in this implementation run, to avoid consuming the configured external LLM/API account. Run it separately with final-answer caching disabled, then evaluate cache/performance in a second pass.
