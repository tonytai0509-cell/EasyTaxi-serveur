from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"status": "Server is running"}

@app.post("/update-location")
def update_location(data: dict):
    print("New data:", data)
    return {"ok": True}
