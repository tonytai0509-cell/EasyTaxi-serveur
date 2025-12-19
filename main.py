from __future__ import annotations

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Set
from datetime import datetime, timedelta
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

def column_exists(cur, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r["name"] for r in cur.fetchall()]
    return col in cols

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # drivers
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

    # jobs
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

    # migrations jobs
    if not column_exists(cur, "jobs", "pickup_lat"):
        cur.execute("ALTER TABLE jobs ADD COLUMN pickup_lat REAL")
    if not column_exists(cur, "jobs", "pickup_lng"):
        cur.execute("ALTER TABLE jobs ADD COLUMN pickup_lng REAL")
    if not column_exists(cur, "jobs", "offer_expires_at"):
        cur.execute("ALTER TABLE jobs ADD COLUMN offer_expires_at TEXT")

    # root_job_id : suivre une même course à travers redistributions
    if not column_exists(cur, "jobs", "root_job_id"):
        cur.execute("ALTER TABLE jobs ADD COLUMN root_job_id TEXT")

    # table refus (anti-boucle)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS job_declines (
            root_job_id TEXT,
            driver_id TEXT,
            declined_at TEXT,
            PRIMARY KEY (root_job_id, driver_id)
        )
        """
    )

    # documents
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

# ---------------- MODELS ----------------

class UpdateLocation(BaseModel):
    driver_id: str
    latitude: float
    longitude: float
    status: str

class JobCreate(BaseModel):
    driver_id: str
    customer_name: str
    address: str
    phone: str
    comment: Optional[str] = ""

class AutoJobCreate(BaseModel):
    pickup_lat: float
    pickup_lng: float
    customer_name: str
    address: str
    phone: str
    comment: Optional[str] = ""
    max_age_sec: Optional[int] = 120
    max_radius_km: Optional[float] = 50.0
    offer_ttl_sec: Optional[int] = 180 # défaut 180s

class JobStatusUpdate(BaseModel):
    status: str

class PushTokenRegister(BaseModel):
    driver_id: str
    expo_push_token: str

class OfferDecision(BaseModel):
    driver_id: str

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

def now_iso():
    return datetime.utcnow().isoformat()

def send_push_notification(token: str, title: str, body: str, data: Optional[Dict[str, Any]] = None):
    payload = {
        "to": token,
        "sound": "default",
        "title": title,
        "body": body,
        "data": data or {},
    }
    try:
        resp = requests.post(EXPO_PUSH_URL, json=payload, timeout=7)
        print("Expo push resp:", resp.status_code, resp.text)
    except Exception as e:
        print("Erreur envoi push :", e)

# ---------------- UTILS ----------------

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def parse_iso(dt_str: str) -> Optional[datetime]:
    try:
        s = (dt_str or "").replace("Z", "")
        return datetime.fromisoformat(s)
    except:
        return None

def is_offer_expired(offer_expires_at: Optional[str]) -> bool:
    if not offer_expires_at:
        return False
    dt = parse_iso(offer_expires_at)
    if not dt:
        return False
    return datetime.utcnow() > dt

def get_declined_driver_ids(root_job_id: str) -> Set[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT driver_id FROM job_declines WHERE root_job_id = ?", (root_job_id,))
    rows = cur.fetchall()
    conn.close()
    return set([str(r["driver_id"]) for r in rows])

def mark_declined(root_job_id: str, driver_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO job_declines (root_job_id, driver_id, declined_at)
        VALUES (?, ?, ?)
        """,
        (root_job_id, str(driver_id), now_iso()),
    )
    conn.commit()
    conn.close()

# ---------------- DRIVER ENDPOINTS ----------------

@app.post("/update-location")
def update_location(body: UpdateLocation):
    conn = get_db()
    cur = conn.cursor()
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
        (body.driver_id, body.latitude, body.longitude, body.status, now_iso()),
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
    cur.execute(
        """
        INSERT INTO drivers (id, latitude, longitude, status, updated_at, expo_push_token)
        VALUES (?, 0, 0, 'offline', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          expo_push_token = excluded.expo_push_token
        """,
        (body.driver_id, now_iso(), body.expo_push_token),
    )
    conn.commit()
    conn.close()
    return {"ok": True}

