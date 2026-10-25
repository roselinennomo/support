"""API HTTP du conteneur navigateur (appelée uniquement par le backend, avec un jeton partagé)."""

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.browser import KBBrowser, NavigationFailed, SelectorFailed, SessionExpired
from app.config import Config

logging.basicConfig(level=logging.INFO)
config = Config()
browser = KBBrowser(config)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not config.token:
        browser.logbook.add("warning", "no_token", message="KB_BROWSER_TOKEN est vide : API non protégée.")
    yield
    await browser.close()


app = FastAPI(title="KB browser", lifespan=lifespan)


def require_token(x_kb_token: str = Header(default="")) -> None:
    if config.token and not hmac.compare_digest(x_kb_token, config.token):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Jeton invalide."})


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    limit: int = Field(default=3, ge=1, le=10)


class SnapshotRequest(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    input_selector: str = Field(default="", max_length=500)
    submit_selector: str = Field(default="", max_length=500)
    probe: str = Field(default="", max_length=200)
    open_result: int = Field(default=-1, ge=-1, le=20)


class ArticleRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class DocumentFetchRequest(ArticleRequest):
    include_file: bool = False  # renvoyer aussi le fichier original (base64) pour l'archive locale


async def _guard(operation):
    """Traduit les erreurs de navigation en réponses HTTP structurées."""
    try:
        return await operation
    except SessionExpired as exc:
        raise HTTPException(status_code=409, detail={"code": "session_expired", "message": str(exc)}) from exc
    except SelectorFailed as exc:
        raise HTTPException(status_code=422, detail={
            "code": "selector_failed", "message": str(exc), "selector": exc.selector, "url": exc.url,
        }) from exc
    except NavigationFailed as exc:
        raise HTTPException(status_code=422, detail={
            "code": "navigation_failed", "message": str(exc), "url": exc.url,
        }) from exc
    except HTTPException:
        raise
    except Exception as exc:
        browser.logbook.add("error", "unexpected", message=f"{type(exc).__name__}: {exc}"[:300])
        raise HTTPException(status_code=503, detail={"code": "unavailable", "message": str(exc)[:300]}) from exc


@app.get("/health")
async def health():
    return {"status": "ok", "browser": "launched" if browser.launched else "idle"}


@app.get("/session", dependencies=[Depends(require_token)])
async def session():
    return {"state": await _guard(browser.session_state())}


@app.post("/search", dependencies=[Depends(require_token)])
async def search(payload: SearchRequest):
    return {"hits": await _guard(browser.search(payload.query.strip(), payload.limit))}


@app.post("/article", dependencies=[Depends(require_token)])
async def article(payload: ArticleRequest):
    return await _guard(browser.fetch_article(payload.url.strip()))


@app.get("/logs", dependencies=[Depends(require_token)])
async def logs(limit: int = Query(default=100, ge=1, le=500)):
    return {"entries": browser.logbook.latest(limit)}


@app.post("/snapshot", dependencies=[Depends(require_token)])
async def snapshot(payload: SnapshotRequest):
    """Diagnostic des sélecteurs : décrit la page de résultats d'une recherche (voir GET /snapshot.png)."""
    return await _guard(browser.snapshot(
        payload.query.strip(), payload.input_selector, payload.submit_selector, payload.probe, payload.open_result,
    ))


@app.get("/snapshot.png", dependencies=[Depends(require_token)])
async def snapshot_image():
    return FileResponse("/tmp/snapshot.png", media_type="image/png")


class InspectRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    wait_ms: int = Field(default=6000, ge=0, le=20000)


@app.post("/inspect", dependencies=[Depends(require_token)])
async def inspect_page(payload: InspectRequest):
    """Diagnostic : décrit une page du portail sans y effectuer de recherche (voir GET /snapshot.png)."""
    return await _guard(browser.inspect(payload.url.strip(), payload.wait_ms))


class DocumentSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    limit: int = Field(default=3, ge=1, le=10)


@app.post("/documents/search", dependencies=[Depends(require_token)])
async def documents_search(payload: DocumentSearchRequest):
    return {"hits": await _guard(browser.search_documents(payload.query.strip(), payload.limit))}


@app.post("/documents/fetch", dependencies=[Depends(require_token)])
async def documents_fetch(payload: DocumentFetchRequest):
    return await _guard(browser.fetch_document(payload.url.strip(), payload.include_file))
