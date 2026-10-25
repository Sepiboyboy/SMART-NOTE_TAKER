from fastapi.testclient import TestClient

from notetaker.config import Settings
from notetaker.models import NoteStatus

from .conftest import InMemoryNoteRepository, RecordingBus


def upload(client: TestClient, name: str = "standup.mp3", data: bytes = b"fake-audio"):
    return client.post("/api/notes", files={"file": (name, data, "audio/mpeg")})


def test_health(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_index_serves_web_ui(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "AI Note Taker" in response.text


def test_upload_stores_note_and_publishes_event(
    client: TestClient, repo: InMemoryNoteRepository, bus: RecordingBus, settings: Settings
) -> None:
    response = upload(client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == NoteStatus.UPLOADED
    assert body["filename"] == "standup.mp3"
    assert body["size_bytes"] == len(b"fake-audio")
    assert "audio_file" not in body  # Internal field must not leak.

    note = repo.notes[body["id"]]
    assert (settings.storage_dir / note.audio_file).read_bytes() == b"fake-audio"
    assert [(topic, e.note_id) for topic, e in bus.events] == [(settings.topic_uploaded, note.id)]


def test_upload_strips_directories_from_filename(
    client: TestClient, repo: InMemoryNoteRepository
) -> None:
    body = upload(client, name="../../etc/evil.wav").json()
    assert body["filename"] == "evil.wav"


def test_upload_rejects_unsupported_type(client: TestClient, bus: RecordingBus) -> None:
    response = upload(client, name="notes.txt")
    assert response.status_code == 415
    assert bus.events == []


def test_upload_rejects_empty_file(client: TestClient, settings: Settings) -> None:
    assert upload(client, data=b"").status_code == 400
    assert list(settings.storage_dir.iterdir()) == []


def test_upload_rejects_oversized_file(client: TestClient, settings: Settings) -> None:
    response = upload(client, data=b"x" * (settings.max_upload_bytes + 1))
    assert response.status_code == 413
    assert list(settings.storage_dir.iterdir()) == []


def test_get_and_list_notes(client: TestClient) -> None:
    first = upload(client, name="a.mp3").json()
    second = upload(client, name="b.mp3").json()

    assert client.get(f"/api/notes/{first['id']}").json()["filename"] == "a.mp3"
    listed = [n["id"] for n in client.get("/api/notes").json()]
    assert listed == [second["id"], first["id"]]  # Newest first.


def test_get_missing_note_returns_404(client: TestClient) -> None:
    assert client.get("/api/notes/does-not-exist").status_code == 404


def test_delete_note_removes_audio(client: TestClient, settings: Settings) -> None:
    note_id = upload(client).json()["id"]

    assert client.delete(f"/api/notes/{note_id}").status_code == 204
    assert client.get(f"/api/notes/{note_id}").status_code == 404
    assert list(settings.storage_dir.iterdir()) == []
    assert client.delete(f"/api/notes/{note_id}").status_code == 404
