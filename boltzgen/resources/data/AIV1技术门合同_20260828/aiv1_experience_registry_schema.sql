PRAGMA foreign_keys = ON;

-- Status: AIV1_BOOTSTRAP_SCHEMA_PARTIAL. This is the smallest executable
-- AIV1 denominator/event skeleton, not the complete AIV2-AIV4 experience
-- registry. A versioned migration is required before AIV2.

-- Identity rows are immutable. Progress is represented by append-only events,
-- never by updating a historical status cell.
CREATE TABLE campaign (
    campaign_id TEXT PRIMARY KEY,
    parent_campaign_id TEXT REFERENCES campaign(campaign_id),
    campaign_type TEXT NOT NULL CHECK (campaign_type = 'AIV1_TECHNICAL_GATE'),
    stage TEXT NOT NULL CHECK (stage = 'AIV1'),
    input_snapshot_sha256 TEXT NOT NULL
        CHECK (length(input_snapshot_sha256) = 64 AND input_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'),
    partition_policy_sha256 TEXT NOT NULL
        CHECK (length(partition_policy_sha256) = 64 AND partition_policy_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at_utc TEXT NOT NULL
) STRICT;

CREATE TABLE campaign_event (
    event_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    event_type TEXT NOT NULL,
    status_code TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL
        CHECK (length(evidence_sha256) = 64 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
    supersedes_event_id TEXT REFERENCES campaign_event(event_id),
    created_at_utc TEXT NOT NULL,
    UNIQUE (campaign_id, event_order)
) STRICT;

CREATE TABLE task (
    task_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
    generation_cell_id TEXT NOT NULL,
    shard_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    full_sequence_sha256 TEXT NOT NULL
        CHECK (length(full_sequence_sha256) = 64 AND full_sequence_sha256 NOT GLOB '*[^0-9a-f]*'),
    target_state_id TEXT NOT NULL,
    target_identity TEXT NOT NULL,
    independence_group TEXT NOT NULL,
    conformer_id TEXT NOT NULL,
    data_partition TEXT NOT NULL
        CHECK (data_partition IN ('positive_compact', 'tuning_challenge')),
    panel_role TEXT NOT NULL CHECK (panel_role IN (
        'positive_primary',
        'positive_fixed_control',
        'positive_compact_medoid',
        'tuning_primary_truncation',
        'tuning_family_glp2'
    )),
    compact_cluster_weight INTEGER,
    fold_run INTEGER NOT NULL CHECK (fold_run = 1),
    expected INTEGER NOT NULL CHECK (expected = 1),
    execution_mode TEXT NOT NULL CHECK (execution_mode = 'REFOLD_REQUIRED'),
    expected_sample_count INTEGER NOT NULL CHECK (expected_sample_count = 5),
    task_contract_sha256 TEXT NOT NULL
        CHECK (length(task_contract_sha256) = 64 AND task_contract_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (
        (panel_role = 'positive_compact_medoid' AND compact_cluster_weight IN (4, 6, 10))
        OR (panel_role != 'positive_compact_medoid' AND compact_cluster_weight IS NULL)
    ),
    UNIQUE (campaign_id, candidate_id, target_state_id, fold_run)
) STRICT;

CREATE TABLE task_attempt_event (
    attempt_event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task(task_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    status_code TEXT NOT NULL,
    output_manifest_sha256 TEXT CHECK (
        output_manifest_sha256 IS NULL OR
        (length(output_manifest_sha256) = 64 AND output_manifest_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    supersedes_event_id TEXT REFERENCES task_attempt_event(attempt_event_id),
    created_at_utc TEXT NOT NULL,
    UNIQUE (task_id, attempt_number, event_order)
) STRICT;

-- This wide fact table must close at exactly 800 rows for a complete AIV1 run.
CREATE TABLE sample_result (
    sample_result_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task(task_id),
    fold_run INTEGER NOT NULL CHECK (fold_run = 1),
    sample_index INTEGER NOT NULL CHECK (sample_index BETWEEN 0 AND 4),
    result_status TEXT NOT NULL CHECK (
        result_status IN ('SUCCESS', 'FAILED', 'MISSING_EXPECTED')
    ),
    raw_artifact_sha256 TEXT CHECK (
        raw_artifact_sha256 IS NULL OR
        (length(raw_artifact_sha256) = 64 AND raw_artifact_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    atom_mapping_sha256 TEXT CHECK (
        atom_mapping_sha256 IS NULL OR
        (length(atom_mapping_sha256) = 64 AND atom_mapping_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    created_at_utc TEXT NOT NULL,
    UNIQUE (task_id, fold_run, sample_index),
    CHECK (
        result_status != 'SUCCESS'
        OR (raw_artifact_sha256 IS NOT NULL AND atom_mapping_sha256 IS NOT NULL)
    )
) STRICT;

-- Metrics are long-form: row count equals 800 multiplied by metric count,
-- so this table is never used as the AIV1 sample-row denominator.
CREATE TABLE metric_sample (
    sample_result_id TEXT NOT NULL REFERENCES sample_result(sample_result_id),
    metric_id TEXT NOT NULL,
    metric_value REAL,
    unit TEXT NOT NULL,
    missing_reason TEXT,
    algorithm_sha256 TEXT NOT NULL
        CHECK (length(algorithm_sha256) = 64 AND algorithm_sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (sample_result_id, metric_id),
    CHECK ((metric_value IS NULL) <> (missing_reason IS NULL))
) STRICT;

CREATE TABLE experience_event (
    experience_event_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    primary_code TEXT NOT NULL,
    secondary_code TEXT,
    attribution_confidence TEXT NOT NULL
        CHECK (attribution_confidence IN ('observed', 'probable', 'unknown')),
    evidence_sha256 TEXT NOT NULL
        CHECK (length(evidence_sha256) = 64 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
    decision TEXT NOT NULL,
    supersedes_event_id TEXT REFERENCES experience_event(experience_event_id),
    created_at_utc TEXT NOT NULL,
    UNIQUE (campaign_id, event_order)
) STRICT;

CREATE TRIGGER campaign_no_update BEFORE UPDATE ON campaign
BEGIN SELECT RAISE(ABORT, 'append-only: campaign UPDATE forbidden'); END;
CREATE TRIGGER campaign_no_delete BEFORE DELETE ON campaign
BEGIN SELECT RAISE(ABORT, 'append-only: campaign DELETE forbidden'); END;
CREATE TRIGGER campaign_event_no_update BEFORE UPDATE ON campaign_event
BEGIN SELECT RAISE(ABORT, 'append-only: campaign_event UPDATE forbidden'); END;
CREATE TRIGGER campaign_event_no_delete BEFORE DELETE ON campaign_event
BEGIN SELECT RAISE(ABORT, 'append-only: campaign_event DELETE forbidden'); END;
CREATE TRIGGER task_no_update BEFORE UPDATE ON task
BEGIN SELECT RAISE(ABORT, 'append-only: task UPDATE forbidden'); END;
CREATE TRIGGER task_no_delete BEFORE DELETE ON task
BEGIN SELECT RAISE(ABORT, 'append-only: task DELETE forbidden'); END;
CREATE TRIGGER task_attempt_event_no_update BEFORE UPDATE ON task_attempt_event
BEGIN SELECT RAISE(ABORT, 'append-only: task_attempt_event UPDATE forbidden'); END;
CREATE TRIGGER task_attempt_event_no_delete BEFORE DELETE ON task_attempt_event
BEGIN SELECT RAISE(ABORT, 'append-only: task_attempt_event DELETE forbidden'); END;
CREATE TRIGGER sample_result_no_update BEFORE UPDATE ON sample_result
BEGIN SELECT RAISE(ABORT, 'append-only: sample_result UPDATE forbidden'); END;
CREATE TRIGGER sample_result_no_delete BEFORE DELETE ON sample_result
BEGIN SELECT RAISE(ABORT, 'append-only: sample_result DELETE forbidden'); END;
CREATE TRIGGER metric_sample_no_update BEFORE UPDATE ON metric_sample
BEGIN SELECT RAISE(ABORT, 'append-only: metric_sample UPDATE forbidden'); END;
CREATE TRIGGER metric_sample_no_delete BEFORE DELETE ON metric_sample
BEGIN SELECT RAISE(ABORT, 'append-only: metric_sample DELETE forbidden'); END;
CREATE TRIGGER experience_event_no_update BEFORE UPDATE ON experience_event
BEGIN SELECT RAISE(ABORT, 'append-only: experience_event UPDATE forbidden'); END;
CREATE TRIGGER experience_event_no_delete BEFORE DELETE ON experience_event
BEGIN SELECT RAISE(ABORT, 'append-only: experience_event DELETE forbidden'); END;
