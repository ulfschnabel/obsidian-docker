"""Obsidian MCP: vault tools over the LiveSync CouchDB database, plus OAuth for remote clients.

CouchDB is the vault's only writable copy (openspec change couchdb-source-of-truth).
Tool logic lives in vault_tools.py and semantic_index.py; this module wires
them to FastMCP, the embedding model, Chroma, and two change-feed followers:
the note catalog (rebuilt in memory at every start) and the semantic index
(persisted, with a checkpoint in STATE_DIR).
"""
import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import chromadb
import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sentence_transformers import SentenceTransformer
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

from semantic_index import COLLECTION, SemanticIndex, model_embedder
from vault_tools import Catalog, VaultTools
from vaultstore.errors import IncompatibleVault, VaultError
from vaultstore.follower import CheckpointFile, Follower
from vaultstore.store import Store

COUCHDB_URL = os.environ["COUCHDB_URL"]  # the obsidian database, e.g. http://obsidian-couchdb:5984/obsidian
COUCHDB_USER = os.environ["COUCHDB_USER"]  # a member of the database, not a server admin
COUCHDB_PASSWORD = os.environ["COUCHDB_PASSWORD"]
STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CHROMA_HOST = os.getenv("CHROMA_HOST", "obsidian-chroma")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "")
PORT = int(os.getenv("PORT", "8000"))
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
AUTHORIZE_PASSWORD = os.getenv("AUTHORIZE_PASSWORD", "")
_CLAUDE_CLIENT_ID = "d7251a335098f456c042c6a3d96146d9"
_SERVER_NAME = "Obsidian MCP"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request, every long-poll
log = logging.getLogger("obsidian-mcp")


def couch_client() -> httpx.Client:
    return httpx.Client(base_url=COUCHDB_URL, auth=(COUCHDB_USER, COUCHDB_PASSWORD), timeout=60)


log.info("Loading model...")
model = SentenceTransformer("all-MiniLM-L6-v2")
log.info("Connecting to ChromaDB...")
collection = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT).get_or_create_collection(COLLECTION)

catalog = Catalog()
catalog_follower = Follower(couch_client(), catalog)
index = SemanticIndex(collection, model_embedder(model))
index_follower = Follower(couch_client(), index, CheckpointFile(STATE_DIR / "semantic-index.json"))
tools = VaultTools(Store(couch_client()), catalog, catalog_follower=catalog_follower, index_follower=index_follower)

mcp = FastMCP(
    "obsidian-mcp",
    instructions=(
        "Tools for an Obsidian vault that also syncs to the user's phone and other devices. "
        "Paths are vault-relative; '.md' is optional for notes. read_note returns a `revision`: "
        "overwriting, deleting, moving or renaming a note requires it as `expected_revision`, so an "
        "edit made elsewhere since you read the note is never silently overwritten (you get CONFLICT; "
        "read again and redo your change). To add to a note or change one passage, use append_to_note "
        "or replace_in_note, which need no revision. Errors start with a code such as EXISTS, CONFLICT, "
        "NOT_FOUND, NO_MATCH, AMBIGUOUS_MATCH, INVALID_PATH or STORE_UNAVAILABLE."
    ),
)


def _run(method, *args, **kwargs):
    """Call a VaultTools method; a VaultError becomes a tool error whose text starts with its code."""
    try:
        return method(*args, **kwargs)
    except VaultError as e:
        raise ToolError(str(e)) from None


