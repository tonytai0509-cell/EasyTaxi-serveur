from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
import sqlite3
import uuid
import requests
import os
import json
import math
import asyncio

DB_PATH = "easytaxi.db"
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_alter(cur, sql: str):
    try:
        cur.execute(sql)
    except Exception:
        pass


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

    # Table courses (jobs)
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

    # Ajout colonnes pour la "proposition en cascade"
    _safe_alter(cur, "ALTER TABLE jobs ADD COLUMN pickup_lat REAL")
    _safe_alter(cur, "ALTER TABLE jobs ADD COLUMN pickup_lng REAL")
    _safe_alter(cur, "ALTER TABLE jobs ADD COLUMN offered_queue TEXT") # JSON array driver_ids
    _safe_alter(cur, "ALTER TABLE jobs ADD COLUMN offer_index INTEGER") # index dans la queue
    _safe_alter(cur, "ALTER TABLE jobs ADD COLUMN current_offer_driver TEXT") # driver_id proposé actuellement
    _safe_alter(cur, "ALTER TABLE jobs ADD COLUMN offer_expires_at TEXT") # ISO UTC

    # Table documents
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

# ----------------- MODELES -----------------

class UpdateLocation(BaseModel):
    driver_id: str
    latitude: float
    longitude: float
    status: str # "online"/"offline"


class JobCreate(BaseModel):
    driver_id: str
    customer_name: str
    address: str
    phone: str
    comment: Optional[str] = ""


class JobOfferCreate(BaseModel):
    # Pour le dispatch auto (multi-proposition)
    pickup_lat: float
    pickup_lng: float
    customer_name: str
    address: str
    phone: str
    comment: Optional[str] = ""
    offer_timeout_sec: Optional[int] = 20 # délai avant de proposer au suivant
    max_drivers: Optional[int] = 10 # combien de chauffeurs max à tenter


class JobStatusUpdate(BaseModel):
    status: str


class JobDecision(BaseModel):
    driver_id: str


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


# ----------------- FASTAPI -----------------

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
        resp = requests.post(EXPO_PUSH_URL, json=payload, timeout=5)
        print("Expo push resp:", resp.status_code, resp.text)
    except Exception as e:
        print("Erreur envoi push :", e)


# ----------------- OUTILS DISTANCE -----------------

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    # distance à vol d’oiseau
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def iso_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ----------------- ENDPOINTS CHAUFFEURS -----------------

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


# ----------------- CREATION COURSE + NOTIF (ancienne logique) -----------------

