from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Dict, List

app = FastAPI()

# --------- Modèles ----------

class UpdateLocation(BaseModel):
    driver_id: str
    latitude: float
    longitude: float
    status: str

# Dictionnaire en mémoire : driver_id -> données
drivers: Dict[str, dict] = {}


# --------- Routes ----------

@app.get("/")
def root():
    return {"status": "Server is running"}

@app.post("/update-location")
def update_location(payload: UpdateLocation):
    """Reçoit la position d’un chauffeur et la stocke en mémoire."""
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
def get_drivers() -> List[dict]:
    """Renvoie la liste de tous les chauffeurs connus."""
    return list(drivers.values())
