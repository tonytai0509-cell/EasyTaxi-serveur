from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import sqlite3
import uuid
import os
import math
import requests

DB_PATH = "easytaxi.db"
UPLOAD_DIR = "uploads"

os.makedirs(UPLOAD_DIR, exist_ok=True)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------------------------------------------------
# DB
# -------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Chauffeurs
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

    # Courses "racine"
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            customer_name TEXT,
            phone TEXT,
            address TEXT,
            comment TEXT,
            created_at TEXT,
            status TEXT,
            driver_id TEXT,
            pickup_lat REAL,
            pickup_lng REAL,
            is_auto INTEGER DEFAULT 0
        )
        """
    )

    # Offres AUTO à chaque chauffeur
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS job_offers (
            id TEXT PRIMARY KEY,
            root_job_id TEXT,
            driver_id TEXT,
            customer_name TEXT,
            address TEXT,
            phone TEXT,
            comment TEXT,
            status TEXT,
            created_at TEXT,
            pickup_lat REAL,
            pickup_lng REAL,
            offer_expires_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


init_db()


# -------------------------------------------------------------------
# Utils
# -------------------------------------------------------------------
def now_iso() -> str:
    return datetime.utcnow().isoformat()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def send_push(token: str, title: str, body: str, data: Optional[Dict[str, Any]] = None):
    if not token:
        return
    try:
        payload = {
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            "data": data or {},
        }
        requests.post(EXPO_PUSH_URL, json=payload, timeout=5)
    except Exception as e:
        print("push error", e)


def choose_nearest_driver(
    pickup_lat: float,
    pickup_lng: float,
    max_age_sec: int,
    max_radius_km: float,
    exclude_ids: Optional[List[str]] = None,
):
    exclude_ids = exclude_ids or []
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM drivers WHERE status = 'online'")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    best = None
    best_dist = None
    limit_time = datetime.utcnow() - timedelta(seconds=max_age_sec)

    for d in rows:
        if str(d["id"]) in exclude_ids:
            continue

        try:
            updated_at = datetime.fromisoformat(d["updated_at"])
        except Exception:
            continue

        if updated_at < limit_time:
            continue

        lat = float(d["latitude"])
        lng = float(d["longitude"])
        dist = haversine_km(pickup_lat, pickup_lng, lat, lng)

        if dist > max_radius_km:
            continue

        if best is None or dist < best_dist:
            best = d
            best_dist = dist

    if best is None:
        return None, None

    return best, best_dist


# -------------------------------------------------------------------
# Schemas
# -------------------------------------------------------------------
class LocationUpdate(BaseModel):
    driver_id: str
    latitude: float
    longitude: float
    status: str


class PushTokenPayload(BaseModel):
    driver_id: str
    expo_push_token: str


class ManualJobPayload(BaseModel):
    driver_id: str
    customer_name: str
    phone: str
    address: str
    comment: Optional[str] = ""


class AutoJobPayload(BaseModel):
    pickup_lat: float
    pickup_lng: float
    customer_name: str
    phone: str
    address: str
    comment: Optional[str] = ""
    max_age_sec: int = 120
    max_radius_km: float = 60.0
    offer_ttl_sec: int = 180
    root_job_id: Optional[str] = None


class JobStatusUpdate(BaseModel):
    status: str


class BusyPayload(BaseModel):
    driver_id: str


class MessagePayload(BaseModel):
    driver_id: str
    title: str
    body: str


# -------------------------------------------------------------------
# Drivers
# -------------------------------------------------------------------
@app.get("/drivers")
def list_drivers():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM drivers")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@app.post("/update-location")
def update_location(payload: LocationUpdate):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO drivers (id, latitude, longitude, status, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            latitude=excluded.latitude,
            longitude=excluded.longitude,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (
            payload.driver_id,
            payload.latitude,
            payload.longitude,
            payload.status,
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/register-push-token")
def register_push_token(payload: PushTokenPayload):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO drivers (id, expo_push_token, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            expo_push_token=excluded.expo_push_token
        """,
        (payload.driver_id, payload.expo_push_token, now_iso()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# -------------------------------------------------------------------
# Jobs – chauffeur
# -------------------------------------------------------------------
@app.get("/jobs/{driver_id}")
def get_jobs_for_driver(driver_id: str):
    """
    Retourne les courses du chauffeur, sans les 'done' (terminées).
    Le chauffeur ne peut donc plus les supprimer après.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM jobs
        WHERE driver_id = ?
          AND (status IS NULL OR status != 'done')
        ORDER BY datetime(created_at) DESC
        """,
        (driver_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@app.post("/jobs/{job_id}/status")
def update_job_status(job_id: str, payload: JobStatusUpdate):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE jobs SET status = ? WHERE id = ?", (payload.status, job_id))
    conn.commit()
    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return dict(row)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """
    Suppression définitive (utilisée côté centrale uniquement).
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    cur.execute("DELETE FROM job_offers WHERE root_job_id = ?", (job_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# -------------------------------------------------------------------
# OFFRES AUTO – chauffeur
# -------------------------------------------------------------------
@app.get("/jobs/offers/{driver_id}")
def get_offers_for_driver(driver_id: str):
    now = datetime.utcnow()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM job_offers
        WHERE driver_id = ?
          AND status = 'offered'
        ORDER BY datetime(created_at) DESC
        """,
        (driver_id,),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        exp = d.get("offer_expires_at")
        if exp:
            try:
                if datetime.fromisoformat(exp) < now:
                    continue
            except Exception:
                pass
        rows.append(d)
    conn.close()
    return rows


@app.post("/jobs/{offer_id}/accept")
def accept_offer(offer_id: str, payload: BusyPayload):
    driver_id = payload.driver_id
    now = datetime.utcnow()
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM job_offers WHERE id = ?", (offer_id,))
    offer = cur.fetchone()
    if not offer:
        conn.close()
        raise HTTPException(status_code=404, detail="Offer not found")

    offer = dict(offer)
    if offer["status"] != "offered":
        conn.close()
        raise HTTPException(status_code=400, detail="Offer not valid")

    exp = offer.get("offer_expires_at")
    if exp:
        try:
            if datetime.fromisoformat(exp) < now:
                conn.close()
                raise HTTPException(status_code=400, detail="Offer expired")
        except Exception:
            pass

    root_job_id = offer["root_job_id"]

    # MAJ offre
    cur.execute(
        "UPDATE job_offers SET status = 'accepted' WHERE id = ?",
        (offer_id,),
    )

    # MAJ / création job racine
    cur.execute("SELECT * FROM jobs WHERE id = ?", (root_job_id,))
    root = cur.fetchone()
    if not root:
        cur.execute(
            """
            INSERT INTO jobs (
                id, customer_name, phone, address, comment,
                created_at, status, driver_id, pickup_lat, pickup_lng, is_auto
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_job_id,
                offer["customer_name"],
                offer["phone"],
                offer["address"],
                offer["comment"],
                now_iso(),
                "accepted",
                driver_id,
                offer["pickup_lat"],
                offer["pickup_lng"],
                1,
            ),
        )
    else:
        cur.execute(
            """
            UPDATE jobs
            SET status = 'accepted',
                driver_id = ?,
                pickup_lat = COALESCE(pickup_lat, ?),
                pickup_lng = COALESCE(pickup_lng, ?),
                is_auto = 1
            WHERE id = ?
            """,
            (
                driver_id,
                offer["pickup_lat"],
                offer["pickup_lng"],
                root_job_id,
            ),
        )

    conn.commit()
    conn.close()

    return {"ok": True, "root_job_id": root_job_id}


@app.post("/jobs/{offer_id}/decline")
def decline_offer(offer_id: str, payload: BusyPayload):
    """
    Le chauffeur met "Occupé" sur la pop-up AUTO.
    On marque l'offre comme 'declined', la centrale pourra utiliser
    le bouton "Suivant" pour reproposer cette course.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM job_offers WHERE id = ?", (offer_id,))
    offer = cur.fetchone()
    if not offer:
        conn.close()
        raise HTTPException(status_code=404, detail="Offer not found")

    offer = dict(offer)
    root_job_id = offer["root_job_id"]

    cur.execute(
        "UPDATE job_offers SET status = 'declined' WHERE id = ?",
        (offer_id,),
    )
    cur.execute(
        "UPDATE jobs SET status = 'declined' WHERE id = ?",
        (root_job_id,),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "root_job_id": root_job_id}
    

# -------------------------------------------------------------------
# AUTO – logique de distribution
# -------------------------------------------------------------------
def internal_auto_offer(
    root_job_id: str,
    pickup_lat: float,
    pickup_lng: float,
    customer_name: str,
    phone: str,
    address: str,
    comment: str,
    max_age_sec: int,
    max_radius_km: float,
    offer_ttl_sec: int,
    extra_exclude: Optional[List[str]] = None,
):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT driver_id FROM job_offers WHERE root_job_id = ?",
        (root_job_id,),
    )
    already = [str(r[0]) for r in cur.fetchall()]
    if extra_exclude:
        already.extend(extra_exclude)

    best, dist = choose_nearest_driver(
        pickup_lat, pickup_lng, max_age_sec, max_radius_km, already
    )
    if best is None:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail="Aucun chauffeur online proche (position trop ancienne ou trop loin).",
        )

    offer_id = str(uuid.uuid4())
    created = now_iso()
    expires = (datetime.utcnow() + timedelta(seconds=offer_ttl_sec)).isoformat()

    cur.execute(
        """
        INSERT INTO job_offers (
            id, root_job_id, driver_id, customer_name, address, phone, comment,
            status, created_at, pickup_lat, pickup_lng, offer_expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'offered', ?, ?, ?, ?)
        """,
        (
            offer_id,
            root_job_id,
            best["id"],
            customer_name,
            address,
            phone,
            comment,
            created,
            pickup_lat,
            pickup_lng,
            expires,
        ),
    )

    # MAJ job racine (statut "new" + coordonnées)
    cur.execute(
        """
        UPDATE jobs
        SET pickup_lat = ?,
            pickup_lng = ?,
            is_auto = 1,
            status = COALESCE(status, 'new')
        WHERE id = ?
        """,
        (pickup_lat, pickup_lng, root_job_id),
    )

    conn.commit()

    # push
    token = best.get("expo_push_token")
    send_push(
        token,
        "Course proposée",
        f"Client - {address}",
        {"type": "job_offer", "job_id": offer_id},
    )

    conn.close()
    return {
        "offer_id": offer_id,
        "chosen_driver_id": best["id"],
        "distance_km": dist,
        "root_job_id": root_job_id,
    }


@app.post("/send-job-auto-offer")
def send_job_auto_offer(payload: AutoJobPayload):
    conn = get_db()
    cur = conn.cursor()

    if payload.root_job_id:
        root_job_id = payload.root_job_id
        cur.execute("SELECT * FROM jobs WHERE id = ?", (root_job_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Root job not found")
    else:
        root_job_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO jobs (
                id, customer_name, phone, address, comment,
                created_at, status, driver_id, pickup_lat, pickup_lng, is_auto
            ) VALUES (?, ?, ?, ?, ?, ?, 'new', NULL, ?, ?, 1)
            """,
            (
                root_job_id,
                payload.customer_name,
                payload.phone,
                payload.address,
                payload.comment,
                now_iso(),
                payload.pickup_lat,
                payload.pickup_lng,
            ),
        )
        conn.commit()

    conn.close()

    return internal_auto_offer(
        root_job_id=root_job_id,
        pickup_lat=payload.pickup_lat,
        pickup_lng=payload.pickup_lng,
        customer_name=payload.customer_name,
        phone=payload.phone,
        address=payload.address,
        comment=payload.comment or "",
        max_age_sec=payload.max_age_sec,
        max_radius_km=payload.max_radius_km,
        offer_ttl_sec=payload.offer_ttl_sec,
    )


@app.post("/jobs/{job_id}/busy")
def job_back_to_auto(job_id: str, payload: BusyPayload):
    """
    Utilisé par :
      - bouton "Occupé" sur une course AUTO déjà attribuée au chauffeur
      - bouton "Suivant" dans l’onglet Courses côté centrale

    On remet la course en recherche AUTO, en excluant le chauffeur précédent.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = cur.fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Job not found")
    job = dict(job)

    pickup_lat = job.get("pickup_lat")
    pickup_lng = job.get("pickup_lng")
    if pickup_lat is None or pickup_lng is None:
        conn.close()
        raise HTTPException(status_code=400, detail="Job has no pickup location")

    customer_name = job.get("customer_name") or "Client"
    phone = job.get("phone") or ""
    address = job.get("address") or ""
    comment = job.get("comment") or ""

    # statut reset
    cur.execute(
        """
        UPDATE jobs
        SET status = 'new',
            driver_id = NULL
        WHERE id = ?
        """,
        (job_id,),
    )
    conn.commit()
    conn.close()

    return internal_auto_offer(
        root_job_id=job_id,
        pickup_lat=pickup_lat,
        pickup_lng=pickup_lng,
        customer_name=customer_name,
        phone=phone,
        address=address,
        comment=comment,
        max_age_sec=120,
        max_radius_km=60.0,
        offer_ttl_sec=180,
        extra_exclude=[payload.driver_id],
    )


# -------------------------------------------------------------------
# Jobs – centrale
# -------------------------------------------------------------------
@app.post("/send-job")
def send_job_manual(payload: ManualJobPayload):
    """
    Envoi MANUEL à un chauffeur précis.
    Le chauffeur verra la course comme 'new' avec les boutons
    ACCEPTER / OCCUPÉ dans l’app.
    """
    job_id = str(uuid.uuid4())
    created = now_iso()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO jobs (
            id, customer_name, phone, address, comment,
            created_at, status, driver_id, pickup_lat, pickup_lng, is_auto
        ) VALUES (?, ?, ?, ?, ?, ?, 'new', ?, NULL, NULL, 0)
        """,
        (
            job_id,
            payload.customer_name,
            payload.phone,
            payload.address,
            payload.comment or "",
            created,
            payload.driver_id,
        ),
    )
    conn.commit()

    # push notif
    cur.execute("SELECT expo_push_token FROM drivers WHERE id = ?", (payload.driver_id,))
    row = cur.fetchone()
    token = row[0] if row else None
    conn.close()

    send_push(
        token,
        "Nouvelle course",
        f"{payload.customer_name} - {payload.address}",
        {"type": "job_manual", "job_id": job_id},
    )

    return {"ok": True, "job_id": job_id}


@app.get("/jobs")
def list_all_jobs():
    """
    Liste globale pour l’onglet "Courses" de la centrale.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM jobs
        ORDER BY datetime(created_at) DESC
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# -------------------------------------------------------------------
# Message centrale -> chauffeur
# -------------------------------------------------------------------
@app.post("/send-message")
def send_message(payload: MessagePayload):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT expo_push_token FROM drivers WHERE id = ?",
        (payload.driver_id,),
    )
    row = cur.fetchone()
    conn.close()

    token = row[0] if row else None
    if not token:
        raise HTTPException(status_code=404, detail="Chauffeur sans push token")

    send_push(
        token,
        payload.title,
        payload.body,
        {"type": "central_message"},
    )

    return {"ok": True}
