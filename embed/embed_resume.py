"""Embed resume PDF and write cosine similarity scores to score_resume column."""

import sqlite3
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

from resume.parser import extract_text

MODEL_NAME = "all-MiniLM-L6-v2"
RESUME_PATH = Path(__file__).parent.parent / "resume.pdf"
DB_PATH = Path(__file__).parent.parent / "data" / "postings.db"


def main() -> None:
    if not RESUME_PATH.exists():
        print(f"Error: {RESUME_PATH} not found. Drop your resume PDF there first.")
        raise SystemExit(1)
    if not DB_PATH.exists():
        print(f"Error: {DB_PATH} not found. Run `make ingest` first.")
        raise SystemExit(1)

    print("Extracting resume text...")
    resume_text = extract_text(RESUME_PATH)
    if not resume_text:
        print("Error: extracted no text from resume PDF.")
        raise SystemExit(1)
    print(f"  {len(resume_text)} characters extracted.")

    print(f"Loading {MODEL_NAME}...")
    model = SentenceTransformer(MODEL_NAME)

    resume_vec = model.encode(resume_text, convert_to_numpy=True).astype(np.float32)
    # Normalize in case model config changes; sentence-transformers normalizes by default
    resume_vec /= np.linalg.norm(resume_vec)

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT job_id, embedding FROM postings WHERE embedding IS NOT NULL"
        ).fetchall()

        if not rows:
            print("No embeddings found. Run `make embed` first.")
            raise SystemExit(1)

        job_ids = [r[0] for r in rows]
        # Stack into (N, 384) matrix
        matrix = np.stack(
            [np.frombuffer(r[1], dtype=np.float32) for r in rows]
        )
        # Normalize rows (should already be unit vectors, but be safe)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix /= np.where(norms == 0, 1, norms)

        # Cosine similarity = dot product of unit vectors
        scores = (matrix @ resume_vec).tolist()

        conn.executemany(
            "UPDATE postings SET score_resume = ? WHERE job_id = ?",
            zip(scores, job_ids),
        )
        conn.commit()

    top5 = sorted(zip(scores, job_ids), reverse=True)[:5]
    print(f"\nScored {len(rows)} postings against resume.")
    print("Top 5:")
    with sqlite3.connect(DB_PATH) as conn:
        for score, job_id in top5:
            row = conn.execute(
                "SELECT title, org FROM postings WHERE job_id = ?", (job_id,)
            ).fetchone()
            print(f"  {score:.3f}  {row[0][:55]}  |  {row[1][:25]}")


if __name__ == "__main__":
    main()