class _AnswersAreNotFaults(logging.Filter):
    """FastMCP logs every tool error with a traceback. A VaultError (EXISTS, CONFLICT, ...) is an
    answer to the agent, not a server fault: log it as one INFO line so real faults stand out."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, ToolError) and isinstance(exc.__context__, VaultError):
            record.msg, record.args, record.exc_info = f"{record.getMessage()}: {exc}", (), None
            record.levelno, record.levelname = logging.INFO, "INFO"
        return True


logging.getLogger("fastmcp.server.server").addFilter(_AnswersAreNotFaults())


# --- Tools ---

@mcp.tool()
def list_notes(folder: str = "") -> list[str]:
    """List the vault's markdown notes as vault-relative paths, optionally only those under `folder`
    (letter case is ignored)."""
    return _run(tools.list_notes, folder)


@mcp.tool()
def read_note(path: str) -> dict:
    """Read a note. Returns {path, content, revision, mtime, conflicted}.

    Keep `revision`: write_note (to overwrite), delete_note, move_note and rename_note require it as
    `expected_revision`. `conflicted` is true when devices hold unresolved conflicting versions.
    The path is vault-relative; '.md' is optional and letter case is ignored.
    """
    return _run(tools.read_note, path)


@mcp.tool()
def write_note(path: str, content: str, expected_revision: str | None = None) -> dict:
    """Create a note, or overwrite one you have read. Returns {path, revision, status}.

    To create, omit expected_revision: it fails with EXISTS if the note already exists.
    To overwrite, pass the `revision` from read_note: it fails with CONFLICT if the note changed
    since (for example, edited on a phone); read it again and redo your change on the new content.
    To add text or change one passage, prefer append_to_note or replace_in_note: they need no revision.
    """
    return _run(tools.write_note, path, content, expected_revision)


@mcp.tool()
def append_to_note(path: str, text: str) -> dict:
    """Append `text` to the end of a note, creating the note if it does not exist.

    The text is added exactly as given: start it with a newline if the note may not end with one.
    Safe against edits made elsewhere at the same time; needs no revision.
    """
    return _run(tools.append_to_note, path, text)


@mcp.tool()
def replace_in_note(path: str, old: str, new: str) -> dict:
    """Replace one exact passage of a note: `old` must occur exactly once.

    Fails with NO_MATCH if `old` does not occur and AMBIGUOUS_MATCH if it occurs more than once
    (include more surrounding text to make it unique). Safe against edits made elsewhere at the
    same time; needs no revision.
    """
    return _run(tools.replace_in_note, path, old, new)


@mcp.tool()
def delete_note(path: str, expected_revision: str) -> dict:
    """Delete a note. Requires the `revision` from read_note; fails with CONFLICT if the note
    changed since you read it."""
    return _run(tools.delete_note, path, expected_revision)


@mcp.tool()
def move_note(src: str, dst: str, expected_revision: str) -> dict:
    """Move a note to a new vault-relative path and rewrite [[wikilinks]] that point to it.

    Requires the source's `revision` from read_note. Fails with EXISTS if a note is already at `dst`.
    The destination is created before the source is deleted, so content is never lost; the result
    lists the steps completed and any notes whose links could not be updated.
    """
    return _run(tools.move_note, src, dst, expected_revision)


@mcp.tool()
def rename_note(path: str, new_name: str, expected_revision: str) -> dict:
    """Rename a note within its folder and rewrite [[wikilinks]] that point to it.

    `new_name` is a name, not a path ('.md' optional). Requires the note's `revision` from read_note.
    """
    return _run(tools.rename_note, path, new_name, expected_revision)


@mcp.tool()
def move_folder(src: str, dst: str) -> dict:
    """Move every note and attachment under folder `src` to `dst`, rewriting [[wikilinks]].

    Reports each document's outcome; a document that cannot move (for example, EXISTS at its
    destination) stays where it is.
    """
    return _run(tools.move_folder, src, dst)


@mcp.tool()
def rename_folder(path: str, new_name: str) -> dict:
    """Rename a folder in place (same parent), rewriting [[wikilinks]]. `new_name` is a name, not a path."""
    return _run(tools.rename_folder, path, new_name)


@mcp.tool()
def search_notes(query: str, max_results: int = 10) -> list[dict]:
    """Case-insensitive keyword search. Returns matching notes with an excerpt around the first match."""
    return _run(tools.search_notes, query, max_results)


@mcp.tool()
def semantic_search(query: str, n_results: int = 5) -> list[dict]:
    """Semantic similarity search across all notes using vector embeddings (at most 10 results)."""
    return _run(index.search, query, n_results)


@mcp.tool()
def get_backlinks(path: str) -> list[str]:
    """Find notes that link to the given note by name: [[Name]] or [[Name|alias]]."""
    return _run(tools.get_backlinks, path)


@mcp.tool()
def get_tags() -> list[str]:
    """Return all unique #tags used across the vault (tags inside code are ignored)."""
    return _run(tools.get_tags)


