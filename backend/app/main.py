from fastapi import FastAPI

app = FastAPI(title="astropi")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
