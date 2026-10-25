"""Audio file storage on a local (or shared, volume-mounted) directory."""

from pathlib import Path

from fastapi import UploadFile

CHUNK_SIZE = 1024 * 1024


class FileTooLargeError(Exception):
    pass


class AudioStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        # Names are generated server-side, but never let one escape the storage root.
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"Invalid storage name: {name!r}")
        return path

    async def save(self, name: str, upload: UploadFile, max_bytes: int) -> int:
        """Stream an upload to disk, enforcing `max_bytes`. Returns the number of bytes written."""
        path = self.path(name)
        written = 0
        try:
            with path.open("wb") as out:
                while chunk := await upload.read(CHUNK_SIZE):
                    written += len(chunk)
                    if written > max_bytes:
                        raise FileTooLargeError
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return written

    def delete(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