# ---------------- JOB CORE ----------------

def _create_job_and_notify(
    driver_id: str,
    customer_name: str,
    address: str,
    phone: str,
    comment: str,
    pickup_lat: Optional[float] = None,
    pickup_lng: Optional[float] = None,
    status: str = "new",
    offer_ttl_sec: Optional[int] = None,
    root_job_id: Optional[str] = None,
) -> str:
    conn = get_db()
    cur = conn.cursor()

    job_id = str(uuid.uuid4())
    created_at = now_iso()

    # root_job_id : si absent => le job devient sa propre racine
    root = root_job_id or job_id

    offer_expires_at = None # <= par défaut, pas de TTL sur les jobs "new"
    if status == "offered":
        ttl = int(offer_ttl_sec or 180)
        offer_expires_at = (datetime.utcnow() + timedelta(seconds=ttl)).isoformat()

    cur.execute(
        """
        INSERT INTO jobs (
            id, driver_id, customer_name, address, phone, comment, created_at, status,
            pickup_lat, pickup_lng, offer_expires_at, root_job_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            str(driver_id),
            customer_name,
            address,
            phone,
            comment or "",
            created_at,
            status,
            pickup_lat,
            pickup_lng,
            offer_expires_at,
            root,
        ),
    )

    cur.execute("SELECT expo_push_token FROM drivers WHERE id = ?", (str(driver_id),))
    row = cur.fetchone()
    conn.commit()
    conn.close()

    if row and row["expo_push_token"]:
        if status == "offered":
            send_push_notification(
                row["expo_push_token"],
                "🚨 Course proposée",
                f"{customer_name} - {address}",
                {"type": "job_offer", "driver_id": str(driver_id), "job_id": job_id, "root_job_id": root},
            )
        else:
            send_push_notification(
                row["expo_push_token"],
                "Nouvelle course",
                f"{customer_name} - {address}",
                {"type": "job_new", "driver_id": str(driver_id), "job_id": job_id, "root_job_id": root},
            )

    return job_id

def _pick_nearest_online_driver(
    pickup_lat: float,
    pickup_lng: float,
    max_age_sec: int,
    max_radius_km: float,
    exclude_driver_ids: Optional[Set[str]] = None,
):
    exclude_driver_ids = exclude_driver_ids or set()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM drivers WHERE status = 'online'")
    rows = cur.fetchall()
    conn.close()

    now = datetime.utcnow()
    best = None

    for r in rows:
        did = str(r["id"])
        if did in exclude_driver_ids:
            continue

        lat = r["latitude"]
        lng = r["longitude"]
        if lat is None or lng is None:
            continue

        dt = parse_iso(r["updated_at"] or "")
        if not dt:
            continue
        if (now - dt) > timedelta(seconds=int(max_age_sec or 120)):
            continue

        d = haversine_km(float(pickup_lat), float(pickup_lng), float(lat), float(lng))
        if d > float(max_radius_km or 50.0):
            continue

        if best is None or d < best["distance_km"]:
            best = {
                "driver_id": did,
                "distance_km": d,
                "updated_at": r["updated_at"],
            }

    return best

def _redistribute_offer_from_job(job_row, max_age_sec: int, max_radius_km: float, offer_ttl_sec: int):
    pickup_lat = job_row["pickup_lat"]
    pickup_lng = job_row["pickup_lng"]
    if pickup_lat is None or pickup_lng is None:
        return None

    root_job_id = str(job_row["root_job_id"] or job_row["id"])
    declined = get_declined_driver_ids(root_job_id)

    pick = _pick_nearest_online_driver(
        pickup_lat=float(pickup_lat),
        pickup_lng=float(pickup_lng),
        max_age_sec=int(max_age_sec or 120),
        max_radius_km=float(max_radius_km or 50.0),
        exclude_driver_ids=declined,
    )
    if not pick:
        return None

    new_job_id = _create_job_and_notify(
        driver_id=pick["driver_id"],
        customer_name=job_row["customer_name"],
        address=job_row["address"],
        phone=job_row["phone"],
        comment=job_row["comment"] or "",
        pickup_lat=float(pickup_lat),
        pickup_lng=float(pickup_lng),
        status="offered",
        offer_ttl_sec=int(offer_ttl_sec or 180),
        root_job_id=root_job_id,
    )

    return {
        "new_offer_job_id": new_job_id,
        "chosen_driver_id": pick["driver_id"],
        "distance_km": round(pick["distance_km"], 3),
        "driver_updated_at": pick["updated_at"],
        "root_job_id": root_job_id,
    }

# ---------------- JOB ENDPOINTS ----------------

@app.post("/jobs")
def create_job(body: JobCreate):
    job_id = _create_job_and_notify(
        body.driver_id, body.customer_name, body.address, body.phone, body.comment or ""
    )
    return {"ok": True, "job_id": job_id}

@app.post("/send-job")
def send_job(body: JobCreate):
    job_id = _create_job_and_notify(
        body.driver_id, body.customer_name, body.address, body.phone, body.comment or ""
    )
    return {"ok": True, "job_id": job_id}

# DIRECT : AUTO -> new
@app.post("/send-job-auto")
def send_job_auto(body: AutoJobCreate):
    pick = _pick_nearest_online_driver(
        pickup_lat=body.pickup_lat,
        pickup_lng=body.pickup_lng,
        max_age_sec=int(body.max_age_sec or 120),
        max_radius_km=float(body.max_radius_km or 50.0),
        exclude_driver_ids=set(),
    )
    if not pick:
        raise HTTPException(status_code=404, detail="Aucun chauffeur online proche (position trop ancienne ou trop loin).")

    job_id = _create_job_and_notify(
        driver_id=pick["driver_id"],
        customer_name=body.customer_name,
        address=body.address,
        phone=body.phone,
        comment=body.comment or "",
        pickup_lat=body.pickup_lat,
        pickup_lng=body.pickup_lng,
        status="new",
    )

    return {
        "ok": True,
        "job_id": job_id,
        "chosen_driver_id": pick["driver_id"],
        "distance_km": round(pick["distance_km"], 3),
        "driver_updated_at": pick["updated_at"],
    }

# OFFERS : AUTO -> offered
@app.post("/send-job-auto-offer")
def send_job_auto_offer(body: AutoJobCreate):
    pick = _pick_nearest_online_driver(
        pickup_lat=body.pickup_lat,
        pickup_lng=body.pickup_lng,
        max_age_sec=int(body.max_age_sec or 120),
        max_radius_km=float(body.max_radius_km or 50.0),
        exclude_driver_ids=set(),
    )
    if not pick:
        raise HTTPException(status_code=404, detail="Aucun chauffeur online proche (position trop ancienne ou trop loin).")

    job_id = _create_job_and_notify(
        driver_id=pick["driver_id"],
        customer_name=body.customer_name,
        address=body.address,
        phone=body.phone,
        comment=body.comment or "",
        pickup_lat=body.pickup_lat,
        pickup_lng=body.pickup_lng,
        status="offered",
        offer_ttl_sec=int(body.offer_ttl_sec or 180),
        root_job_id=None,
    )

    return {
        "ok": True,
        "job_id": job_id,
        "chosen_driver_id": pick["driver_id"],
        "distance_km": round(pick["distance_km"], 3),
        "driver_updated_at": pick["updated_at"],
    }

# Chauffeur: récupère ses courses (pas les offered/declined)
@app.get("/jobs/{driver_id}")
def get_jobs(driver_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM jobs
        WHERE driver_id = ?
          AND status NOT IN ('offered','declined')
        ORDER BY created_at DESC
        """,
        (str(driver_id),),
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
            "root_job_id": r["root_job_id"],
        }
        for r in rows
    ]

