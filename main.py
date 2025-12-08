from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import sqlite3
import uuid
import requests

DB_PATH = "easytaxi.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # ---------- Table chauffeurs ----------
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

    # ---------- Table courses ----------
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

    # ---------- Table documents (bons scannés) ----------
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            driver_id TEXT,
            title TEXT,
            image_base64 TEXT,
            created_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


init_db()

# ----------- MODELES -----------


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


class JobStatusUpdate(BaseModel):
    status: str


class PushTokenRegister(BaseModel):
    driver_id: str
    expo_push_token: str


class DocumentCreate(BaseModel):
    driver_id: str
    title: str
    image_base64: str # image en base64 envoyée par le chauffeur


# ----------- FASTAPI -----------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # on ouvre tout pour simplifier
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def send_push_notification(token: str, title: str, body: str, data: dict | None = None):
    """
    Envoie une notification push via Expo à un téléphone.
    """
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


# ----------- ENDPOINTS CHAUFFEURS -----------


@app.post("/update-location")
def update_location(body: UpdateLocation):
    conn = get_db()
    cur = conn.cursor()

    now = datetime.utcnow().isoformat()

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
    """
    Appelé par l'app chauffeur :
    - enregistre le token Expo dans la table drivers
    """
    conn = get_db()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()

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


# ----------- ENDPOINTS COURSES -----------


@app.post("/send-job")
def send_job(body: JobCreate):
    conn = get_db()
    cur = conn.cursor()

    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

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

    # récupérer expo_push_token du chauffeur
    cur.execute(
        "SELECT expo_push_token FROM drivers WHERE id = ?",
        (body.driver_id,),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()

    # si on a un token, on envoie une notification
    if row and row["expo_push_token"]:
        send_push_notification(
            row["expo_push_token"],
            "Nouvelle course",
            f"{body.customer_name} - {body.address}",
            {"driver_id": body.driver_id, "job_id": job_id},
        )

    return {"ok": True, "job_id": job_id}


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

    cur.execute(
        "UPDATE jobs SET status = ? WHERE id = ?",
        (body.status, job_id),
    )

    conn.commit()
    conn.close()

    return {"ok": True}


# ----------- ENDPOINTS DOCUMENTS (BON DE TRANSPORT) -----------


@app.post("/documents")
def upload_document(body: DocumentCreate):
    """
    Le chauffeur envoie un document scanné (photo en base64)
    """
    conn = get_db()
    cur = conn.cursor()

    doc_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    cur.execute(
        """
        INSERT INTO documents (id, driver_id, title, image_base64, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (doc_id, body.driver_id, body.title, body.image_base64, now),
    )

    conn.commit()
    conn.close()

    return {"ok": True, "id": doc_id}


@app.get("/documents/by-driver/{driver_id}")
def list_documents_for_driver(driver_id: str):
    """
    Liste des documents pour un chauffeur (utilisé par l'app chauffeur)
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM documents WHERE driver_id = ? ORDER BY created_at DESC",
        (driver_id,),
    )
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "driver_id": r["driver_id"],
            "title": r["title"],
            "image_base64": r["image_base64"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/documents")
def list_all_documents():
    """
    Liste de tous les documents (pour la centrale)
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM documents ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "driver_id": r["driver_id"],
            "title": r["title"],
            "image_base64": r["image_base64"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
