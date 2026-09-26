"""FastAPI app instance; mounts the webhooks router and starts Slack Bolt + APScheduler.

Wired up fully in Phase D. Left as a structural placeholder in Phase A.
"""

from fastapi import FastAPI

app = FastAPI(title="Nexoryn Social Agent")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
