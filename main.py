from fastapi import FastAPI
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

class JobRequest(BaseModel):
    driver_id: str
    message: str

# positions des chauffeurs
drivers: Dict[str, Dict] = {}

# courses en attente pour chaque chauffeur
jobs: Dict[str, Dict] = {}

# ---------- Routes ----------

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

# ---------- Gestion des courses ----------

@app.post("/assign-job")
def assign_job(job: JobRequest):
    """La centrale assigne une course à un chauffeur."""
    jobs[job.driver_id] = {
        "message": job.message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    print(f"Nouvelle course pour {job.driver_id} :", jobs[job.driver_id])
    return {"ok": True}

@app.get("/job/{driver_id}")
def get_job(driver_id: str):
    """Le chauffeur vient voir s'il a une course."""
    job = jobs.get(driver_id)
    if not job:
        return {"has_job": False}
    return {"has_job": True, **job}

@app.post("/job/{driver_id}/clear")
def clear_job(driver_id: str):
    """Le chauffeur accepte / refuse -> on efface la course côté serveur."""
    jobs.pop(driver_id, None)
    return {"ok": True}
