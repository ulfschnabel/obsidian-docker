# obsidian-remarkable-sync

Watches the Obsidian vault for changes and pushes updated notes to the reMarkable as PDFs.

## How it works

1. On startup: full vault scan — uploads any notes that are new or changed since the last run.
2. Then: watches the vault with inotify. After 60 seconds of quiet (no `.md` file changes), batches and uploads everything that changed.
3. Deleted notes are removed from reMarkable.

## One-time setup

### 1. Build the patched rmapi binary

The stock [rmapi](https://github.com/juruen/rmapi) sends bare UUIDs in the `rm-filename` header, which the reMarkable cloud API now rejects (HTTP 400). You need to apply two small patches before building:

**`api/sync15/blobdoc.go`** — in `BlobDoc.Mirror`, change:
```go
// before
entryIndex, err := r.GetReader(e.Hash, e.DocumentID)
// after
entryIndex, err := r.GetReader(e.Hash, addExt(e.DocumentID, archive.DocSchemaExt))
```

**`api/sync15/tree.go`** — in `BuildTree`, change:
```go
// before
f, err := provider.GetReader(e.Hash, e.DocumentID)
// after
f, err := provider.GetReader(e.Hash, addExt(e.DocumentID, archive.DocSchemaExt))
```

Then build and copy the binary:
```bash
cd /path/to/rmapi
go build -o rmapi .
cp rmapi /path/to/obsidian-remarkable-sync/rmapi
```

### 2. Authenticate rmapi

Run rmapi once to pair it with your reMarkable account:
```bash
RMAPI_CONFIG=/path/to/data/rmapi-config ./rmapi ls
```

### 3. Start the service

```bash
mkdir -p data/rmapi-config data/sync-state
docker compose up -d --build obsidian-remarkable-sync
```

## CLI flags (for one-off runs)

```bash
# Preview what would be uploaded/skipped without touching anything
docker compose run --rm obsidian-remarkable-sync obsidian-remarkable-sync --dry-run

# Force re-upload everything
docker compose run --rm obsidian-remarkable-sync obsidian-remarkable-sync --force

# Reset sync state (next run re-uploads everything)
docker compose run --rm obsidian-remarkable-sync obsidian-remarkable-sync --reset-manifest
```

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `VAULT_PATH` | `/vault` | Path to the Obsidian vault inside the container |
| `RMAPI_CONFIG` | `/rmapi-config` | Writable rmapi config dir (tokens live here) |
| `MANIFEST_PATH` | `/state/manifest.json` | Sync state file — must be on a persistent volume |
| `REMARKABLE_ROOT` | `Obsidian` | Root folder on reMarkable |
| `DEBOUNCE_SECONDS` | `60` | Quiet period before batch upload fires |
| `PAPER_SIZE` | `a5` | PDF page size (`a5` or `a4`) |
