from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Autoriser toutes les connexions (Android, iPhone, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

drivers = {}

@app.get("/")
def home():
    return {"status": "Server is running"}

@app.post("/update_position")
def update_position(driver_id: str, lat: float, lon: float, status: str):
    drivers[driver_id] = {"lat": lat, "lon": lon, "status": status}
    return {"ok": True}

@app.get("/drivers")
def get_drivers():
    return drivers

