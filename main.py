from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Dict, List, Literal
from uuid import uuid4

app = FastAPI()

# ---------------------------
# Modèle pour la position
# ---------------------------

class UpdateLocation(BaseModel):
    driver_id: str
    latitude: float
    longitude: float
    status: str # "online" / "offline" par ex.

# driver_id -> données du chauffeur
drivers: Dict[str, Dict] = {}


@app.get("/")
def root():
    return {"status": "Server is running"}


@app.post("/update-location")
def update_location(payload: UpdateLocation):
    drivers[payload.driver_id] = {
        "id": payload.driver_id,
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "status": payload.status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    print("Nouvelles données :", drivers[payload.driver_id])
    return {"ok": True}


@app.get("/drivers")
def get_drivers() -> List[Dict]:
    """
    Retourne la liste de TOUS les chauffeurs connectés.
    """
    return list(drivers.values())

# ---------------------------
# Modèles pour les courses
# ---------------------------

# Ce que la centrale envoie quand elle crée une course
class NewJob(BaseModel):
    driver_id: str # taxi01, taxi02, ...
    client_name: str # Nom du client
    pickup_address: str # Adresse de prise en charge
    dropoff_address: str | None = None # Destination (optionnel)
    phone: str | None = None # Téléphone client
    comment: str | None = None # Commentaire


# Pour changer le statut d'une course
class JobStatusUpdate(BaseModel):
    status: Literal["pending", "in_progress", "done"]


# job_id -> données de la course
jobs: Dict[str, Dict] = {}


@app.post("/jobs")
def create_job(payload: NewJob):
    """
    Création d'une nouvelle course par la centrale.
    Statut de départ : pending (en attente).
    """
    job_id = str(uuid4())

    job_data = {
        "id": job_id,
        "driver_id": payload.driver_id,
        "client_name": payload.client_name,
        "pickup_address": payload.pickup_address,
        "dropoff_address": payload.dropoff_address,
        "phone": payload.phone,
        "comment": payload.comment,
        "status": "pending", # en attente
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).
