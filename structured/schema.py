"""四张元数据表和 data_job。字段以设计第 10.1、第 11 节为准。"""

from __future__ import annotations

from psycopg import Connection

METADATA_DDL = """
CREATE TABLE IF NOT EXISTS doc_info (
    import_version_id text PRIMARY KEY,
    doc_id text NOT NULL,
    source_file text NOT NULL,
    format text NOT NULL,
    summary text,
    minio_key text NOT NULL,
    checksum text NOT NULL,
    sheet_name text,
    upload_task_id text,
    status text NOT NULL,
    data_version_id text,
    fields_snapshot jsonb,
    generated_by text,
    review_status text NOT NULL,
    created_at timestamptz NOT NULL,
    CONSTRAINT doc_info_status CHECK (status IN ('pending', 'indexing', 'ready', 'archived', 'failed')),
    CONSTRAINT doc_info_review CHECK (review_status IN ('pending_review', 'confirmed')),
    CONSTRAINT doc_info_source CHECK (source_file <> ''),
    CONSTRAINT doc_info_format CHECK (format <> ''),
    CONSTRAINT doc_info_key CHECK (minio_key <> ''),
    CONSTRAINT doc_info_checksum CHECK (checksum <> '')
);
CREATE UNIQUE INDEX IF NOT EXISTS doc_info_one_ready ON doc_info (doc_id) WHERE status = 'ready';

CREATE TABLE IF NOT EXISTS table_info (
    table_id text NOT NULL,
    import_version_id text NOT NULL REFERENCES doc_info (import_version_id),
    doc_id text NOT NULL,
    table_name text NOT NULL,
    role text,
    summary text,
    fields_snapshot jsonb,
    generated_by text,
    review_status text NOT NULL,
    PRIMARY KEY (table_id, import_version_id),
    CONSTRAINT table_info_role CHECK (role IS NULL OR role IN ('事实表', '维度表')),
    CONSTRAINT table_info_review CHECK (review_status IN ('pending_review', 'confirmed'))
);

CREATE TABLE IF NOT EXISTS column_info (
    column_id text NOT NULL,
    import_version_id text NOT NULL REFERENCES doc_info (import_version_id),
    table_id text NOT NULL,
    column_name text NOT NULL,
    position integer NOT NULL,
    data_type text NOT NULL,
    role text,
    aliases jsonb,
    description text,
    value_index boolean NOT NULL DEFAULT false,
    generated_by text,
    review_status text NOT NULL,
    PRIMARY KEY (column_id, import_version_id),
    CONSTRAINT column_info_role CHECK (role IS NULL OR role IN ('维度', '度量', '时间', '主键', '属性')),
    CONSTRAINT column_info_review CHECK (review_status IN ('pending_review', 'confirmed')),
    UNIQUE (table_id, import_version_id, column_name),
    UNIQUE (table_id, import_version_id, position)
);

CREATE TABLE IF NOT EXISTS metric_info (
    metric_id text NOT NULL,
    import_version_id text NOT NULL REFERENCES doc_info (import_version_id),
    table_id text NOT NULL,
    name text NOT NULL,
    summary text,
    aliases jsonb,
    column_ids jsonb NOT NULL,
    formula_tree jsonb NOT NULL,
    formula_text text,
    status text NOT NULL,
    review_status text NOT NULL,
    PRIMARY KEY (metric_id, import_version_id),
    CONSTRAINT metric_info_status CHECK (status IN ('draft', 'approved')),
    CONSTRAINT metric_info_review CHECK (review_status IN ('pending_review', 'confirmed')),
    CONSTRAINT metric_info_name CHECK (name <> '')
);

CREATE TABLE IF NOT EXISTS data_job (
    job_id text PRIMARY KEY,
    name text NOT NULL,
    sop_ref text NOT NULL,
    description text,
    code_id text,
    run_at time NOT NULL,
    schedule_kind text NOT NULL DEFAULT 'daily',
    schedule_day integer,
    skill_id text,
    doc_ids text[] NOT NULL,
    enabled boolean NOT NULL,
    last_run_at timestamptz,
    last_status text NOT NULL,
    last_error text,
    last_success_on date,
    CONSTRAINT data_job_name CHECK (name <> ''),
    CONSTRAINT data_job_sop CHECK (sop_ref <> ''),
    CONSTRAINT data_job_status CHECK (last_status IN ('never', 'running', 'succeeded', 'failed')),
    CONSTRAINT data_job_schedule CHECK (schedule_kind IN ('daily', 'weekly', 'monthly'))
);

CREATE TABLE IF NOT EXISTS code_version (
    version_id text PRIMARY KEY,
    code_id text NOT NULL,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    doc_id text,
    minio_key text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT code_version_status CHECK (status IN ('pending', 'ready', 'archived')),
    CONSTRAINT code_version_name CHECK (name <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS code_version_one_ready
    ON code_version (code_id) WHERE status = 'ready';

CREATE TABLE IF NOT EXISTS user_skill (
    skill_id text PRIMARY KEY,
    name text NOT NULL,
    summary text NOT NULL DEFAULT '',
    instructions text NOT NULL,
    code_id text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_skill_name CHECK (name <> ''),
    CONSTRAINT user_skill_instructions CHECK (instructions <> '')
);
"""


