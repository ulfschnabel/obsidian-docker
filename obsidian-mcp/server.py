import os
import re
import threading
import hashlib
import hmac
import secrets
import base64
import time
from pathlib import Path
from urllib.parse import urlencode

import chromadb
import uvicorn
from fastmcp import FastMCP
from sentence_transformers import SentenceTransformer
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, HTMLResponse
from starlette.routing import Route, Mount
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

VAULT_PATH = Path(os.getenv("VAULT_PATH", "/vault"))
CHROMA_HOST = os.getenv("CHROMA_HOST", "obsidian-chroma")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "")
PORT = int(os.getenv("PORT", "8000"))
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
AUTHORIZE_PASSWORD = os.getenv("AUTHORIZE_PASSWORD", "")
_CLAUDE_CLIENT_ID = "d7251a335098f456c042c6a3d96146d9"
_SERVER_NAME = "Obsidian MCP"

print("Loading model...")
model = SentenceTransformer("all-MiniLM-L6-v2")
print("Connecting to ChromaDB...")
chroma_client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
collection = chroma_client.get_or_create_collection("vault")

mcp = FastMCP("obsidian-mcp")


def note_id(path: Path) -> str:
    return hashlib.md5(str(path).encode()).hexdigest()


def to_relative(path: Path) -> str:
    return str(path.relative_to(VAULT_PATH))


def resolve_path(rel_path: str) -> Path:
    p = VAULT_PATH / rel_path
    if not rel_path.endswith(".md"):
        p = p.with_suffix(".md")
    return p


# --- Indexing ---