@mcp.tool()
def vault_status() -> dict:
    """Report the vault connection's health: whether writes are allowed (and why not), whether the
    note catalog has loaded, the change-feed positions, and notes still waiting for data from a device."""
    return _run(tools.vault_status)


def _follow(name: str, follower: Follower) -> None:
    """Run a follower in the background. A vault it cannot read is retried (vault_status says why);
    any other failure exits the process so the container restarts from a clean state."""

    def target() -> None:
        while True:
            try:
                follower.run(threading.Event())
                return
            except IncompatibleVault as e:
                log.error("%s follower: %s; retrying in 60s", name, e)
                time.sleep(60)
            except Exception:
                log.exception("%s follower failed; exiting so the container restarts", name)
                os._exit(1)

    threading.Thread(target=target, name=f"{name}-follower", daemon=True).start()


# --- OAuth 2.1 PKCE ---

_pending: dict[str, dict] = {}
_auth_codes: dict[str, dict] = {}
_access_tokens: dict[str, dict] = {}


def _rand(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def _sha256b64url(s: str) -> str:
    digest = hashlib.sha256(s.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _safe_compare(a: str, b: str) -> bool:
    key = secrets.token_bytes(32)
    ha = hmac.new(key, a.encode(), "sha256").digest()
    hb = hmac.new(key, b.encode(), "sha256").digest()
    return hmac.compare_digest(ha, hb)


def _ms() -> int:
    return int(time.time() * 1000)


def _validate_oauth_token(token: str) -> bool:
    t = _access_tokens.get(token)
    if not t:
        return False
    if t["expires_at"] < _ms():
        del _access_tokens[token]
        return False
    return True


async def _well_known_resource(request: Request):
    issuer = BASE_URL or str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "resource": f"{issuer}/",
            "authorization_servers": [issuer],
            "scopes_supported": ["mcp"],
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "no-cache"},
    )


async def _well_known_auth_server(request: Request):
    issuer = BASE_URL or str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "registration_endpoint": f"{issuer}/register",
            "scopes_supported": ["mcp"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
        },
        headers={"Cache-Control": "no-cache"},
    )


