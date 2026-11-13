from __future__ import annotations

import asyncio
import io
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterable

import fitz
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from pillow_heif import register_heif_opener

register_heif_opener()

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
MERGED_DIR = DATA_DIR / "merged"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "metadata.sqlite3"
MAX_STORAGE_BYTES = int(os.getenv("MAX_STORAGE_BYTES", str(1024**3)))
UPLOAD_TTL_SECONDS = int(os.getenv("UPLOAD_TTL_SECONDS", "3600"))
MERGED_TTL_SECONDS = int(os.getenv("MERGED_TTL_SECONDS", "86400"))
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".heic", ".heif"}
CHUNK_SIZE = 1024 * 1024
workspace_lock = asyncio.Lock()


def connect_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_storage() -> None:
    for folder in (DATA_DIR, UPLOAD_DIR, MERGED_DIR, TMP_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    with connect_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                sort_order INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS merged_files (
                id TEXT PRIMARY KEY,
                stored_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
        """)


def current_storage_bytes() -> int:
    total = 0
    for folder in (UPLOAD_DIR, MERGED_DIR):
        for path in folder.iterdir():
            if path.is_file():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass
    return total


def cleanup_expired() -> None:
    now = time.time()
    with connect_db() as con:
        docs = con.execute("SELECT id, stored_name FROM documents WHERE expires_at <= ?", (now,)).fetchall()
        for row in docs:
            (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
            con.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        merged = con.execute("SELECT id, stored_name FROM merged_files WHERE expires_at <= ?", (now,)).fetchall()
        for row in merged:
            (MERGED_DIR / row["stored_name"]).unlink(missing_ok=True)
            con.execute("DELETE FROM merged_files WHERE id = ?", (row["id"],))
    for path in TMP_DIR.iterdir():
        if path.is_file():
            try:
                if path.stat().st_mtime < now - 3600:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(300)
        try:
            cleanup_expired()
        except Exception:
            # A later cycle retries cleanup; the API remains available.
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_storage()
    cleanup_expired()
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Shared Document Merger API", version="1.0.0", lifespan=lifespan)

origins = [x.strip() for x in os.getenv(
    "CORS_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500"
).split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


class OrderRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1)


def safe_display_name(name: str) -> str:
    # Keep the original name for display only; never use it as a filesystem path.
    return Path(name.replace("\\", "/")).name[:255] or "document"


def verify_file(path: Path, extension: str) -> None:
    try:
        if extension == ".pdf":
            with path.open("rb") as f:
                if f.read(5) != b"%PDF-":
                    raise ValueError("File does not have a valid PDF signature.")
            with fitz.open(path) as doc:
                if doc.needs_pass:
                    raise ValueError("Password-protected PDFs are not supported.")
                if doc.page_count == 0:
                    raise ValueError("The PDF contains no pages.")
        else:
            with Image.open(path) as image:
                image.verify()
    except Exception as exc:
        raise ValueError(str(exc) or "Unsupported or invalid file.") from exc


async def write_upload(upload: UploadFile, destination: Path, extension: str) -> int:
    size = 0
    try:
        with destination.open("wb") as output:
            while chunk := await upload.read(CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_STORAGE_BYTES:
                    raise HTTPException(status_code=413, detail="This file exceeds the total 1 GiB storage quota.")
                output.write(chunk)
        if size == 0:
            raise ValueError("The uploaded file is empty.")
        verify_file(destination, extension)
        return size
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def image_to_pdf_bytes(path: Path) -> bytes:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        # Keep very large photos manageable while preserving aspect ratio.
        max_pixels = 12000
        image.thumbnail((max_pixels, max_pixels), Image.Resampling.LANCZOS)
        width_pt = image.width * 72 / 150
        height_pt = image.height * 72 / 150
        max_pt = 2000
        scale = min(1.0, max_pt / max(width_pt, height_pt))
        page = fitz.Rect(0, 0, width_pt * scale, height_pt * scale)
        image_buffer = io.BytesIO()
        image.save(image_buffer, format="JPEG", quality=92)
        pdf = fitz.open()
        p = pdf.new_page(width=page.width, height=page.height)
        p.insert_image(page, stream=image_buffer.getvalue())
        result = pdf.tobytes(garbage=4, deflate=True)
        pdf.close()
        return result


def create_merged_pdf(document_rows: list[sqlite3.Row], output_path: Path) -> None:
    merged = fitz.open()
    try:
        for row in document_rows:
            source_path = UPLOAD_DIR / row["stored_name"]
            ext = source_path.suffix.lower()
            if ext == ".pdf":
                with fitz.open(source_path) as source:
                    if source.needs_pass:
                        raise ValueError(f"{row['original_name']}: password-protected PDF.")
                    merged.insert_pdf(source)
            else:
                image_pdf = fitz.open(stream=image_to_pdf_bytes(source_path), filetype="pdf")
                merged.insert_pdf(image_pdf)
                image_pdf.close()
        if merged.page_count == 0:
            raise ValueError("There are no pages to merge.")
        merged.save(output_path, garbage=4, deflate=True)
    finally:
        merged.close()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/state")
def get_state():
    cleanup_expired()
    with connect_db() as con:
        docs = con.execute(
            "SELECT id, original_name, size, created_at, expires_at, sort_order "
            "FROM documents ORDER BY sort_order, created_at"
        ).fetchall()
        outputs = con.execute(
            "SELECT id, size, created_at, expires_at FROM merged_files ORDER BY created_at DESC"
        ).fetchall()
    return {
        "documents": [dict(row) for row in docs],
        "merged_files": [
            {**dict(row), "download_url": f"/api/merged/{row['id']}"}
            for row in outputs
        ],
        "storage": {
            "used_bytes": current_storage_bytes(),
            "limit_bytes": MAX_STORAGE_BYTES,
        },
        "upload_ttl_seconds": UPLOAD_TTL_SECONDS,
        "merged_ttl_seconds": MERGED_TTL_SECONDS,
    }


@app.post("/api/documents")
async def upload_documents(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="Choose at least one file.")
    results = []
    errors = []
    async with workspace_lock:
        cleanup_expired()
        for upload in files:
            display_name = safe_display_name(upload.filename or "document")
            extension = Path(display_name).suffix.lower()
            if extension not in ALLOWED_EXTENSIONS:
                errors.append({"filename": display_name, "error": "Unsupported file type."})
                await upload.close()
                continue
            file_id = str(uuid.uuid4())
            stored_name = f"{file_id}{extension}"
            destination = UPLOAD_DIR / stored_name
            try:
                size = await write_upload(upload, destination, extension)
                if current_storage_bytes() > MAX_STORAGE_BYTES:
                    destination.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="The shared 1 GiB storage quota is full.")
                now = time.time()
                with connect_db() as con:
                    next_order = con.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM documents").fetchone()[0]
                    con.execute(
                        """INSERT INTO documents
                        (id, original_name, stored_name, size, created_at, expires_at, sort_order)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (file_id, display_name, stored_name, size, now, now + UPLOAD_TTL_SECONDS, next_order),
                    )
                results.append({"id": file_id, "filename": display_name, "size": size})
            except HTTPException as exc:
                errors.append({"filename": display_name, "error": str(exc.detail)})
            except Exception as exc:
                errors.append({"filename": display_name, "error": str(exc)})
            finally:
                await upload.close()
    return {"uploaded": results, "errors": errors, "state": get_state()}


@app.put("/api/documents/order")
async def reorder_documents(request: OrderRequest):
    async with workspace_lock:
        cleanup_expired()
        with connect_db() as con:
            existing = [row["id"] for row in con.execute(
                "SELECT id FROM documents ORDER BY sort_order, created_at"
            ).fetchall()]
            if len(request.document_ids) != len(existing) or set(request.document_ids) != set(existing):
                raise HTTPException(status_code=400, detail="Order must contain every current document ID exactly once.")
            con.executemany(
                "UPDATE documents SET sort_order = ? WHERE id = ?",
                [(index, doc_id) for index, doc_id in enumerate(request.document_ids)],
            )
    return get_state()


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str):
    async with workspace_lock:
        with connect_db() as con:
            row = con.execute("SELECT stored_name FROM documents WHERE id = ?", (document_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Document not found or expired.")
            (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
            con.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            con.execute("UPDATE documents SET sort_order = sort_order - 1 WHERE sort_order > (SELECT COALESCE(sort_order, -1) FROM documents WHERE id = ?)", (document_id,))
    return get_state()


@app.post("/api/merge")
async def merge_documents():
    async with workspace_lock:
        cleanup_expired()
        with connect_db() as con:
            rows = con.execute("SELECT * FROM documents ORDER BY sort_order, created_at").fetchall()
        if not rows:
            raise HTTPException(status_code=400, detail="Upload at least one document before merging.")
        merged_id = str(uuid.uuid4())
        stored_name = f"{merged_id}.pdf"
        output_path = MERGED_DIR / stored_name
        try:
            create_merged_pdf(rows, output_path)
            output_size = output_path.stat().st_size
            if current_storage_bytes() > MAX_STORAGE_BYTES:
                output_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Not enough space under the shared 1 GiB storage quota for the merged PDF.")
            now = time.time()
            with connect_db() as con:
                con.execute(
                    "INSERT INTO merged_files (id, stored_name, size, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (merged_id, stored_name, output_size, now, now + MERGED_TTL_SECONDS),
                )
        except HTTPException:
            raise
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=f"Could not create the merged PDF: {exc}") from exc
    return {"id": merged_id, "download_url": f"/api/merged/{merged_id}", "size": output_size, "expires_at": now + MERGED_TTL_SECONDS}


@app.get("/api/merged/{merged_id}")
def download_merged(merged_id: str):
    cleanup_expired()
    with connect_db() as con:
        row = con.execute("SELECT stored_name FROM merged_files WHERE id = ?", (merged_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Merged PDF not found or expired.")
    path = MERGED_DIR / row["stored_name"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Merged PDF file is missing.")
    return FileResponse(path, media_type="application/pdf", filename="merged-documents.pdf")


@app.delete("/api/merged/{merged_id}")
def delete_merged(merged_id: str):
    with connect_db() as con:
        row = con.execute("SELECT stored_name FROM merged_files WHERE id = ?", (merged_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Merged PDF not found.")
        (MERGED_DIR / row["stored_name"]).unlink(missing_ok=True)
        con.execute("DELETE FROM merged_files WHERE id = ?", (merged_id,))
    return get_state()