def _create_job_and_notify(body: JobCreate) -> str:
    conn = get_db()
    cur = conn.cursor()

    job_id = str(uuid.uuid4())
    now = utc_now_iso()

    cur.execute(
        """
        INSERT INTO jobs (
            id, driver_id, customer_name, address, phone, comment, created_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            body.driver_id,
            body.customer_name,
            body.address,
            body.phone,
            body.comment or "",
            now,
            "new",
        ),
    )

    cur.execute("SELECT expo_push_token FROM drivers WHERE id = ?", (body.driver_id,))
    row = cur.fetchone()
    conn.commit()
    conn.close()

    if row and row["expo_push_token"]:
        send_push_notification(
            row["expo_push_token"],
            "Nouvelle course",
            f"{body.customer_name} - {body.address}",
            {"driver_id": body.driver_id, "job_id": job_id, "type": "job_new"},
        )

    return job_id


# ----------------- DISPATCH AUTO (multi-proposition) -----------------

def _get_online_drivers_sorted(pickup_lat: float, pickup_lng: float, max_drivers: int) -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, latitude, longitude, status FROM drivers WHERE status = 'online'")
    rows = cur.fetchall()
    conn.close()

    scored = []
    for r in rows:
        if r["latitude"] is None or r["longitude"] is None:
            continue
        d = haversine_km(pickup_lat, pickup_lng, float(r["latitude"]), float(r["longitude"]))
        scored.append((d, r["id"]))

    scored.sort(key=lambda x: x[0])
    return [driver_id for _, driver_id in scored[: max_drivers]]


def _push_offer_to_driver(driver_id: str, job_id: str, customer_name: str, address: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT expo_push_token FROM drivers WHERE id = ?", (driver_id,))
    row = cur.fetchone()
    conn.close()

    if row and row["expo_push_token"]:
        send_push_notification(
            row["expo_push_token"],
            "Course proposée",
            f"{customer_name} - {address}",
            {"job_id": job_id, "type": "job_offer"},
        )


def _create_offer_job(body: JobOfferCreate) -> dict:
    queue = _get_online_drivers_sorted(body.pickup_lat, body.pickup_lng, body.max_drivers or 10)
    if not queue:
        raise HTTPException(status_code=409, detail="Aucun chauffeur online disponible")

    job_id = str(uuid.uuid4())
    now = datetime.utcnow()
    expires = now + timedelta(seconds=int(body.offer_timeout_sec or 20))

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO jobs (
            id, driver_id, customer_name, address, phone, comment, created_at, status,
            pickup_lat, pickup_lng, offered_queue, offer_index, current_offer_driver, offer_expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            None,
            body.customer_name,
            body.address,
            body.phone,
            body.comment or "",
            now.isoformat(),
            "offered",
            float(body.pickup_lat),
            float(body.pickup_lng),
            json.dumps(queue),
            0,
            queue[0],
            expires.isoformat(),
        ),
    )

    conn.commit()
    conn.close()

    _push_offer_to_driver(queue[0], job_id, body.customer_name, body.address)

    return {"ok": True, "job_id": job_id, "offered_to": queue[0], "queue_size": len(queue)}


def _advance_offer(job_row: sqlite3.Row, force_next: bool = False) -> Optional[str]:
    """
    Passe au chauffeur suivant. Retourne le driver_id proposé ou None si fin.
    """
    queue = []
    try:
        queue = json.loads(job_row["offered_queue"] or "[]")
    except Exception:
        queue = []

    idx = int(job_row["offer_index"] or 0)
    if force_next:
        idx += 1
    else:
        # timeout → on passe au suivant
        idx += 1

    if idx >= len(queue):
        return None

    return queue[idx]


async def offer_watcher_loop():
    """
    Boucle qui surveille les jobs en "offered" expirés et propose au suivant.
    """
    while True:
        try:
            conn = get_db()
            cur = conn.cursor()

            cur.execute("SELECT * FROM jobs WHERE status = 'offered' AND offer_expires_at IS NOT NULL")
            rows = cur.fetchall()

            now = datetime.utcnow()

            for r in rows:
                try:
                    exp = iso_to_dt(r["offer_expires_at"])
                except Exception:
                    continue

                if now < exp:
                    continue

                next_driver = _advance_offer(r, force_next=True)

                if not next_driver:
                    # plus personne → non attribuée
                    cur.execute(
                        """
                        UPDATE jobs
                        SET status = 'unassigned',
                            current_offer_driver = NULL,
                            offer_expires_at = NULL
                        WHERE id = ?
                        """,
                        (r["id"],),
                    )
                    continue

                # update offer
                new_idx = int(r["offer_index"] or 0) + 1
                new_exp = now + timedelta(seconds=20) # même délai par défaut ici
                cur.execute(
                    """
                    UPDATE jobs
                    SET offer_index = ?,
                        current_offer_driver = ?,
                        offer_expires_at = ?
                    WHERE id = ?
                    """,
                    (new_idx, next_driver, new_exp.isoformat(), r["id"]),
                )

                _push_offer_to_driver(next_driver, r["id"], r["customer_name"], r["address"])

            conn.commit()
            conn.close()

        except Exception as e:
            print("offer_watcher_loop error:", e)

        await asyncio.sleep(2)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(offer_watcher_loop())


# ----------------- ENDPOINTS COURSES -----------------

@app.post("/jobs")
def create_job(body: JobCreate):
    job_id = _create_job_and_notify(body)
    return {"ok": True, "job_id": job_id}


@app.post("/send-job")
def send_job(body: JobCreate):
    job_id = _create_job_and_notify(body)
    return {"ok": True, "job_id": job_id}


# ✅ NOUVEAU : envoi auto au plus proche puis cascade
@app.post("/jobs/send-nearest")
def send_nearest(body: JobOfferCreate):
    return _create_offer_job(body)


# Chauffeur : récupérer les courses "proposées" à lui
@app.get("/jobs/offers/{driver_id}")
def get_offers_for_driver(driver_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'offered'
          AND current_offer_driver = ?
        ORDER BY created_at DESC
        """,
        (driver_id,),
    )
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "customer_name": r["customer_name"],
            "address": r["address"],
            "phone": r["phone"],
            "comment": r["comment"],
            "created_at": r["created_at"],
            "status": r["status"],
            "pickup_lat": r["pickup_lat"],
            "pickup_lng": r["pickup_lng"],
            "offer_expires_at": r["offer_expires_at"],
        }
        for r in rows
    ]


# Chauffeur : accepter
@app.post("/jobs/{job_id}/accept")
def accept_job(job_id: str, body: JobDecision):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = cur.fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] != "offered" or job["current_offer_driver"] != body.driver_id:
        conn.close()
        raise HTTPException(status_code=409, detail="Cette course n'est pas proposée à ce chauffeur")

    cur.execute(
        """
        UPDATE jobs
        SET driver_id = ?,
            status = 'accepted',
            current_offer_driver = NULL,
            offer_expires_at = NULL
        WHERE id = ?
        """,
        (body.driver_id, job_id),
    )

    conn.commit()
    conn.close()
    return {"ok": True}


# Chauffeur : refuser (passe immédiatement au suivant)
@app.post("/jobs/{job_id}/decline")
def decline_job(job_id: str, body: JobDecision):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = cur.fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] != "offered" or job["current_offer_driver"] != body.driver_id:
        conn.close()
        raise HTTPException(status_code=409, detail="Cette course n'est pas proposée à ce chauffeur")

    next_driver = _advance_offer(job, force_next=True)
    now = datetime.utcnow()

    if not next_driver:
        cur.execute(
            """
            UPDATE jobs
            SET status = 'unassigned',
                current_offer_driver = NULL,
                offer_expires_at = NULL
            WHERE id = ?
            """,
            (job_id,),
        )
        conn.commit()
        conn.close()
        return {"ok": True, "next": None}

    new_idx = int(job["offer_index"] or 0) + 1
    new_exp = now + timedelta(seconds=20)

    cur.execute(
        """
        UPDATE jobs
        SET offer_index = ?,
            current_offer_driver = ?,
            offer_expires_at = ?
        WHERE id = ?
        """,
        (new_idx, next_driver, new_exp.isoformat(), job_id),
    )
    conn.commit()
    conn.close()

    _push_offer_to_driver(next_driver, job_id, job["customer_name"], job["address"])
    return {"ok": True, "next": next_driver}


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


# ----------------- ENDPOINTS DOCUMENTS -----------------

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
