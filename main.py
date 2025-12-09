from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import sqlite3
import uuid
import requests
import os

DB_PATH = "easytaxi.db"
UPLOAD_DIR = "uploads"

# Assurer que le dossier d'upload existe
os.makedirs(UPLOAD_DIR, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


class DocumentOut(BaseModel):
    id: str
    driver_id: str
    title: str
    created_at: str
    original_name: str


# ----------- FASTAPI -----------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # pour ton usage, on ouvre tout
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def send_push_notification(
    token: str,
    title: str,
    body: str,
    data: dict | None = None,
):
    """
    Envoie une notification push via Expo à un téléphone chauffeur.
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
    """
    Appelée par l'app chauffeur toutes les X secondes avec sa position.
    """
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
    """
    Utilisé par la centrale pour afficher les taxis sur la carte.
    """
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


# ----------- FONCTION INTERNE POUR CREER UNE COURSE -----------

def _create_job_and_notify(body: JobCreate) -> str:
    """
    Crée une course dans la base + envoie la notif push
    si le chauffeur a un token Expo enregistré.
    Retourne l'id de la course.
    """
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

    return job_id


# ----------- ENDPOINTS COURSES -----------

@app.post("/jobs")
def create_job(body: JobCreate):
    """
    Endpoint principal utilisé par l'application centrale
    pour créer une course.
    """
    job_id = _create_job_and_notify(body)
    return {"ok": True, "job_id": job_id}


# Ancien endpoint (toujours dispo si tu l'utilises)
@app.post("/send-job")
def send_job(body: JobCreate):
    job_id = _create_job_and_notify(body)
    return {"ok": True, "job_id": job_id}


@app.get("/jobs/{driver_id}")
def get_jobs(driver_id: str):
    """
    Utilisé par l'app chauffeur pour récupérer ses courses.
    """
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
    """
    Appelé par l'app chauffeur quand il accepte / termine la course.
    """
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


# ----------- ENDPOINTS DOCUMENTS -----------

@app.post("/documents/upload", response_model=DocumentOut)
async def upload_document(
    driver_id: str = Form(...),
    title: str = Form(""),
    file: UploadFile = File(...),
):
    # limiter aux pdf / images
    if not (
        file.content_type.startswith("image/")
        or file.content_type == "application/pdf"
    ):
        raise HTTPException(
            status_code=400,
            detail="Format non supporté (seulement images ou PDF).",
        )

    doc_id = str(uuid.uuid4())
    _, ext = os.path.splitext(file.filename or "")
    ext = ext or ".bin"
    stored_name = f"{doc_id}{ext}"
    path = os.path.join(UPLOAD_DIR, stored_name)

    # sauvegarde fichier
    with open(path, "wb") as f:
        content = await file.read()
        f.write(content)

    now = datetime.utcnow().isoformat()

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
def list_documents(driver_id: Optional[str] = None):
    conn = get_db()
    cur = conn.cursor()

    if driver_id:
        cur.execute(
            "SELECT * FROM documents WHERE driver_id = ? ORDER BY created_at DESC",
            (driver_id,),
        )
    else:
        cur.execute(
            "SELECT * FROM documents ORDER BY created_at DESC"
        )

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

    # on renvoie le fichier
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=row["original_name"] or "document",
    )


@app.get("/health")
def health():
    """
    Petit endpoint pour vérifier que le serveur tourne.
    """
    return {"status": "ok"}
