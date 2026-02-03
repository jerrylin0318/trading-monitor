"""Startup script for Render deployment."""
import os
import uvicorn

port = int(os.environ.get("PORT", "10000"))
uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