async def _register(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(
        {
            "client_id": _CLAUDE_CLIENT_ID,
            "client_name": body.get("client_name", "Claude"),
            "redirect_uris": body.get("redirect_uris", []),
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


async def _authorize(request: Request):
    issuer = BASE_URL or str(request.base_url).rstrip("/")
    q = dict(request.query_params)
    if q.get("response_type") != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if q.get("code_challenge_method") != "S256":
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Only S256 is supported"}, status_code=400
        )
    nonce = _rand(16)
    _pending[nonce] = {
        "client_id": q.get("client_id", _CLAUDE_CLIENT_ID),
        "redirect_uri": q.get("redirect_uri", ""),
        "state": q.get("state"),
        "scopes": q.get("scope", "mcp").split(),
        "code_challenge": q.get("code_challenge", ""),
        "expires": _ms() + 300_000,
    }
    return RedirectResponse(f"{issuer}/consent?nonce={nonce}", status_code=302)


async def _consent(request: Request):
    issuer = BASE_URL or str(request.base_url).rstrip("/")
    nonce = request.query_params.get("nonce", "")
    if nonce not in _pending:
        return HTMLResponse("<h2>Invalid or expired request</h2>", status_code=400)
    return HTMLResponse(_consent_html(issuer, _SERVER_NAME, nonce))


async def _consent_submit(request: Request):
    issuer = BASE_URL or str(request.base_url).rstrip("/")
    form = await request.form()
    nonce = form.get("nonce", "")
    password = form.get("password", "")
    p = _pending.get(nonce)
    if not p or p["expires"] < _ms():
        return HTMLResponse("<h2>Authorization request expired</h2>", status_code=400)

    if not _safe_compare(str(password), AUTHORIZE_PASSWORD):
        new_nonce = _rand(16)
        _pending[new_nonce] = {**p, "expires": _ms() + 300_000}
        del _pending[nonce]
        return HTMLResponse(_consent_html(issuer, _SERVER_NAME, new_nonce, "Incorrect password"), status_code=401)

    del _pending[nonce]
    code = _rand(32)
    _auth_codes[code] = {
        "client_id": p["client_id"],
        "redirect_uri": p["redirect_uri"],
        "scopes": p["scopes"],
        "code_challenge": p["code_challenge"],
        "expires_at": _ms() + 300_000,
    }
    params = urlencode({"code": code, **({"state": p["state"]} if p["state"] else {})})
    return RedirectResponse(f"{p['redirect_uri']}?{params}", status_code=302)


async def _token(request: Request):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        form = await request.form()
        body = dict(form)

    grant_type = body.get("grant_type", "")
    code = body.get("code", "")
    code_verifier = body.get("code_verifier", "")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    ac = _auth_codes.get(code)
    if not ac or ac["expires_at"] < _ms():
        _auth_codes.pop(code, None)
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if _sha256b64url(str(code_verifier)) != ac["code_challenge"]:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400
        )

    del _auth_codes[code]
    token = _rand(32)
    expires_at = _ms() + 2_592_000_000
    _access_tokens[token] = {"client_id": ac["client_id"], "scopes": ac["scopes"], "expires_at": expires_at}

    return JSONResponse(
        {"access_token": token, "token_type": "Bearer", "expires_in": 2592000, "scope": " ".join(ac["scopes"])}
    )


def _consent_html(issuer: str, name: str, nonce: str, error: str = "") -> str:
    err_html = f'<p style="color:red">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html><head><title>Authorize {name}</title>
<style>body{{font-family:sans-serif;max-width:420px;margin:120px auto;text-align:center;color:#333}}
input{{padding:10px;font-size:16px;border:1px solid #ccc;border-radius:6px;width:100%;box-sizing:border-box;margin:8px 0}}
button{{padding:12px 28px;font-size:16px;background:#5865f2;color:#fff;border:none;border-radius:6px;cursor:pointer;margin-top:8px}}</style>
</head><body>
<h2>Authorize Claude</h2>
<p>Allow Claude to access <strong>{name}</strong>?</p>
<form method="post" action="{issuer}/consent/submit">
  <input type="hidden" name="nonce" value="{nonce}">
  <input type="password" name="password" placeholder="Password" required autofocus>
  <button type="submit">Authorize</button>
  {err_html}
</form>
</body></html>"""


# --- Auth middleware ---

_OAUTH_PATHS = {
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/register",
    "/authorize",
    "/consent",
    "/consent/submit",
    "/token",
}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _OAUTH_PATHS:
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if BEARER_TOKEN and token == BEARER_TOKEN:
            return await call_next(request)
        if _validate_oauth_token(token):
            return await call_next(request)
        if not BEARER_TOKEN and not AUTHORIZE_PASSWORD:
            return await call_next(request)
        issuer = BASE_URL or str(request.base_url).rstrip("/")
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer error="invalid_token", error_description="Authentication required",'
                    f' resource_metadata="{issuer}/.well-known/oauth-protected-resource"'
                )
            },
        )


if __name__ == "__main__":
    _follow("catalog", catalog_follower)
    _follow("semantic-index", index_follower)

    mcp_asgi = mcp.http_app(path="/mcp")

    oauth_routes = [
        Route("/.well-known/oauth-protected-resource", _well_known_resource),
        Route("/.well-known/oauth-authorization-server", _well_known_auth_server),
        Route("/register", _register, methods=["POST"]),
        Route("/authorize", _authorize),
        Route("/consent", _consent),
        Route("/consent/submit", _consent_submit, methods=["POST"]),
        Route("/token", _token, methods=["POST"]),
    ]

    app = Starlette(routes=oauth_routes + [Mount("/", app=mcp_asgi)], lifespan=mcp_asgi.lifespan)
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
