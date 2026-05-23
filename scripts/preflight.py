"""Preflight checks for goosehunt command-line workflows."""

import argparse
from pathlib import Path

ROOT = Path(__file__).parent.parent
RESUME_PATH = ROOT / "resume.pdf"
JSONL_PATH = ROOT / "data" / "postings.jsonl"


def require_file(path: Path, label: str, hint: str) -> bool:
    if not path.exists():
        print(f"Error: {label} not found at {path}.")
        print(f"Hint: {hint}")
        return False
    if not path.is_file():
        print(f"Error: {label} path exists but is not a file: {path}.")
        print(f"Hint: {hint}")
        return False
    if path.stat().st_size == 0:
        print(f"Error: {label} is empty: {path}.")
        print(f"Hint: {hint}")
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate required goosehunt inputs.")
    parser.add_argument("--resume", action="store_true", help="Require resume.pdf")
    parser.add_argument("--jsonl", action="store_true", help="Require scraped postings JSONL")
    args = parser.parse_args()

    ok = True
    if args.resume:
        ok &= require_file(
            RESUME_PATH,
            "resume PDF",
            "copy your resume to ./resume.pdf before running scoring or the full pipeline",
        )
    if args.jsonl:
        ok &= require_file(
            JSONL_PATH,
            "scraped postings JSONL",
            "run `make scrape` first, then rerun the pipeline",
        )

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
