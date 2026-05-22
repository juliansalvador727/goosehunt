CREATE TABLE IF NOT EXISTS postings (
    job_id            TEXT PRIMARY KEY,
    board_type        TEXT,
    title             TEXT,
    org               TEXT,
    location          TEXT,
    deadline          TEXT,
    deadline_iso      TEXT,
    work_term         TEXT,
    openings          INTEGER,
    summary           TEXT,
    responsibilities  TEXT,
    required_skills   TEXT,
    raw_fields_json   TEXT,
    scraped_at        TEXT,
    updated_at        TEXT,
    embedding         BLOB,
    score_firmware          REAL,
    score_embedded          REAL,
    score_hardware          REAL,
    score_software          REAL,
    score_fde               REAL,
    score_mts               REAL,
    score_power_electronics REAL,
    score_resume            REAL
);

-- Reserved for on-demand LLM fit evaluations, keyed by (resume_hash, job_id).
-- Not used in v1 of the UI.
CREATE TABLE IF NOT EXISTS llm_evals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_hash  TEXT NOT NULL,
    job_id       TEXT NOT NULL REFERENCES postings(job_id),
    eval_json    TEXT,
    created_at   TEXT,
    UNIQUE(resume_hash, job_id)
);
