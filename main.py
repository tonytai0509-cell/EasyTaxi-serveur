from __future__ import annotations

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Any
from datetime import datetime, timedelta, timezone
import sqlite3
import uuid
import requests
import os
import math

DB_PATH = "easytaxi.db"
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

# ---------------- DB ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _try_add_column(cur: sqlite3.Cursor, table: str, column_def: str):
    # column_def example: "pickup_lat REAL"
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
    except sqlite3.OperationalError:
        # column already exists (or table missing)
        pass


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Drivers
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS drivers (
            id TEXT PRIMARY KEY,
            latitude REAL,
            longitude REAL,
            status TEXT,
            updated_at TEXT,
            expo_push_token TEXT
        )
        """
    )

    # Jobs
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            driver_id TEXT,
            customer_name TEXT,
            address TEXT,
            phone TEXT,
            comment TEXT,
            created_at TEXT,
            status TEXT
        )
        """
    )

    # Add missing columns if DB existed before (migration safe)
    _try_add_column(cur, "jobs", "pickup_lat REAL")
    _try_add_column(cur, "jobs", "pickup_lng REAL")

    # Documents
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            driver_id TEXT,
            title TEXT,
            filename TEXT,
            original_name TEXT,
            created_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


init_db()

# ---------------- Utils ----------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(dt_str: str | None) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        # handles "2025-12-15T20:32:18.708Z" or "...+00:00"
        s = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # distance in meters
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def send_push_notification(token: str, title: str, body: str, data: dict | None = None):
    payload = {
        "to": token,
        "sound": "default",
        "title": title,
        "body": body,
        "data": data or {},
    }
    try:
        resp = requests.post(EXPO_PUSH_URL, json=payload, timeout=8)
        print("Expo push resp:", resp.status_code, resp.text)
    except Exception as e:
        print("Erreur envoi push :", e)


def choose_nearest_online_driver(pickup_lat: float, pickup_lng: float, max_age_seconds: int = 180) -> Optional[str]:
    """
    Retourne l'id du chauffeur ONLINE le plus proche.
    max_age_seconds : on ignore les chauffeurs qui n'ont pas envoyé de position récemment.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, latitude, longitude, status, updated_at FROM drivers")
    rows = cur.fetchall()
    conn.close()

    now = datetime.now(timezone.utc)

    best_id = None
    best_dist = None

    for r in rows:
        status = (r["status"] or "").lower()
        if status != "online":
            continue

        lat = r["latitude"]
        lng = r["longitude"]
        if lat is None or lng is None:
            continue

        updated_at = parse_iso(r["updated_at"])
        if not updated_at:
            continue

        # ensure timezone-aware
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        age = (now - updated_at).total_seconds()
        if age > max_age_seconds:
            continue

        d = haversine_m(pickup_lat, pickup_lng, float(lat), float(lng))
        if best_dist is None or d < best_dist:
            best_dist = d
            best_id = r["id"]

    return best_id


# ---------------- Models ----------------

class UpdateLocation(BaseModel):
    driver_id: str
    latitude: float
    longitude: float
    status: str


class PushTokenRegister(BaseModel):
    driver_id: str
    expo_push_token: str


class JobCreate(BaseModel):
    # ✅ supporte driver_id OU chosen_driver_id (alias venant de la centrale)
    driver_id: Optional[str] = None
    chosen_driver_id: Optional[str] = Field(default=None)

    customer_name: str
    address: str
    phone: str
    comment: Optional[str] = ""

    # ✅ coordonnées départ (pour choisir le plus proche)
    pickup_lat: Optional[float] = None
    pickup_lng: Optional[float] = None

    # optionnel, on ignore si fourni
    created_at: Optional[str] = None


class JobStatusUpdate(BaseModel):
    status: str


class DocumentOut(BaseModel):
    id: str
    driver_id: str
    title: str
    created_at: str
    original_name: str


class DocumentRename(BaseModel):
    title: Optional[str] = None


# ---------------- FastAPI ----------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Drivers endpoints ----------------

@app.post("/update-location")
def update_location(body: UpdateLocation):
    conn = get_db()
    cur = conn.cursor()

    now = utc_now_iso()

    cur.execute(
        """
        INSERT INTO drivers (id, latitude, longitude, status, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          latitude = excluded.latitude,
          longitude = excluded.longitude,
          status = excluded.status,
          updated_at = excluded.updated_at
        """,
        (body.driver_id, body.latitude, body.longitude, body.status, now),
    )

    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/drivers")
def list_drivers():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM drivers")
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "status": r["status"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


@app.post("/register-push-token")
def register_push_token(body: PushTokenRegister):
    conn = get_db()
    cur = conn.cursor()
    now = utc_now_iso()

    cur.execute(
        """
        INSERT INTO drivers (id, latitude, longitude, status, updated_at, expo_push_token)
        VALUES (?, 0, 0, 'offline', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          expo_push_token = excluded.expo_push_token
        """,
        (body.driver_id, now, body.expo_push_token),
    )

    conn.commit()
    conn.close()
    return {"ok": True}

# ---------------- Jobs internals ----------------

def _resolve_driver_id(body: JobCreate) -> str:
    """
    Ordre de priorité :
    1) chosen_driver_id
    2) driver_id
    3) nearest online driver (si pickup_lat/pickup_lng fournis)
    """
    chosen = (body.chosen_driver_id or "").strip()
    if chosen:
        return chosen

    direct = (body.driver_id or "").strip()
    if direct:
        return direct

    if body.pickup_lat is not None and body.pickup_lng is not None:
        nearest = choose_nearest_online_driver(body.pickup_lat, body.pickup_lng)
        if nearest:
            return nearest

    raise HTTPException(
        status_code=422,
        detail="Aucun driver_id/chosen_driver_id fourni, et impossible de choisir un chauffeur (pickup_lat/pickup_lng manquants ou aucun chauffeur online récent)."
    )


def _create_job_and_notify(body: JobCreate) -> dict:
    driver_id = _resolve_driver_id(body)

    conn = get_db()
    cur = conn.cursor()

    job_id = str(uuid.uuid4())
    now = utc_now_iso()

    cur.execute(
        """
        INSERT INTO jobs (
            id, driver_id, customer_name, address, phone, comment, created_at, status, pickup_lat, pickup_lng
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            driver_id,
            body.customer_name,
            body.address,
            body.phone,
            body.comment or "",
            now,
            "new",
            body.pickup_lat,
            body.pickup_lng,
        ),
    )

    # token push du chauffeur
    cur.execute("SELECT expo_push_token FROM drivers WHERE id = ?", (driver_id,))
    row = cur.fetchone()

    conn.commit()
    conn.close()

    if row and row["expo_push_token"]:
        send_push_notification(
            row["expo_push_token"],
            "Nouvelle course",
            f"{body.customer_name} - {body.address}",
            {"driver_id": driver_id, "job_id": job_id},
        )

    return {"job_id": job_id, "assigned_driver_id": driver_id}

