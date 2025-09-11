
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from kafka_services import send_to_kafka, collection
from uuid import uuid4
import shutil
import os

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/upload")
async def upload_audio(file: UploadFile = File(...)):
    note_id = str(uuid4())
    file_path = f"tmp/{note_id}.m4a"
    os.makedirs("tmp", exist_ok=True)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    translated_text = "This is the translated text from the audio."
    send_to_kafka("translated-text", {"note_id": note_id, "text": translated_text})

    return {"note_id": note_id}

@app.get("/summaries/{note_id}")
def get_summary(note_id: str):
    result = collection.find_one({"note_id": note_id})
    return {"summary": result["summary"] if result else "Summary not found."}
