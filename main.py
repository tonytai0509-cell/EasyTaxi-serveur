from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Dict, List, Optional

app = FastAPI()

# ---------- Modèles ----------

class UpdateLocation(BaseModel):
    driver_id: str
    latitude: float
    longitude: float
    status: str

class Job(BaseModel):
    driver_id: str
    customer_name: str
    address: str
    phone: str
    comment: Optional[str] = ""

class JobStatusUpdate(BaseModel):
    status: str # "new", "accepted", "done", etc.

# ---------- Données en mémoire ----------

drivers: Dict[str, Dict] = {}
jobs: Dict[str, Dict] = {} # clé = driver_id

# ---------- Routes existantes ----------

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
    return list(drivers.values())

# ---------- 🎯 Nouvelles routes courses ----------

@app.post("/send-job")
def send_job(payload: Job):
    job = {
        "driver_id": payload.driver_id,
        "customer_name": payload.customer_name,
        "address": payload.address,
        "phone": payload.phone,
        "comment": payload.comment or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "new",
    }
    jobs[payload.driver_id] = job
    print("Nouvelle course :", job)
    return {"ok": True}

@app.get("/job/{driver_id}")
def get_job(driver_id: str):
    # Renvoie {"job": {...}} ou {"job": None}
    return {"job": jobs.get(driver_id)}

@app.post("/job/{driver_id}/status")
def update_job_status(driver_id: str, payload: JobStatusUpdate):
    if driver_id not in jobs:
        raise HTTPException(status_code=404, detail="Aucune course pour ce chauffeur")
    jobs[driver_id]["status"] = payload.status
    # Si on met "done", on supprime la course
    if payload.status == "done":
        jobs.pop(driver_id, None)
    return {"ok": True}
