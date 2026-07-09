"""
API entrypoint — run the chatbot as a web service:

    python server.py
        (or)
    uvicorn server:app --host 0.0.0.0 --port 8000

Interactive docs (dev): http://127.0.0.1:8000/docs
"""

import os

from app import config
from app.api import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    # Hosted platforms (Render/Railway/…) inject $PORT and require binding
    # 0.0.0.0; locally we keep the configured host/port.
    port = int(os.getenv("PORT", config.API_PORT))
    host = "0.0.0.0" if os.getenv("PORT") else config.API_HOST
    uvicorn.run(app, host=host, port=port)
