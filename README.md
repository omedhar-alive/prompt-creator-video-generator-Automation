# AI Video Factory - Minimal FastAPI App

This repo contains a minimal FastAPI app with:

- Health route: `GET /` returning `{ "status": "ok" }`
- Generate route: `POST /generate-video` returning a placeholder JSON response

Run locally:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Test endpoints with `curl` or Postman.