def ensure_schema(conn: Connection) -> None:
    from psycopg.pq import TransactionStatus

    started_idle = conn.info.transaction_status == TransactionStatus.IDLE
    found = conn.execute("SELECT to_regclass('public.doc_info') AS name").fetchone()
    if found and found["name"]:
        if not started_idle:
            return
        conn.rollback()
        _ensure_extensions(conn)
        return
    for statement in METADATA_DDL.split(";"):
        sql_text = statement.strip()
        if sql_text:
            conn.execute(sql_text)
    conn.commit()
    _ensure_extensions(conn)


def _ensure_extensions(conn: Connection) -> None:
    conn.execute("ALTER TABLE data_job ADD COLUMN IF NOT EXISTS schedule_kind text NOT NULL DEFAULT 'daily'")
    conn.execute("ALTER TABLE data_job ADD COLUMN IF NOT EXISTS schedule_day integer")
    conn.execute("ALTER TABLE data_job ADD COLUMN IF NOT EXISTS skill_id text")
    conn.execute("ALTER TABLE data_job ADD COLUMN IF NOT EXISTS run_started_at timestamptz")
    conn.execute("ALTER TABLE data_job ADD COLUMN IF NOT EXISTS last_duration_seconds integer")
    conn.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE data_job
                ADD CONSTRAINT data_job_schedule
                CHECK (schedule_kind IN ('daily', 'weekly', 'monthly'));
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS code_version (
            version_id text PRIMARY KEY,
            code_id text NOT NULL,
            name text NOT NULL,
            description text NOT NULL DEFAULT '',
            doc_id text,
            minio_key text NOT NULL,
            status text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT code_version_status CHECK (status IN ('pending', 'ready', 'archived')),
            CONSTRAINT code_version_name CHECK (name <> '')
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS code_version_one_ready
            ON code_version (code_id) WHERE status = 'ready'
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS library_folder (
            folder_id text PRIMARY KEY,
            name text NOT NULL,
            parent_id text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT library_folder_name CHECK (name <> '')
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS library_placement (
            item_kind text NOT NULL,
            item_id text NOT NULL,
            folder_id text NOT NULL DEFAULT '',
            PRIMARY KEY (item_kind, item_id),
            CONSTRAINT library_placement_kind CHECK (item_kind IN ('file', 'code', 'upload'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_skill (
            skill_id text PRIMARY KEY,
            name text NOT NULL,
            summary text NOT NULL DEFAULT '',
            instructions text NOT NULL,
            code_id text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT user_skill_name CHECK (name <> ''),
            CONSTRAINT user_skill_instructions CHECK (instructions <> '')
        )
        """
    )
    conn.commit()
