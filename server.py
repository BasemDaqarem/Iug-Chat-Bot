"""
API entrypoint — run the chatbot as a web service:

    python server.py
        (or)
    uvicorn server:app --host 127.0.0.1 --port 8000

Interactive docs (dev): http://127.0.0.1:8000/docs
"""

from app import config
from app.api import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)