def index_file(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            return
        embedding = model.encode(text[:8000]).tolist()
        collection.upsert(
            ids=[note_id(path)],
            embeddings=[embedding],
            documents=[text[:2000]],
            metadatas=[{"path": to_relative(path), "filename": path.name}],
        )
    except Exception as e:
        print(f"Index error {path}: {e}")


def remove_from_index(path: Path):
    try:
        collection.delete(ids=[note_id(path)])
    except Exception:
        pass


def index_vault():
    files = list(VAULT_PATH.rglob("*.md"))
    print(f"Indexing {len(files)} notes...")
    for f in files:
        index_file(f)
    print("Indexing complete.")


class VaultWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            index_file(Path(event.src_path))

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            index_file(Path(event.src_path))

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            remove_from_index(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            if event.src_path.endswith(".md"):
                remove_from_index(Path(event.src_path))
            if event.dest_path.endswith(".md"):
                index_file(Path(event.dest_path))


# --- Tools ---

@mcp.tool()
def list_notes(folder: str = "") -> list[str]:
    """List all notes in the vault, optionally filtered to a subfolder."""
    base = VAULT_PATH / folder if folder else VAULT_PATH
    return [to_relative(p) for p in sorted(base.rglob("*.md"))]


@mcp.tool()
def read_note(path: str) -> str:
    """Read the full content of a note by its vault-relative path."""
    p = resolve_path(path)
    if not p.exists():
        return f"Note not found: {path}"
    return p.read_text(encoding="utf-8", errors="ignore")


@mcp.tool()
def write_note(path: str, content: str) -> str:
    """Write (create or overwrite) a note. Path is vault-relative, .md extension optional."""
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    index_file(p)
    return f"Written: {to_relative(p)}"


@mcp.tool()
def delete_note(path: str) -> str:
    """Delete a note by vault-relative path."""
    p = resolve_path(path)
    if not p.exists():
        return f"Not found: {path}"
    remove_from_index(p)
    p.unlink()
    return f"Deleted: {to_relative(p)}"


def _rewrite_wikilinks(moves: list[tuple[Path, Path]]) -> int:
    """Rewrite [[wikilinks]] across the vault for a batch of moves.
    moves is a list of (old_path, new_path) pairs. Returns number of files changed."""
    if not moves:
        return 0

    # Build substitution rules: for each move, handle stem-only and path-qualified links
    rules: list[tuple[re.Pattern, str]] = []
    for old, new in moves:
        old_stem = re.escape(old.stem)
        old_rel = re.escape(to_relative(old).removesuffix(".md"))
        new_rel = to_relative(new).removesuffix(".md")
        new_stem = new.stem
        # Path-qualified links (more specific — must come first)
        rules.append((
            re.compile(r'\[\[' + old_rel + r'(\|[^\]]*)?]]', re.IGNORECASE),
            lambda m, nr=new_rel: f'[[{nr}{m.group(1) or ""}]]'
        ))
        # Stem-only links (only when stem changes)
        if old.stem.lower() != new.stem.lower():
            rules.append((
                re.compile(r'\[\[' + old_stem + r'(\|[^\]]*)?]]', re.IGNORECASE),
                lambda m, ns=new_stem: f'[[{ns}{m.group(1) or ""}]]'
            ))

    new_paths = {new for _, new in moves}
    changed = 0
    for p in VAULT_PATH.rglob("*.md"):
        if p in new_paths:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            result = text
            for pattern, repl in rules:
                result = pattern.sub(repl, result)
            if result != text:
                p.write_text(result, encoding="utf-8")
                index_file(p)
                changed += 1
        except Exception:
            pass
    return changed


def _move_note_internal(src: Path, dst: Path) -> str:
    if not src.exists():
        return f"Not found: {to_relative(src)}"
    if dst.exists():
        return f"Destination already exists: {to_relative(dst)}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
    updated = _rewrite_wikilinks([(src, dst)])
    remove_from_index(src)
    src.unlink()
    index_file(dst)
    msg = f"Moved: {to_relative(src)} → {to_relative(dst)}"
    if updated:
        msg += f" (updated links in {updated} note{'s' if updated != 1 else ''})"
    return msg


@mcp.tool()
def move_note(src: str, dst: str) -> str:
    """Move a note to a new vault-relative path, rewriting all [[wikilinks]] that point to it."""
    return _move_note_internal(resolve_path(src), resolve_path(dst))


@mcp.tool()
def rename_note(path: str, new_name: str) -> str:
    """Rename a note in place (same folder), rewriting all [[wikilinks]] that point to it."""
    src = resolve_path(path)
    if not new_name.endswith(".md"):
        new_name += ".md"
    return _move_note_internal(src, src.parent / new_name)


@mcp.tool()
def move_folder(src: str, dst: str) -> str:
    """Move an entire folder of notes to a new location, rewriting all [[wikilinks]]."""
    src_dir = (VAULT_PATH / src).resolve()
    dst_dir = (VAULT_PATH / dst).resolve()
    if not src_dir.is_dir():
        return f"Folder not found: {src}"
    notes = list(src_dir.rglob("*.md"))
    if not notes:
        return f"No notes in: {src}"

    moves = []
    for note in notes:
        rel = note.relative_to(src_dir)
        new_path = dst_dir / rel
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(note.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        moves.append((note, new_path))

    updated = _rewrite_wikilinks(moves)

    for old, _ in moves:
        remove_from_index(old)
        old.unlink()
    for _, new in moves:
        index_file(new)

    # Remove now-empty source dirs
    for d in sorted(src_dir.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    try:
        src_dir.rmdir()
    except OSError:
        pass

    msg = f"Moved {len(moves)} note{'s' if len(moves) != 1 else ''}: {src} → {dst}"
    if updated:
        msg += f" (updated links in {updated} note{'s' if updated != 1 else ''})"
    return msg


@mcp.tool()
def rename_folder(path: str, new_name: str) -> str:
    """Rename a folder in place (same parent), rewriting all [[wikilinks]]."""
    src_dir = VAULT_PATH / path
    dst_dir = src_dir.parent / new_name
    return move_folder(
        str(src_dir.relative_to(VAULT_PATH)),
        str(dst_dir.relative_to(VAULT_PATH)),
    )


@mcp.tool()
def search_notes(query: str, max_results: int = 10) -> list[dict]:
    """Keyword search across all notes. Returns matching notes with context excerpts."""
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []
    for p in VAULT_PATH.rglob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            m = pattern.search(text)
            if m:
                start = max(0, m.start() - 100)
                excerpt = text[start:start + 300].strip()
                results.append({"path": to_relative(p), "excerpt": excerpt})
                if len(results) >= max_results:
                    break
        except Exception:
            pass
    return results


@mcp.tool()
def semantic_search(query: str, n_results: int = 5) -> list[dict]:
    """Semantic similarity search across all notes using vector embeddings."""
    embedding = model.encode(query).tolist()
    results = collection.query(query_embeddings=[embedding], n_results=min(n_results, 10))
    if not results["documents"] or not results["documents"][0]:
        return []
    return [
        {"path": m["path"], "filename": m["filename"], "excerpt": d[:500], "score": round(1 - s, 3)}
        for d, m, s in zip(results["documents"][0], results["metadatas"][0], results["distances"][0])
    ]


@mcp.tool()
def get_backlinks(path: str) -> list[str]:
    """Find all notes that contain a [[wikilink]] pointing to the given note."""
    target = Path(resolve_path(path)).stem
    pattern = re.compile(r'\[\[' + re.escape(target) + r'(\|[^\]]+)?\]\]', re.IGNORECASE)
    return [
        to_relative(p)
        for p in VAULT_PATH.rglob("*.md")
        if p.stem != target and pattern.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]


@mcp.tool()
def get_tags() -> list[str]:
    """Return all unique #tags used across the vault."""
    tag_pattern = re.compile(r'(?<![`\w])#([a-zA-Z][a-zA-Z0-9/_-]*)')
    tags: set[str] = set()
    for p in VAULT_PATH.rglob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
            text = re.sub(r'`[^`]*`', '', text)
            tags.update(tag_pattern.findall(text))
        except Exception:
            pass
    return sorted(tags)


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
    threading.Thread(target=index_vault, daemon=True).start()
    observer = Observer()
    observer.schedule(VaultWatcher(), str(VAULT_PATH), recursive=True)
    observer.start()

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
