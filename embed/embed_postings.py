"""Embed postings with all-MiniLM-L6-v2 → BLOB in SQLite."""

import argparse
import sqlite3
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

MODEL_NAME = "all-MiniLM-L6-v2"
DB_PATH = Path(__file__).parent.parent / "data" / "postings.db"
BATCH_SIZE = 64


def build_text(title: str | None, summary: str | None,
               responsibilities: str | None, required_skills: str | None) -> str:
    t = title or ""
    return " ".join([t, t, t, summary or "", responsibilities or "", required_skills or ""]).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed postings into SQLite BLOB column.")
    parser.add_argument("--force", action="store_true",
                        help="Re-embed all rows, not just those with embedding IS NULL")
    parser.add_argument("--db", default=str(DB_PATH), metavar="PATH",
                        help="Path to SQLite DB (default: data/postings.db)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: {db_path} not found. Run `make ingest` first.")
        raise SystemExit(1)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        query = (
            "SELECT job_id, title, summary, responsibilities, required_skills FROM postings"
            if args.force else
            "SELECT job_id, title, summary, responsibilities, required_skills "
            "FROM postings WHERE embedding IS NULL"
        )
        rows = conn.execute(query).fetchall()

        total = conn.execute("SELECT count(*) FROM postings").fetchone()[0]
        skipped = total - len(rows)

        if not rows:
            print(f"Nothing to embed — all {total} rows already have embeddings.")
            return

        print(f"Loading {MODEL_NAME}...")
        model = SentenceTransformer(MODEL_NAME)

        job_ids = [r["job_id"] for r in rows]
        texts = [
            build_text(r["title"], r["summary"], r["responsibilities"], r["required_skills"])
            for r in rows
        ]

        print(f"Embedding {len(texts)} postings (batch_size={BATCH_SIZE})...")
        vecs = model.encode(
            texts,
            batch_size=BATCH_SIZE,
            show_progress_bar=True,
            convert_to_numpy=True,
        ).astype(np.float32)

        conn.executemany(
            "UPDATE postings SET embedding = ? WHERE job_id = ?",
            ((vec.tobytes(), job_id) for job_id, vec in zip(job_ids, vecs)),
        )
        conn.commit()

    print(f"Done.  Embedded: {len(rows)}  Skipped (already set): {skipped}")


if __name__ == "__main__":
    main()
