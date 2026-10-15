from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from insightface.app import FaceAnalysis


class FaceAI:
    """Small wrapper around InsightFace / ArcFace.

    Each person is stored as the average embedding of their photos. A new face is
    matched by cosine similarity against every stored person."""

    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}

    def __init__(
        self,
        people_dir: str | Path = "people",
        database_path: str | Path = "face_database.npy",
        model_name: str = "buffalo_l",
        threshold: float = 0.45,
    ) -> None:
        self.people_dir = Path(people_dir)
        self.database_path = Path(database_path)
        self.threshold = threshold
        self.database: dict[str, dict[str, Any]] = {}

        self.app = FaceAnalysis(
            name=model_name,
            providers=["CPUExecutionProvider"],
        )
        self.app.prepare(ctx_id=0, det_size=(640, 640))

    def load_or_build_database(self, rebuild: bool = False) -> None:
        if self.database_path.exists() and not rebuild:
            self.database = np.load(self.database_path, allow_pickle=True).item()
            print(f"Loaded face database: {self.database_path}")
            return

        self.build_database()

    def build_database(self) -> None:
        self.database = {}
        self.people_dir.mkdir(parents=True, exist_ok=True)

        for person_folder in sorted(self.people_dir.iterdir()):
            if not person_folder.is_dir():
                continue

            info = self._load_person_info(person_folder)
            person_id = info["person_id"]
            embeddings = []

            for image_path in sorted(person_folder.iterdir()):
                if image_path.suffix.lower() not in self.IMAGE_EXTENSIONS:
                    continue

                embedding = self._embedding_from_file(image_path)
                if embedding is None:
                    print(f"No face found: {image_path}")
                    continue

                embeddings.append(embedding)
                print(f"Loaded face: {image_path}")

            if not embeddings:
                print(f"Skipped {person_folder.name}: no valid face images")
                continue

            mean_embedding = np.mean(embeddings, axis=0)
            mean_embedding = mean_embedding / np.linalg.norm(mean_embedding)

            self.database[person_id] = {
                "info": info,
                "embedding": mean_embedding,
            }
            print(f"Enrolled: {info['name']} ({person_id})")

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(self.database_path, self.database)
        print(f"Saved face database: {self.database_path}")

    def recognize_bytes(self, image_bytes: bytes) -> dict[str, Any]:
        image = self._image_from_bytes(image_bytes)
        if image is None:
            return self._reject("invalid_image")

        embedding = self._embedding_from_image(image)
        if embedding is None:
            return self._reject("no_face_detected")

        if not self.database:
            return self._reject("empty_database")

        best_person_id = None
        best_score = -1.0

        for person_id, data in self.database.items():
            score = float(np.dot(embedding, data["embedding"]))
            if score > best_score:
                best_score = score
                best_person_id = person_id

        if best_person_id is None or best_score < self.threshold:
            return self._reject("unknown_person", best_score)

        return {
            "authenticated": True,
            "reason": "match_found",
            "confidence": best_score,
            "person": self.database[best_person_id]["info"],
        }

    def _load_person_info(self, person_folder: Path) -> dict[str, str]:
        info_path = person_folder / "info.json"
        if info_path.exists():
            with info_path.open("r", encoding="utf-8") as file:
                info = json.load(file)
        else:
            info = {}

        person_id = str(info.get("person_id", person_folder.name))
        name = str(info.get("name", person_id))

        return {
            "person_id": person_id,
            "name": name,
        }

    def _embedding_from_file(self, image_path: Path) -> np.ndarray | None:
        image = cv2.imread(str(image_path))
        if image is None:
            print(f"Could not read image: {image_path}")
            return None
        return self._embedding_from_image(image)

    def _embedding_from_image(self, image: np.ndarray) -> np.ndarray | None:
        faces = self.app.get(image)
        if not faces:
            return None

        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        return face.normed_embedding

    @staticmethod
    def _image_from_bytes(image_bytes: bytes) -> np.ndarray | None:
        data = np.frombuffer(image_bytes, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)

    @staticmethod
    def _reject(reason: str, confidence: float = 0.0) -> dict[str, Any]:
        return {
            "authenticated": False,
            "reason": reason,
            "confidence": confidence,
            "person": None,
        }
