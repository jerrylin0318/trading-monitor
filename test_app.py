"""Minimal test app."""
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok", "demo_mode": True}

@app.get("/api/status")
def status():
    return {"connected": False, "demo_mode": True}
