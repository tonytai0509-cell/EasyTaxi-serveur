from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
import sqlite3
import uuid
import requests
import os
import math

DB_PATH = "easytaxi.db"
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------------- DB ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _safe_add_column(cur, table: str, column: str, coltype: str):
    # Ajoute une colonne si elle n'existe pas (SQLite)
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Table chauffeurs
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

    # Table courses
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

    # ✅ Ajout colonnes pickup (si pas déjà là)
    _safe_add_column(cur, "jobs", "pickup_lat", "REAL")
    _safe_add_column(cur, "jobs", "pickup_lng", "REAL")

    # Table documents (partagés)
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

# ---------------- MODELES ----------------

class UpdateLocation(BaseModel):
    driver_id: str
    latitude: float
    longitude: float
    status: str # "online" / "offline"

class JobCreate(BaseModel):
    # ⚠️ utilisé pour envoi manuel (chauffeur choisi)
    driver_id: str
    customer_name: str
    address: str
    phone: str
    comment: Optional[str] = ""
    pickup_lat: Optional[float] = None
    pickup_lng: Optional[float] = None

class JobCreateAuto(BaseModel):
    # ✅ utilisé pour envoi auto (plus proche)
    customer_name: str
    address: str
    phone: str
    comment: Optional[str] = ""
    pickup_lat: float
    pickup_lng: float

class JobStatusUpdate(BaseModel):
    status: str # "new" | "accepted" | "done"

class PushTokenRegister(BaseModel):
    driver_id: str
    expo_push_token: str

class DocumentOut(BaseModel):
    id: str
    driver_id: str
    title: str
    created_at: str
    original_name: str

class DocumentRename(BaseModel):
    title: Optional[str] = None

# ---------------- FASTAPI ----------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

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

# ---------------- UTILS DISTANCE ----------------

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    # Distance en km entre 2 points
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def parse_iso(iso: str) -> Optional[datetime]:
    if not iso:
        return None
    try:
        # ex: "2025-12-15T20:32:18.708Z" ou sans Z
        s = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except:
        return None

# ---------------- ENDPOINTS CHAUFFEURS ----------------

@app.post("/update-location")
def update_location(body: UpdateLocation):
    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

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
    now = datetime.now(timezone.utc).isoformat()

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

# ---------------- INTERNAL JOB CREATE ----------------

def _create_job_and_notify(
    driver_id: str,
    customer_name: str,
    address: str,
    phone: str,
    comment: str,
    pickup_lat: Optional[float],
    pickup_lng: Optional[float],
) -> str:
    conn = get_db()
    cur = conn.cursor()

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    cur.execute(
        """
        INSERT INTO jobs (
            id, driver_id, customer_name, address, phone, comment, created_at, status, pickup_lat, pickup_lng
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            driver_id,
            customer_name,
            address,
            phone,
            comment or "",
            now,
            "new",
            pickup_lat,
            pickup_lng,
        ),
    )

    cur.execute("SELECT expo_push_token FROM drivers WHERE id = ?", (driver_id,))
    row = cur.fetchone()
    conn.commit()
    conn.close()

    if row and row["expo_push_token"]:
        send_push_notification(
            row["expo_push_token"],
            "Nouvelle course",
            f"{customer_name} - {address}",
            {"driver_id": driver_id, "job_id": job_id},
        )

    return job_id

def _pick_nearest_driver(pickup_lat: float, pickup_lng: float) -> tuple[str, float]:
    """
    Choisit le chauffeur ONLINE le + proche, parmi ceux "récents".
    """
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM drivers WHERE status = 'online'")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        raise HTTPException(status_code=409, detail="Aucun chauffeur en ligne")

    now = datetime.now(timezone.utc)
    best_id = None
    best_km = None

    for r in rows:
        lat = r["latitude"]
        lng = r["longitude"]
        upd = parse_iso(r["updated_at"])

        # ignore positions invalides
        if lat is None or lng is None:
            continue
        if float(lat) == 0 and float(lng) == 0:
            continue

        # ignore trop vieux (ex: > 90 sec)
        if upd:
            age = (now - upd).total_seconds()
            if age > 90:
                continue

        d = haversine_km(pickup_lat, pickup_lng, float(lat), float(lng))
        if best_km is None or d < best_km:
            best_km = d
            best_id = r["id"]

    if not best_id:
        raise HTTPException(status_code=409, detail="Aucun chauffeur ONLINE avec position récente")

    return best_id, float(best_km)

# ---------------- ENDPOINTS COURSES ----------------

@app.post("/jobs")
def create_job(body: JobCreate):
    job_id = _create_job_and_notify(
        body.driver_id,
        body.customer_name,
        body.address,
        body.phone,
        body.comment or "",
        body.pickup_lat,
        body.pickup_lng,
    )
    return {"ok": True, "job_id": job_id}

@app.post("/send-job")
def send_job(body: JobCreate):
    job_id = _create_job_and_notify(
        body.driver_id,
        body.customer_name,
        body.address,
        body.phone,
        body.comment or "",
        body.pickup_lat,
        body.pickup_lng,
    )
    return {"ok": True, "job_id": job_id}

# ✅ NOUVEAU : ENVOI AUTO (plus proche)
@app.post("/send-job/auto")
def send_job_auto(body: JobCreateAuto):
    driver_id, km = _pick_nearest_driver(body.pickup_lat, body.pickup_lng)

    job_id = _create_job_and_notify(
        driver_id,
        body.customer_name,
        body.address,
        body.phone,
        body.comment or "",
        body.pickup_lat,
        body.pickup_lng,
    )

    return {
        "ok": True,
        "job_id": job_id,
        "driver_id": driver_id,
        "distance_km": round(km, 3),
    }

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

# ---------------- DOCUMENTS ----------------

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

    now = datetime.now(timezone.utc).isoformat()

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