# ---------------- Jobs endpoints ----------------

@app.post("/jobs")
def create_job(body: JobCreate):
    result = _create_job_and_notify(body)
    return {"ok": True, **result}


@app.post("/send-job")
def send_job(body: JobCreate):
    # ancien endpoint conservé
    result = _create_job_and_notify(body)
    return {"ok": True, **result}


@app.get("/jobs/{driver_id}")
def get_jobs(driver_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM jobs WHERE driver_id = ? ORDER BY created_at DESC",
        (driver_id,),
    )
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "driver_id": r["driver_id"],
            "customer_name": r["customer_name"],
            "address": r["address"],
            "phone": r["phone"],
            "comment": r["comment"],
            "created_at": r["created_at"],
            "status": r["status"],
            "pickup_lat": r["pickup_lat"],
            "pickup_lng": r["pickup_lng"],
        }
        for r in rows
    ]


@app.post("/jobs/{job_id}/status")
def update_job_status(job_id: str, body: JobStatusUpdate):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id FROM jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Job not found")

    cur.execute("UPDATE jobs SET status = ? WHERE id = ?", (body.status, job_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id FROM jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Job not found")

    cur.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ---------------- Documents endpoints ----------------

@app.post("/documents/upload", response_model=DocumentOut)
async def upload_document(
    file: UploadFile = File(...),
    driver_id: str = Form("global"),
    title: str = Form(""),
):
    content_type = (file.content_type or "").lower()

    if not (
        content_type.startswith("image/")
        or content_type == "application/pdf"
        or content_type == "application/octet-stream"
    ):
        raise HTTPException(status_code=400, detail=f"Format non supporté ({content_type})")

    driver_id = (driver_id or "").strip()
    if driver_id in ("", "undefined", "null"):
        driver_id = "global"

    doc_id = str(uuid.uuid4())
    _, ext = os.path.splitext(file.filename or "")
    if not ext:
        if content_type.startswith("image/") or content_type == "application/octet-stream":
            ext = ".jpg"
        elif content_type == "application/pdf":
            ext = ".pdf"
        else:
            ext = ".bin"

    stored_name = f"{doc_id}{ext}"
    path = os.path.join(UPLOAD_DIR, stored_name)

    with open(path, "wb") as f:
        content = await file.read()
        f.write(content)

    now = utc_now_iso()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO documents (id, driver_id, title, filename, original_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (doc_id, driver_id, title, stored_name, file.filename or "", now),
    )
    conn.commit()
    conn.close()

    return DocumentOut(
        id=doc_id,
        driver_id=driver_id,
        title=title,
        created_at=now,
        original_name=file.filename or "",
    )


@app.get("/documents", response_model=List[DocumentOut])
def list_documents():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM documents ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()

    return [
        DocumentOut(
            id=r["id"],
            driver_id=r["driver_id"],
            title=r["title"],
            created_at=r["created_at"],
            original_name=r["original_name"],
        )
        for r in rows
    ]


@app.get("/documents/{doc_id}/download")
def download_document(doc_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Document introuvable")

    path = os.path.join(UPLOAD_DIR, row["filename"])
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Fichier manquant sur le serveur")

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=row["original_name"] or "document",
    )


@app.patch("/documents/{doc_id}", response_model=DocumentOut)
def rename_document(doc_id: str, body: DocumentRename):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Document introuvable")

    new_title = body.title if body.title is not None else row["title"]

    cur.execute("UPDATE documents SET title = ? WHERE id = ?", (new_title, doc_id))
    conn.commit()

    cur.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
    updated = cur.fetchone()
    conn.close()

    return DocumentOut(
        id=updated["id"],
        driver_id=updated["driver_id"],
        title=updated["title"],
        created_at=updated["created_at"],
        original_name=updated["original_name"],
    )


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
    row = cur.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Document introuvable")

    path = os.path.join(UPLOAD_DIR, row["filename"])
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            print("Erreur suppression fichier :", e)

    cur.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()

    return {"ok": True}
