# -*- coding: utf-8 -*-
"""Export the exact public uploaded-file chunks visible to a guest bot."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import file_catalog  # noqa: E402
from app.chatbot import IUGChatbot  # noqa: E402
from app.rbac import Principal  # noqa: E402
from app.sessions import SessionStore  # noqa: E402


OUTPUT = (
    ROOT
    / "eval"
    / "retest_440_detailed_2026-07-18"
    / "live_public_bot_corpus.json"
)


def main() -> int:
    bot = IUGChatbot(sessions=SessionStore())
    bot.initialize()
    runtime = bot.get_uploaded_files_list()
    available = {item["collection"] for item in runtime}
    allowed = file_catalog.allowed_collections(Principal.guest(), available)
    collections = []
    total_chunks = 0
    unique_hashes: set[str] = set()
    for name in sorted(allowed):
        chunks = list(bot._uploaded.chunks_of(name))  # evaluation snapshot only
        total_chunks += len(chunks)
        for chunk in chunks:
            unique_hashes.add(
                hashlib.sha256(chunk.encode("utf-8")).hexdigest()
            )
        collections.append(
            {
                "collection": name,
                "chunk_count": len(chunks),
                "chunks": chunks,
            }
        )
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "available_collection_count": len(available),
        "allowed_public_collection_count": len(allowed),
        "total_chunks": total_chunks,
        "unique_exact_chunk_count": len(unique_hashes),
        "collections": collections,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                key: payload[key]
                for key in [
                    "exported_at",
                    "available_collection_count",
                    "allowed_public_collection_count",
                    "total_chunks",
                    "unique_exact_chunk_count",
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    for collection in collections:
        print(f"{collection['collection']}: {collection['chunk_count']}")
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
