# Shared Document Merger

A small single-workspace document organizer. The frontend is plain HTML, CSS, and JavaScript and can be hosted on GitHub Pages. The Python API uses FastAPI and is intended to run in Docker.

## Features

- Upload PDF, PNG, JPEG/JPG, and HEIC/HEIF files in any order.
- Reorder uploaded documents using drag-and-drop or the Move up / Move down controls.
- Merge the current order into one PDF.
- Uploaded source files expire after 1 hour.
- Generated merged PDFs expire after 1 day.
- A shared workspace: every visitor to the same API sees the same document list.
- A 1 GiB storage limit across source uploads and generated PDFs.
- Docker volume persistence across container restarts.

## Important: shared and unauthenticated workspace

This is deliberately a **single global workspace**, not a per-user application. Anyone who can reach the API can see, upload, reorder, delete, and merge the shared documents. Do not expose it publicly with sensitive documents unless you put an authentication/access-control layer in front of it (for example, a VPN, Cloudflare Access, or an authenticated reverse proxy). CORS is not authentication.

The API does not try to identify visitors. All browsers connected to the same API share the same workspace.

## Run locally with Docker

From this directory:

```bash
docker compose up --build -d
```

API and interactive API docs:

- `http://localhost:8000`
- `http://localhost:8000/docs`

The frontend can be opened directly as `frontend/index.html`, but set its API URL first (see below). Or serve it locally:

```bash
cd frontend
python -m http.server 5500
```

Then open `http://localhost:5500`.

## Deploy the frontend to GitHub Pages

1. Push the contents of `frontend/` to your GitHub Pages branch/repository (or configure Pages to publish that folder).
2. Edit `frontend/config.js` and set `API_BASE_URL` to the public HTTPS base URL of your deployed backend, with no trailing slash. Example:

   ```js
   window.APP_CONFIG = { API_BASE_URL: "https://documents-api.example.com" };
   ```

3. Configure the backend's `CORS_ORIGINS` environment variable to include the exact GitHub Pages origin, e.g. `https://yourname.github.io`. For a project site, the origin is still `https://yourname.github.io` (not the repository path).
4. Ensure the backend is reachable over HTTPS. GitHub Pages is HTTPS, so browsers will block calls to an HTTP API.

## Configuration

Set these in `docker-compose.yml` or the container environment:

| Variable | Default | Meaning |
|---|---:|---|
| `DATA_DIR` | `/data` | Persistent storage directory |
| `MAX_STORAGE_BYTES` | `1073741824` | Total stored bytes allowed (1 GiB) |
| `UPLOAD_TTL_SECONDS` | `3600` | Source upload lifetime (1 hour) |
| `MERGED_TTL_SECONDS` | `86400` | Merged PDF lifetime (1 day) |
| `CORS_ORIGINS` | `http://localhost:5500,http://127.0.0.1:5500` | Comma-separated allowed origins |

The storage limit counts source files and generated merged PDFs. Temporary merge files are written under the data directory and removed after processing. A merge can temporarily require additional disk space while the output is being generated; leave some free disk space on the host.

## API overview

- `GET /api/state` — list current uploads, merged outputs, and storage usage.
- `POST /api/documents` — multipart upload; field name `files` (repeat for multiple files).
- `PUT /api/documents/order` — JSON body: `{"document_ids":["id-1","id-2"]}`.
- `DELETE /api/documents/{document_id}` — delete one source upload.
- `POST /api/merge` — create a merged PDF in the current order.
- `GET /api/merged/{merged_id}` — download a generated PDF.
- `DELETE /api/merged/{merged_id}` — delete a generated PDF.
- `GET /health` — health check.

## Notes / limitations

- Supported input extensions: `.pdf`, `.png`, `.jpg`, `.jpeg`, `.heic`, `.heif`.
- HEIC conversion is supported through `pillow-heif`; unusual or damaged image files may still fail conversion.
- Images are placed on PDF pages using their original pixel dimensions at approximately 150 DPI, with a maximum page dimension of 2000 points to avoid creating extremely large pages.
- PDF page content is preserved by PyMuPDF. Password-protected PDFs are not supported.
- The 1 GiB limit is a storage quota, not a maximum individual upload size.
- Expiration cleanup runs every 5 minutes and also runs when the API starts. Expired files may therefore remain on disk for up to about 5 minutes beyond their nominal expiry.
