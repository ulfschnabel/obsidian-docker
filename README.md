# obsidian-docker

A self-hosted Obsidian stack with CouchDB sync, a semantic MCP server, and automatic reMarkable sync.

## Services

| Service | Image / Build | Purpose |
|---|---|---|
| `obsidian-couchdb` | `couchdb:3` | Obsidian LiveSync backend |
| `obsidian-headless` | `./obsidian-headless` | Obsidian running headless (Xvfb) for plugin init |
| `obsidian-chroma` | `chromadb/chroma` | Vector store for semantic search |
| `obsidian-mcp` | `./obsidian-mcp` | MCP server — semantic vault search for Claude |
| `obsidian-remarkable-sync` | `./obsidian-remarkable-sync` | Watches vault, converts notes to PDF, pushes to reMarkable |

## Quick start

```bash
cp .env.example .env
# Fill in .env — see comments in the file

# Build the patched rmapi binary first (see obsidian-remarkable-sync/README.md)

docker compose up -d
```

## Directory layout

```
data/               # created at runtime, not in git
  vault/            # your Obsidian markdown files
  couchdb/          # CouchDB data
  chroma/           # ChromaDB embeddings
  obsidian-config/  # Obsidian Electron config
  rmapi-config/     # reMarkable auth tokens
  sync-state/       # manifest tracking which notes are synced
```

## Obsidian LiveSync setup

1. Start `obsidian-couchdb` and `obsidian-headless`.
2. Connect via VNC (`localhost:5901`, password `obsidian`) for initial plugin configuration.
3. Install the [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync) plugin and point it at `http://localhost:8796` (or `https://couchdb.yourhost`) with your CouchDB credentials.
4. Set `VNC_ENABLED=false` in the headless service once setup is done.

## reMarkable sync

See [obsidian-remarkable-sync/README.md](obsidian-remarkable-sync/README.md) for details on the sync logic and the required `rmapi` binary.
