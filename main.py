from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Dict, List

app = FastAPI()


class UpdateLocation(BaseModel):
    driver_id: str
    latitude: float
    longitude: float
    status: str


drivers: Dict[str, dict] = {}


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
def get_drivers() -> List[dict]:
    return list(drivers.values())