# OFFERS: le chauffeur lit ses offres (expire => declined + redistribue)
@app.get("/jobs/offers/{driver_id}")
def get_offers(driver_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM jobs
        WHERE driver_id = ?
          AND status = 'offered'
        ORDER BY created_at DESC
        """,
        (str(driver_id),),
    )
    rows = cur.fetchall()

    offers = []
    for r in rows:
        if is_offer_expired(r["offer_expires_at"]):
            root = str(r["root_job_id"] or r["id"])
            mark_declined(root, str(r["driver_id"]))

            # passer declined
            cur.execute("UPDATE jobs SET status='declined', offer_expires_at=NULL WHERE id = ?", (r["id"],))
            conn.commit()

            # redistribution (TTL 180 par défaut)
            _redistribute_offer_from_job(
                job_row=r,
                max_age_sec=120,
                max_radius_km=50.0,
                offer_ttl_sec=180,
            )
            continue

        offers.append(
            {
                "id": r["id"],
                "driver_id": r["driver_id"],
                "customer_name": r["customer_name"],
                "address": r["address"],
                "phone": r["phone"],
                "comment": r["comment"],
                "created_at": r["created_at"],
                "status": "offered",
                "pickup_lat": r["pickup_lat"],
                "pickup_lng": r["pickup_lng"],
                "offer_expires_at": r["offer_expires_at"],
                "root_job_id": r["root_job_id"],
            }
        )

    conn.close()
    return offers

# OFFERS: accepter (offered -> new)
@app.post("/jobs/{job_id}/accept")
def accept_offer(job_id: str, body: OfferDecision):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        raise HTTPException(status_code=404, detail="Offer not found")

    if r["status"] != "offered":
        conn.close()
        raise HTTPException(status_code=409, detail="Offer already handled")

    if str(r["driver_id"]) != str(body.driver_id):
        conn.close()
        raise HTTPException(status_code=403, detail="Not your offer")

    if is_offer_expired(r["offer_expires_at"]):
        root = str(r["root_job_id"] or r["id"])
        mark_declined(root, str(r["driver_id"]))
        cur.execute("UPDATE jobs SET status='declined', offer_expires_at=NULL WHERE id = ?", (job_id,))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=410, detail="Offer expired")

    cur.execute("UPDATE jobs SET status='new', offer_expires_at=NULL WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# OFFERS: refuser (offered -> declined) + redistribution auto
@app.post("/jobs/{job_id}/decline")
def decline_offer(job_id: str, body: OfferDecision):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        raise HTTPException(status_code=404, detail="Offer not found")

    if r["status"] != "offered":
        conn.close()
        raise HTTPException(status_code=409, detail="Offer already handled")

    if str(r["driver_id"]) != str(body.driver_id):
        conn.close()
        raise HTTPException(status_code=403, detail="Not your offer")

    root = str(r["root_job_id"] or r["id"])
    mark_declined(root, str(body.driver_id))

    cur.execute("UPDATE jobs SET status='declined', offer_expires_at=NULL WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()

    redistributed = _redistribute_offer_from_job(
        job_row=r,
        max_age_sec=120,
        max_radius_km=50.0,
        offer_ttl_sec=180,
    )

    return {"ok": True, "redistributed": redistributed}

# SUPPRIMER : si c’est une course AUTO, on la considère comme refusée + redistribution
@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Job not found")

    # On prépare les infos pour éventuellement redistribuer
    root_job_id = str(row["root_job_id"] or row["id"])
    driver_id = str(row["driver_id"])

    redistributed = None

    # Si la course vient d'un AUTO (on a un point de prise en charge)
    # et qu’elle n’est pas terminée, on la traite comme un refus
    if row["status"] in ("new", "accepted") and row["pickup_lat"] is not None and row["pickup_lng"] is not None:
        mark_declined(root_job_id, driver_id)

        redistributed = _redistribute_offer_from_job(
            job_row=row,
            max_age_sec=120,
            max_radius_km=50.0,
            offer_ttl_sec=180,
        )

    # On supprime quand même la course pour ce chauffeur
    cur.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()

    return {"ok": True, "redistributed": redistributed}

# ---------------- DEBUG ----------------

@app.get("/debug/offers/{driver_id}")
def debug_offers(driver_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, driver_id, status, created_at, offer_expires_at, root_job_id, pickup_lat, pickup_lng
        FROM jobs
        WHERE driver_id=?
        ORDER BY created_at DESC
        LIMIT 30
        """,
        (str(driver_id),),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

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

    created_at = now_iso()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO documents (id, driver_id, title, filename, original_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (doc_id, driver_id, title, stored_name, file.filename or "", created_at),
    )
    conn.commit()
    conn.close()

    return DocumentOut(id=doc_id, driver_id=driver_id, title=title, created_at=created_at, original_name=file.filename or "")

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
