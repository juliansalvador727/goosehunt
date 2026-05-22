"""Keyword scorer: reads config/roles.yaml, scores all postings, writes score_* columns."""

import sqlite3
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config" / "roles.yaml"
DB_PATH = Path(__file__).parent.parent / "data" / "postings.db"

ROLES = ["firmware", "hardware", "software", "ai_ml"]


def load_keywords(path: Path) -> dict[str, list[str]]:
    with path.open(encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    return {role: [kw.lower() for kw in config[role]["keywords"]] for role in ROLES}


def build_text(row: sqlite3.Row) -> str:
    fields = [row["title"], row["org"], row["summary"],
              row["responsibilities"], row["required_skills"]]
    return " ".join(f or "" for f in fields).lower()


def count_hits(text: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw in text)


def main() -> None:
    if not CONFIG_PATH.exists():
        print(f"Error: {CONFIG_PATH} not found.")
        raise SystemExit(1)
    if not DB_PATH.exists():
        print(f"Error: {DB_PATH} not found. Run `make ingest` first.")
        raise SystemExit(1)

    keywords = load_keywords(CONFIG_PATH)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT job_id, title, org, summary, responsibilities, required_skills "
            "FROM postings"
        ).fetchall()

        # Raw hit counts: {role: [count, ...]} parallel to rows
        raw: dict[str, list[int]] = {role: [] for role in ROLES}
        for row in rows:
            text = build_text(row)
            for role in ROLES:
                raw[role].append(count_hits(text, keywords[role]))

        # Normalize per role to [0, 1]
        scores: dict[str, list[float]] = {}
        for role in ROLES:
            role_max = max(raw[role]) if raw[role] else 0
            if role_max == 0:
                scores[role] = [0.0] * len(rows)
            else:
                scores[role] = [c / role_max for c in raw[role]]

        # Write back
        conn.executemany(
            """UPDATE postings SET
                score_firmware = :firmware,
                score_hardware = :hardware,
                score_software = :software,
                score_ai_ml    = :ai_ml
               WHERE job_id = :job_id""",
            [
                {
                    "job_id":   rows[i]["job_id"],
                    "firmware": scores["firmware"][i],
                    "hardware": scores["hardware"][i],
                    "software": scores["software"][i],
                    "ai_ml":    scores["ai_ml"][i],
                }
                for i in range(len(rows))
            ],
        )
        conn.commit()

    # Summary: top hit count per role
    print(f"Scored {len(rows)} postings.")
    print(f"{'Role':<12}  {'Max hits':>8}  {'Non-zero':>8}")
    print("-" * 32)
    for role in ROLES:
        nonzero = sum(1 for c in raw[role] if c > 0)
        print(f"score_{role:<8}  {max(raw[role]):>8}  {nonzero:>8}")


if __name__ == "__main__":
    main()
