"""Relational persistence for one Deployments server instance.

The browser and the rest of the application continue to exchange ordinary
Python dictionaries/JSON. Inside SQLite, durable state is represented as
domain tables with foreign keys, constraints, and indexes; no application
state is stored as an opaque JSON document.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator

from .data_paths import DATABASE_PATH, INSTANCE_ID, LEGACY_RESULTS_DIR


SCHEMA_VERSION = 2
MAX_HISTORY_JOBS = 100
MAX_DRAFTS = 10
MAX_VERIFICATION_ITEMS = 10_000
MAX_PC_TOOLKIT_ITEMS = 10_000

_DB_LOCK = threading.RLock()
_INSTANCES: dict[tuple[str, str], "AppDatabase"] = {}

_BOOL_SETTINGS = (
    "validate_editor_serials",
    "validate_editor_users",
    "validate_bulk_serials",
    "validate_quick_import",
    "validate_workbook_import",
    "save_alm_import_drafts",
    "show_returned_serials_on_hand",
    "start_at_login",
    "headless_auth_enabled",
    "pc_toolkit_enabled",
    "pc_toolkit_auto_connect",
)
_IMPORT_COLUMN_NAMES = (
    "username",
    "deployment_serial",
    "returned_device",
    "pending_return",
    "enabled",
    "device_allocation",
    "new_asset_status",
    "first_name",
    "last_name",
)
_MODEL_STATUS_FIELDS = ("user_status", "location_status")
_PC_PERSON_SLOTS = ("assigned_user", "used_by", "owner", "people", "primary_users", "profile_users")
_MAX_PORTAL_FIELDS = (
    "reference", "helix_id", "request_type", "status", "requested_for",
    "requested_for_name", "requested_by", "requested_by_name",
    "requested_for_location", "workflow_instance_id", "created_at", "updated_at",
    "updated_by", "old_serial", "old_model", "old_manufacturer", "device_type",
    "comments", "detail_loaded", "details_expanded", "match_source", "match_key",
    "matched_username",
)
_REQUEST_STRING_FIELDS = (
    "kind", "status", "user_login", "returning_user_login", "group_name", "source",
    "device_allocation", "first_name", "last_name", "location_city",
    "location_building", "location_floor", "location_room", "location_cabinet",
    "primary_serial", "username", "deployment_date", "current_status",
    "new_asset_status", "import_validation", "import_error",
    "serial_validation", "serial_validation_error", "user_validation",
    "user_validation_error", "returning_user_validation",
    "returning_user_validation_error", "bulk_validation", "bulk_validation_error",
    "bulk_serial_mode", "manual_return_id", "manual_return_serial",
    "manual_return_type", "manual_return_status", "manual_return_auto_status",
    "manual_return_error", "manual_return_lookup_serial", "manual_return_lookup_state",
    "manual_return_lookup_error", "manual_return_model", "manual_return_source_id",
    "manual_username_source_id", "pc_toolkit_suggested_status",
    "pc_toolkit_conflict_level", "pc_toolkit_conflict_message",
)
_REQUEST_BOOL_FIELDS = (
    "returning_requested", "returning_user_selected", "user_selected",
    "cached_serial_verification", "cached_user_verification", "manual_return_dismissed",
    "included", "default_excluded", "backlog_ignored", "attending", "new_joiner",
    "has_returned_device_serial", "has_pending_return_serial",
    "pc_toolkit_default_excluded", "returned_serial_match",
)
_REQUEST_INT_FIELDS = (
    "serial_validation_epoch", "user_validation_epoch", "pc_toolkit_epoch",
    "import_validation_epoch", "backlog_validation_epoch", "alm_row_number",
    "username_occurrence", "username_occurrence_total",
)


_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS app_migrations (
    name TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL,
    source_path TEXT NOT NULL,
    issue_count INTEGER NOT NULL DEFAULT 0 CHECK(issue_count >= 0)
);

CREATE TABLE IF NOT EXISTS migration_issues (
    issue_id INTEGER PRIMARY KEY,
    migration_name TEXT NOT NULL,
    source_file TEXT NOT NULL,
    message TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    concurrency INTEGER NOT NULL CHECK(concurrency BETWEEN 1 AND 200),
    validate_editor_serials INTEGER NOT NULL CHECK(validate_editor_serials IN (0, 1)),
    validate_editor_users INTEGER NOT NULL CHECK(validate_editor_users IN (0, 1)),
    validate_bulk_serials INTEGER NOT NULL CHECK(validate_bulk_serials IN (0, 1)),
    validate_quick_import INTEGER NOT NULL CHECK(validate_quick_import IN (0, 1)),
    validate_workbook_import INTEGER NOT NULL CHECK(validate_workbook_import IN (0, 1)),
    save_alm_import_drafts INTEGER NOT NULL CHECK(save_alm_import_drafts IN (0, 1)),
    show_returned_serials_on_hand INTEGER NOT NULL CHECK(show_returned_serials_on_hand IN (0, 1)),
    update_channel TEXT NOT NULL CHECK(update_channel IN ('stable', 'development')),
    start_at_login INTEGER NOT NULL CHECK(start_at_login IN (0, 1)),
    headless_auth_enabled INTEGER NOT NULL CHECK(headless_auth_enabled IN (0, 1)),
    pc_toolkit_enabled INTEGER NOT NULL CHECK(pc_toolkit_enabled IN (0, 1)),
    pc_toolkit_auto_connect INTEGER NOT NULL CHECK(pc_toolkit_auto_connect IN (0, 1)),
    pc_toolkit_transport TEXT NOT NULL CHECK(pc_toolkit_transport IN ('browser', 'puppeteer', 'api')),
    saved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_request_statuses (
    position INTEGER PRIMARY KEY CHECK(position >= 0),
    status TEXT NOT NULL UNIQUE,
    deployment_kind TEXT NOT NULL CHECK(deployment_kind IN ('user', 'location'))
);
CREATE TABLE IF NOT EXISTS preference_import_columns (
    field_name TEXT PRIMARY KEY,
    heading TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_model_mappings (
    model_key TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    user_status TEXT NOT NULL DEFAULT '',
    location_status TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL UNIQUE CHECK(position >= 0),
    CHECK(user_status <> '' OR location_status <> '')
);

CREATE TABLE IF NOT EXISTS verification_records (
    category TEXT NOT NULL CHECK(category IN ('serials', 'usernames')),
    canonical_key TEXT NOT NULL,
    display_value TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT '',
    verified_at TEXT,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY(category, canonical_key)
);
CREATE INDEX IF NOT EXISTS idx_verification_records_lru
    ON verification_records(category, last_used_at DESC);
CREATE TABLE IF NOT EXISTS verification_aliases (
    category TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    alias_display TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    PRIMARY KEY(category, alias_key),
    FOREIGN KEY(category, canonical_key)
        REFERENCES verification_records(category, canonical_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_verification_aliases_canonical
    ON verification_aliases(category, canonical_key);

CREATE TABLE IF NOT EXISTS alm_imports (
    import_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    content BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK(byte_count = length(content)),
    source_format TEXT NOT NULL CHECK(source_format IN ('excel', 'csv')),
    has_sheets INTEGER NOT NULL CHECK(has_sheets IN (0, 1)),
    supports_formatting INTEGER NOT NULL CHECK(supports_formatting IN (0, 1)),
    default_sheet TEXT NOT NULL DEFAULT '',
    needs_mapping INTEGER NOT NULL DEFAULT 0 CHECK(needs_mapping IN (0, 1)),
    has_inspection INTEGER NOT NULL DEFAULT 0 CHECK(has_inspection IN (0, 1)),
    inspection_default_sheet TEXT NOT NULL DEFAULT '',
    summary_signature TEXT NOT NULL DEFAULT '',
    saved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alm_import_columns (
    import_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    heading TEXT NOT NULL,
    PRIMARY KEY(import_id, field_name),
    FOREIGN KEY(import_id) REFERENCES alm_imports(import_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_sheets (
    import_id TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(import_id, sheet_name),
    UNIQUE(import_id, position),
    FOREIGN KEY(import_id) REFERENCES alm_imports(import_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_sheet_headings (
    import_id TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    heading TEXT NOT NULL,
    PRIMARY KEY(import_id, sheet_name, position),
    FOREIGN KEY(import_id, sheet_name)
        REFERENCES alm_sheets(import_id, sheet_name) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_sheet_dates (
    import_id TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    date_value TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    deployment_count INTEGER NOT NULL DEFAULT 0 CHECK(deployment_count >= 0),
    missing_username_deployment_count INTEGER NOT NULL DEFAULT 0 CHECK(missing_username_deployment_count >= 0),
    returned_device_count INTEGER NOT NULL DEFAULT 0 CHECK(returned_device_count >= 0),
    pending_return_count INTEGER NOT NULL DEFAULT 0 CHECK(pending_return_count >= 0),
    row_count INTEGER NOT NULL DEFAULT 0 CHECK(row_count >= 0),
    eligible_row_count INTEGER NOT NULL DEFAULT 0 CHECK(eligible_row_count >= 0),
    PRIMARY KEY(import_id, sheet_name, date_value),
    FOREIGN KEY(import_id, sheet_name)
        REFERENCES alm_sheets(import_id, sheet_name) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_sheet_date_groups (
    import_id TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    date_value TEXT NOT NULL,
    group_value TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0 CHECK(row_count >= 0),
    eligible_row_count INTEGER NOT NULL DEFAULT 0 CHECK(eligible_row_count >= 0),
    deployment_count INTEGER NOT NULL DEFAULT 0 CHECK(deployment_count >= 0),
    missing_username_deployment_count INTEGER NOT NULL DEFAULT 0 CHECK(missing_username_deployment_count >= 0),
    returned_device_count INTEGER NOT NULL DEFAULT 0 CHECK(returned_device_count >= 0),
    pending_return_count INTEGER NOT NULL DEFAULT 0 CHECK(pending_return_count >= 0),
    PRIMARY KEY(import_id, sheet_name, date_value, group_value),
    FOREIGN KEY(import_id, sheet_name, date_value)
        REFERENCES alm_sheet_dates(import_id, sheet_name, date_value) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_sheet_warnings (
    import_id TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    date_value TEXT NOT NULL,
    row_number INTEGER NOT NULL CHECK(row_number > 0),
    username TEXT NOT NULL DEFAULT '',
    missing_returned INTEGER NOT NULL CHECK(missing_returned IN (0, 1)),
    missing_pending INTEGER NOT NULL CHECK(missing_pending IN (0, 1)),
    PRIMARY KEY(import_id, sheet_name, date_value, row_number),
    FOREIGN KEY(import_id, sheet_name, date_value)
        REFERENCES alm_sheet_dates(import_id, sheet_name, date_value) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_inspection_sheets (
    import_id TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(import_id, sheet_name),
    UNIQUE(import_id, position),
    FOREIGN KEY(import_id) REFERENCES alm_imports(import_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_inspection_headings (
    import_id TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    heading TEXT NOT NULL,
    PRIMARY KEY(import_id, sheet_name, position),
    FOREIGN KEY(import_id, sheet_name)
        REFERENCES alm_inspection_sheets(import_id, sheet_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alm_import_drafts (
    draft_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('mapping', 'options', 'review', 'import')),
    saved_at TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT '',
    selected_sheet TEXT NOT NULL DEFAULT '',
    location_city TEXT NOT NULL DEFAULT '',
    location_building TEXT NOT NULL DEFAULT '',
    location_floor TEXT NOT NULL DEFAULT '',
    location_room TEXT NOT NULL DEFAULT '',
    location_cabinet TEXT NOT NULL DEFAULT '',
    returned_serials_on_hand TEXT NOT NULL DEFAULT '',
    backlog_days INTEGER NOT NULL DEFAULT 30 CHECK(backlog_days BETWEEN 1 AND 3650),
    backlog_include_today INTEGER NOT NULL DEFAULT 0 CHECK(backlog_include_today IN (0, 1)),
    FOREIGN KEY(import_id) REFERENCES alm_imports(import_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_alm_drafts_saved
    ON alm_import_drafts(saved_at DESC);
CREATE TABLE IF NOT EXISTS alm_draft_dates (
    draft_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    date_value TEXT NOT NULL,
    PRIMARY KEY(draft_id, position),
    UNIQUE(draft_id, date_value),
    FOREIGN KEY(draft_id) REFERENCES alm_import_drafts(draft_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_draft_groups (
    draft_id TEXT NOT NULL,
    date_value TEXT NOT NULL,
    group_value TEXT NOT NULL,
    PRIMARY KEY(draft_id, date_value),
    FOREIGN KEY(draft_id) REFERENCES alm_import_drafts(draft_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_draft_modes (
    draft_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    mode TEXT NOT NULL,
    PRIMARY KEY(draft_id, position),
    UNIQUE(draft_id, mode),
    FOREIGN KEY(draft_id) REFERENCES alm_import_drafts(draft_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_draft_columns (
    draft_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    heading TEXT NOT NULL,
    PRIMARY KEY(draft_id, field_name),
    FOREIGN KEY(draft_id) REFERENCES alm_import_drafts(draft_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_draft_preview (
    draft_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT '',
    sheet_name TEXT NOT NULL DEFAULT '',
    days_back INTEGER NOT NULL DEFAULT 0,
    include_today INTEGER NOT NULL DEFAULT 0 CHECK(include_today IN (0, 1)),
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    ignored_count INTEGER NOT NULL DEFAULT 0 CHECK(ignored_count >= 0),
    returned_serials_on_hand TEXT NOT NULL DEFAULT '',
    request_count INTEGER NOT NULL DEFAULT 0 CHECK(request_count >= 0),
    deployment_count INTEGER NOT NULL DEFAULT 0 CHECK(deployment_count >= 0),
    returned_device_count INTEGER NOT NULL DEFAULT 0 CHECK(returned_device_count >= 0),
    pending_return_count INTEGER NOT NULL DEFAULT 0 CHECK(pending_return_count >= 0),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count >= 0),
    already_deployed_count INTEGER NOT NULL DEFAULT 0 CHECK(already_deployed_count >= 0),
    ignored_row_count INTEGER NOT NULL DEFAULT 0 CHECK(ignored_row_count >= 0),
    outside_range_count INTEGER NOT NULL DEFAULT 0 CHECK(outside_range_count >= 0),
    today_excluded_count INTEGER NOT NULL DEFAULT 0 CHECK(today_excluded_count >= 0),
    missing_serial_count INTEGER NOT NULL DEFAULT 0 CHECK(missing_serial_count >= 0),
    missing_username_count INTEGER NOT NULL DEFAULT 0 CHECK(missing_username_count >= 0),
    FOREIGN KEY(draft_id) REFERENCES alm_import_drafts(draft_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_draft_ignored_reasons (
    draft_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK(item_count >= 0),
    PRIMARY KEY(draft_id, reason),
    FOREIGN KEY(draft_id) REFERENCES alm_import_drafts(draft_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_draft_selected_dates (
    draft_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    date_value TEXT NOT NULL,
    PRIMARY KEY(draft_id, position),
    UNIQUE(draft_id, date_value),
    FOREIGN KEY(draft_id) REFERENCES alm_import_drafts(draft_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_draft_missing_user_warnings (
    draft_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    row_number INTEGER NOT NULL CHECK(row_number > 0),
    date_value TEXT NOT NULL DEFAULT '',
    serial TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    status_preselected INTEGER NOT NULL CHECK(status_preselected IN (0, 1)),
    device_allocation TEXT NOT NULL DEFAULT '',
    new_asset_status TEXT NOT NULL DEFAULT '',
    has_returned_device_serial INTEGER NOT NULL CHECK(has_returned_device_serial IN (0, 1)),
    has_pending_return_serial INTEGER NOT NULL CHECK(has_pending_return_serial IN (0, 1)),
    new_joiner INTEGER NOT NULL CHECK(new_joiner IN (0, 1)),
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(draft_id, position),
    FOREIGN KEY(draft_id) REFERENCES alm_import_drafts(draft_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alm_backlog_ignores (
    serial_normalized TEXT NOT NULL,
    username_normalized TEXT NOT NULL,
    serial TEXT NOT NULL,
    username TEXT NOT NULL,
    ignored_at TEXT NOT NULL,
    PRIMARY KEY(serial_normalized, username_normalized)
);
CREATE INDEX IF NOT EXISTS idx_alm_backlog_ignores_user
    ON alm_backlog_ignores(username_normalized);

CREATE TABLE IF NOT EXISTS pc_toolkit_models (
    model_key TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pc_toolkit_lookups (
    lookup_id INTEGER PRIMARY KEY,
    query_key TEXT NOT NULL,
    query_text TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    lookup_kind TEXT NOT NULL DEFAULT 'device',
    found INTEGER NOT NULL CHECK(found IN (0, 1)),
    active_count INTEGER NOT NULL DEFAULT 0 CHECK(active_count >= 0),
    record_count INTEGER NOT NULL DEFAULT 0 CHECK(record_count >= 0),
    ambiguous INTEGER NOT NULL DEFAULT 0 CHECK(ambiguous IN (0, 1)),
    warning TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    cached_result INTEGER NOT NULL DEFAULT 0 CHECK(cached_result IN (0, 1)),
    stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0, 1)),
    age_seconds INTEGER,
    is_cache_entry INTEGER NOT NULL DEFAULT 0 CHECK(is_cache_entry IN (0, 1)),
    source TEXT NOT NULL DEFAULT 'lookup'
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pc_toolkit_cached_query
    ON pc_toolkit_lookups(query_key) WHERE is_cache_entry = 1;
CREATE INDEX IF NOT EXISTS idx_pc_toolkit_lookup_fetched
    ON pc_toolkit_lookups(is_cache_entry, fetched_at DESC);

CREATE TABLE IF NOT EXISTS submission_jobs (
    job_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    request_for TEXT NOT NULL DEFAULT '',
    simulation INTEGER NOT NULL CHECK(simulation IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_submission_jobs_created
    ON submission_jobs(created_at DESC);
CREATE TABLE IF NOT EXISTS request_records (
    request_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope IN ('queue', 'submission', 'alm')),
    queue_position INTEGER,
    draft_id TEXT,
    draft_position INTEGER,
    job_id TEXT,
    submission_position INTEGER,
    kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    user_login TEXT NOT NULL DEFAULT '',
    returning_requested INTEGER NOT NULL DEFAULT 0 CHECK(returning_requested IN (0, 1)),
    returning_user_login TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    device_allocation TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    location_city TEXT NOT NULL DEFAULT '',
    location_building TEXT NOT NULL DEFAULT '',
    location_floor TEXT NOT NULL DEFAULT '',
    location_room TEXT NOT NULL DEFAULT '',
    location_cabinet TEXT NOT NULL DEFAULT '',
    primary_serial TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    deployment_date TEXT NOT NULL DEFAULT '',
    current_status TEXT NOT NULL DEFAULT '',
    new_asset_status TEXT NOT NULL DEFAULT '',
    alm_row_number INTEGER,
    username_occurrence INTEGER,
    username_occurrence_total INTEGER,
    attending INTEGER CHECK(attending IN (0, 1)),
    new_joiner INTEGER CHECK(new_joiner IN (0, 1)),
    has_returned_device_serial INTEGER CHECK(has_returned_device_serial IN (0, 1)),
    has_pending_return_serial INTEGER CHECK(has_pending_return_serial IN (0, 1)),
    returning_user_selected INTEGER CHECK(returning_user_selected IN (0, 1)),
    user_selected INTEGER CHECK(user_selected IN (0, 1)),
    cached_serial_verification INTEGER CHECK(cached_serial_verification IN (0, 1)),
    cached_user_verification INTEGER CHECK(cached_user_verification IN (0, 1)),
    serial_validation TEXT NOT NULL DEFAULT '',
    serial_validation_error TEXT NOT NULL DEFAULT '',
    serial_validation_epoch INTEGER NOT NULL DEFAULT 0,
    user_validation TEXT NOT NULL DEFAULT '',
    user_validation_error TEXT NOT NULL DEFAULT '',
    user_validation_epoch INTEGER NOT NULL DEFAULT 0,
    returning_user_validation TEXT NOT NULL DEFAULT '',
    returning_user_validation_error TEXT NOT NULL DEFAULT '',
    bulk_validation TEXT NOT NULL DEFAULT '',
    bulk_validation_error TEXT NOT NULL DEFAULT '',
    bulk_serial_mode TEXT NOT NULL DEFAULT '',
    pc_toolkit_epoch INTEGER NOT NULL DEFAULT 0,
    pc_toolkit_enriched_at TEXT NOT NULL DEFAULT '',
    import_validation TEXT NOT NULL DEFAULT '',
    import_error TEXT NOT NULL DEFAULT '',
    import_validation_epoch INTEGER NOT NULL DEFAULT 0,
    backlog_validation_epoch INTEGER NOT NULL DEFAULT 0,
    included INTEGER CHECK(included IN (0, 1)),
    default_excluded INTEGER CHECK(default_excluded IN (0, 1)),
    backlog_ignored INTEGER CHECK(backlog_ignored IN (0, 1)),
    manual_return_id TEXT NOT NULL DEFAULT '',
    manual_return_serial TEXT NOT NULL DEFAULT '',
    manual_return_type TEXT NOT NULL DEFAULT '',
    manual_return_status TEXT NOT NULL DEFAULT '',
    manual_return_auto_status TEXT NOT NULL DEFAULT '',
    manual_return_error TEXT NOT NULL DEFAULT '',
    manual_return_dismissed INTEGER CHECK(manual_return_dismissed IN (0, 1)),
    manual_return_lookup_serial TEXT NOT NULL DEFAULT '',
    manual_return_lookup_state TEXT NOT NULL DEFAULT '',
    manual_return_lookup_error TEXT NOT NULL DEFAULT '',
    manual_return_model TEXT NOT NULL DEFAULT '',
    manual_return_source_id TEXT NOT NULL DEFAULT '',
    manual_username_source_id TEXT NOT NULL DEFAULT '',
    pc_toolkit_suggested_status TEXT NOT NULL DEFAULT '',
    pc_toolkit_default_excluded INTEGER CHECK(pc_toolkit_default_excluded IN (0, 1)),
    pc_toolkit_conflict_level TEXT NOT NULL DEFAULT '',
    pc_toolkit_conflict_message TEXT NOT NULL DEFAULT '',
    returned_serial_match INTEGER CHECK(returned_serial_match IN (0, 1)),
    content_signature TEXT NOT NULL DEFAULT '',
    UNIQUE(queue_position),
    UNIQUE(draft_id, draft_position),
    UNIQUE(job_id, submission_position),
    FOREIGN KEY(draft_id) REFERENCES alm_import_drafts(draft_id) ON DELETE CASCADE,
    FOREIGN KEY(job_id) REFERENCES submission_jobs(job_id) ON DELETE CASCADE,
    CHECK((scope = 'queue' AND queue_position IS NOT NULL AND draft_id IS NULL AND job_id IS NULL)
       OR (scope = 'submission' AND job_id IS NOT NULL AND submission_position IS NOT NULL AND draft_id IS NULL AND queue_position IS NULL)
       OR (scope = 'alm' AND draft_id IS NOT NULL AND draft_position IS NOT NULL AND job_id IS NULL AND queue_position IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_request_records_scope ON request_records(scope, queue_position, draft_id, draft_position);
CREATE INDEX IF NOT EXISTS idx_request_records_user ON request_records(user_login COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_request_records_username ON request_records(username COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_request_records_serial ON request_records(primary_serial COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_request_records_date ON request_records(deployment_date);
CREATE TABLE IF NOT EXISTS request_serials (
    request_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    serial TEXT NOT NULL,
    normalized_serial TEXT NOT NULL,
    validation_state TEXT NOT NULL DEFAULT '',
    validation_error TEXT NOT NULL DEFAULT '',
    queue_active INTEGER NOT NULL CHECK(queue_active IN (0, 1)),
    PRIMARY KEY(request_id, position),
    UNIQUE(request_id, normalized_serial),
    FOREIGN KEY(request_id) REFERENCES request_records(request_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_queue_serial
    ON request_serials(normalized_serial) WHERE queue_active = 1;
CREATE INDEX IF NOT EXISTS idx_request_serials_normalized ON request_serials(normalized_serial);
CREATE TABLE IF NOT EXISTS request_person_details (
    request_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'returning_user')),
    login TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(request_id, role),
    FOREIGN KEY(request_id) REFERENCES request_records(request_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS request_person_columns (
    request_id TEXT NOT NULL,
    role TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    column_value TEXT NOT NULL,
    PRIMARY KEY(request_id, role, position),
    FOREIGN KEY(request_id, role)
        REFERENCES request_person_details(request_id, role) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS request_validation_items (
    request_id TEXT NOT NULL,
    group_name TEXT NOT NULL CHECK(group_name IN ('bulk_missing', 'bulk_serial')),
    item_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0 CHECK(position >= 0),
    PRIMARY KEY(request_id, group_name, item_key),
    FOREIGN KEY(request_id) REFERENCES request_records(request_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS request_import_failed_fields (
    request_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(request_id, field_name),
    FOREIGN KEY(request_id) REFERENCES request_records(request_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS request_error_messages (
    request_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    message TEXT NOT NULL,
    PRIMARY KEY(request_id, position),
    FOREIGN KEY(request_id) REFERENCES request_records(request_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS max_portal_associations (
    request_id TEXT PRIMARY KEY,
    reference TEXT NOT NULL DEFAULT '',
    helix_id TEXT NOT NULL DEFAULT '',
    request_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    requested_for TEXT NOT NULL DEFAULT '',
    requested_for_name TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    requested_by_name TEXT NOT NULL DEFAULT '',
    requested_for_location TEXT NOT NULL DEFAULT '',
    workflow_instance_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    old_serial TEXT NOT NULL DEFAULT '',
    old_model TEXT NOT NULL DEFAULT '',
    old_manufacturer TEXT NOT NULL DEFAULT '',
    device_type TEXT NOT NULL DEFAULT '',
    comments TEXT NOT NULL DEFAULT '',
    detail_loaded INTEGER NOT NULL DEFAULT 0 CHECK(detail_loaded IN (0, 1)),
    details_expanded INTEGER NOT NULL DEFAULT 0 CHECK(details_expanded IN (0, 1)),
    match_source TEXT NOT NULL DEFAULT '',
    match_key TEXT NOT NULL DEFAULT '',
    matched_username TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(request_id) REFERENCES request_records(request_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS submission_entries (
    job_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL CHECK(position >= 0),
    state TEXT NOT NULL,
    message TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 0 CHECK(step >= 0),
    step_count INTEGER NOT NULL DEFAULT 3 CHECK(step_count >= 0),
    request_id_remote TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    elapsed_seconds REAL,
    PRIMARY KEY(job_id, position),
    FOREIGN KEY(job_id) REFERENCES submission_jobs(job_id) ON DELETE CASCADE,
    FOREIGN KEY(request_id) REFERENCES request_records(request_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pc_toolkit_lookup_devices (
    lookup_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    serial TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    has_sccm INTEGER NOT NULL CHECK(has_sccm IN (0, 1)),
    has_cmdb INTEGER NOT NULL CHECK(has_cmdb IN (0, 1)),
    location TEXT NOT NULL DEFAULT '',
    organisation TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    legal_hold TEXT NOT NULL DEFAULT '',
    ci_id TEXT NOT NULL DEFAULT '',
    reconciliation_id TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
    PRIMARY KEY(lookup_id, position),
    FOREIGN KEY(lookup_id) REFERENCES pc_toolkit_lookups(lookup_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pc_lookup_device_serial ON pc_toolkit_lookup_devices(serial COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_pc_lookup_device_model ON pc_toolkit_lookup_devices(model COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS pc_toolkit_lookup_people (
    lookup_id INTEGER NOT NULL,
    device_position INTEGER NOT NULL,
    slot TEXT NOT NULL CHECK(slot IN ('assigned_user', 'used_by', 'owner', 'people', 'primary_users', 'profile_users')),
    position INTEGER NOT NULL CHECK(position >= 0),
    login TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(lookup_id, device_position, slot, position),
    FOREIGN KEY(lookup_id, device_position)
        REFERENCES pc_toolkit_lookup_devices(lookup_id, position) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pc_lookup_people_login ON pc_toolkit_lookup_people(login COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS pc_toolkit_lookup_hardware (
    lookup_id INTEGER NOT NULL,
    device_position INTEGER NOT NULL,
    chassis TEXT NOT NULL DEFAULT '',
    memory_gb REAL,
    operating_system TEXT NOT NULL DEFAULT '',
    os_build TEXT NOT NULL DEFAULT '',
    bitlocker TEXT NOT NULL DEFAULT '',
    tpm_enabled INTEGER CHECK(tpm_enabled IN (0, 1)),
    reboot_needed INTEGER CHECK(reboot_needed IN (0, 1)),
    hardware_inventory_days REAL,
    health_check_days REAL,
    software_inventory_days REAL,
    PRIMARY KEY(lookup_id, device_position),
    FOREIGN KEY(lookup_id, device_position)
        REFERENCES pc_toolkit_lookup_devices(lookup_id, position) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS request_pc_toolkit_links (
    request_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    slot TEXT NOT NULL DEFAULT '',
    lookup_id INTEGER NOT NULL,
    PRIMARY KEY(request_id, field_name, slot),
    FOREIGN KEY(request_id) REFERENCES request_records(request_id) ON DELETE CASCADE,
    FOREIGN KEY(lookup_id) REFERENCES pc_toolkit_lookups(lookup_id)
);
"""

def _upgrade_schema_2(connection: sqlite3.Connection) -> None:
    """Add hashes used to avoid rewriting unchanged relational records."""
    for table, column, definition in (
        ("alm_imports", "summary_signature", "TEXT NOT NULL DEFAULT ''"),
        ("request_records", "content_signature", "TEXT NOT NULL DEFAULT ''"),
    ):
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )


# Add one migration function for each version and keep it safe to run inside a
# single EXCLUSIVE transaction. Hash columns store fingerprints only; the data
# remains in typed domain tables rather than serialized JSON documents.
_SCHEMA_UPGRADES: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _upgrade_schema_2,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _bool(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _bool_value(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _mapping(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _list(raw: Any) -> list[Any]:
    return raw if isinstance(raw, list) else []


def _content_signature(raw: dict[str, Any]) -> str:
    """Fingerprint an API-shaped record without persisting its JSON form."""
    encoded = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DatabaseError(RuntimeError):
    """A persisted Deployments operation failed."""


class AppDatabase:
    """A WAL-backed database scoped to exactly one running instance."""

    def __init__(
        self,
        path: Path | str,
        *,
        instance_id: str = "default",
        legacy_results_dir: Path | str | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.instance_id = instance_id
        self.legacy_results_dir = (
            Path(legacy_results_dir).expanduser().resolve()
            if legacy_results_dir is not None
            else None
        )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._initialise()
        if self.legacy_results_dir is not None:
            self.migrate_legacy_results(self.legacy_results_dir)

    @classmethod
    def current_instance(cls) -> "AppDatabase":
        key = (str(DATABASE_PATH.expanduser().resolve()), INSTANCE_ID)
        with _DB_LOCK:
            database = _INSTANCES.get(key)
            if database is None:
                database = cls(
                    DATABASE_PATH,
                    instance_id=INSTANCE_ID,
                    legacy_results_dir=LEGACY_RESULTS_DIR,
                )
                _INSTANCES[key] = database
            return database

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=20,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def _read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """Read related rows from one consistent SQLite snapshot."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialise(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise DatabaseError(
                    f"Database schema {current} is newer than this Deployments build supports."
                )
            if current == 0:
                connection.executescript(
                    "BEGIN EXCLUSIVE;\n"
                    + _SCHEMA
                    + f"\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                )
                current = SCHEMA_VERSION
            while current < SCHEMA_VERSION:
                target_version = current + 1
                migration = _SCHEMA_UPGRADES.get(target_version)
                if migration is None:
                    raise DatabaseError(
                        f"No database migration is available for schema {target_version}."
                    )
                connection.execute("BEGIN EXCLUSIVE")
                try:
                    # Another process may have upgraded the database while
                    # this process waited for SQLite's schema lock.
                    actual_version = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    if actual_version >= target_version:
                        connection.commit()
                        current = actual_version
                        continue
                    migration(connection)
                    connection.execute(f"PRAGMA user_version = {target_version}")
                    connection.commit()
                    current = target_version
                except BaseException:
                    connection.rollback()
                    raise
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # Preferences are expanded into one settings row and ordered child tables.
    def has_preferences(self) -> bool:
        with self._read_snapshot() as connection:
            return connection.execute(
                "SELECT 1 FROM preferences WHERE singleton = 1"
            ).fetchone() is not None

    def load_preferences(self, defaults: dict[str, Any]) -> dict[str, Any]:
        with self._read_snapshot() as connection:
            return self._load_preferences_connection(connection, defaults)

    @staticmethod
    def _load_preferences_connection(
        connection: sqlite3.Connection, defaults: dict[str, Any]
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM preferences WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return dict(defaults)
        values = dict(defaults)
        values["concurrency"] = int(row["concurrency"])
        for field in _BOOL_SETTINGS:
            values[field] = bool(row[field])
        values["update_channel"] = row["update_channel"]
        values["pc_toolkit_transport"] = row["pc_toolkit_transport"]
        values["request_statuses"] = [
            item["status"]
            for item in connection.execute(
                "SELECT status FROM preference_request_statuses ORDER BY position"
            )
        ]
        values["import_columns"] = {
            item["field_name"]: item["heading"]
            for item in connection.execute(
                "SELECT field_name, heading FROM preference_import_columns"
            )
        }
        values["pc_toolkit_model_mappings"] = [
            {
                "model": item["model_name"],
                "user_status": item["user_status"],
                "location_status": item["location_status"],
            }
            for item in connection.execute(
                "SELECT model_name, user_status, location_status "
                "FROM device_model_mappings ORDER BY position"
            )
        ]
        return values

    def _save_preferences_connection(
        self, connection: sqlite3.Connection, values: dict[str, Any]
    ) -> None:
        saved_at = _now()
        bool_values = {field: int(bool(values.get(field, False))) for field in _BOOL_SETTINGS}
        fields = [
            "concurrency", *_BOOL_SETTINGS, "update_channel", "pc_toolkit_transport",
        ]
        columns = ["singleton", *fields, "saved_at"]
        row_values = [
            1,
            int(values.get("concurrency", 1)),
            *(bool_values[field] for field in _BOOL_SETTINGS),
            str(values.get("update_channel", "stable")),
            str(values.get("pc_toolkit_transport", "browser")),
            saved_at,
        ]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{field} = excluded.{field}" for field in fields + ["saved_at"])
        connection.execute(
            f"INSERT INTO preferences ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(singleton) DO UPDATE SET {updates}",
            row_values,
        )
        connection.execute("DELETE FROM preference_request_statuses")
        user_statuses = {
            "Deployed - Existing Stock", "Deployed - New Stock", "Loan",
            "Pending Return", "Unmanaged",
        }
        connection.executemany(
            "INSERT INTO preference_request_statuses(position, status, deployment_kind) VALUES (?, ?, ?)",
            [
                (position, str(status), "user" if str(status) in user_statuses else "location")
                for position, status in enumerate(values.get("request_statuses", []))
            ],
        )
        connection.execute("DELETE FROM preference_import_columns")
        connection.executemany(
            "INSERT INTO preference_import_columns(field_name, heading) VALUES (?, ?)",
            [
                (field, str(_mapping(values.get("import_columns")).get(field, "")))
                for field in _IMPORT_COLUMN_NAMES
            ],
        )
        connection.execute("DELETE FROM device_model_mappings")
        mappings = []
        for position, mapping in enumerate(_list(values.get("pc_toolkit_model_mappings"))):
            model = " ".join(str(_mapping(mapping).get("model", "")).split()).strip()
            key = _normalise(model)
            if not key:
                continue
            user_status = str(_mapping(mapping).get("user_status", "") or "")
            location_status = str(_mapping(mapping).get("location_status", "") or "")
            if not user_status and not location_status:
                continue
            mappings.append((key, model, user_status, location_status, position))
        connection.executemany(
            "INSERT INTO device_model_mappings(model_key, model_name, user_status, location_status, position) "
            "VALUES (?, ?, ?, ?, ?)",
            mappings,
        )

    def save_preferences(self, values: dict[str, Any]) -> None:
        with self._write() as connection:
            self._save_preferences_connection(connection, values)

    def update_preferences(
        self,
        update: Callable[[dict[str, Any]], dict[str, Any]],
        defaults: dict[str, Any],
    ) -> dict[str, Any]:
        with self._write() as connection:
            current = self._load_preferences_connection(connection, defaults)
            values = update(current)
            self._save_preferences_connection(connection, values)
            return values

    # PC Toolkit lookup results are decomposed into lookup, device, people,
    # hardware, and freshness rows. The input/output mapping is only an API
    # adapter, not the storage format.
    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _insert_person(
        connection: sqlite3.Connection,
        lookup_id: int,
        device_position: int,
        slot: str,
        position: int,
        raw: Any,
    ) -> None:
        person = _mapping(raw)
        if not person:
            return
        connection.execute(
            "INSERT INTO pc_toolkit_lookup_people "
            "(lookup_id, device_position, slot, position, login, full_name, role) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                lookup_id, device_position, slot, position,
                str(person.get("login", "") or ""),
                str(person.get("name", "") or ""),
                str(person.get("role", "") or ""),
            ),
        )

    def _write_lookup_devices(
        self,
        connection: sqlite3.Connection,
        lookup_id: int,
        result: dict[str, Any],
    ) -> None:
        devices = [_mapping(device) for device in _list(result.get("devices"))]
        primary = _mapping(result.get("primary"))
        primary_key = tuple(_normalise(primary.get(name)) for name in ("serial", "name", "model"))
        primary_marked = False
        for position, device in enumerate(devices):
            device_key = tuple(_normalise(device.get(name)) for name in ("serial", "name", "model"))
            is_primary = bool(primary) and not primary_marked and device_key == primary_key
            primary_marked |= is_primary
            connection.execute(
                "INSERT INTO pc_toolkit_lookup_devices "
                "(lookup_id, position, serial, name, model, manufacturer, status, active, has_sccm, has_cmdb, "
                "location, organisation, department, description, legal_hold, ci_id, reconciliation_id, is_primary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    lookup_id, position,
                    str(device.get("serial", "") or ""),
                    str(device.get("name", "") or ""),
                    str(device.get("model", "") or ""),
                    str(device.get("manufacturer", "") or ""),
                    str(device.get("status", "") or ""),
                    int(bool(device.get("active"))),
                    int(bool(device.get("has_sccm"))),
                    int(bool(device.get("has_cmdb"))),
                    str(device.get("location", "") or ""),
                    str(device.get("organisation", "") or ""),
                    str(device.get("department", "") or ""),
                    str(device.get("description", "") or ""),
                    str(device.get("legal_hold", "") or ""),
                    str(device.get("ci_id", "") or ""),
                    str(device.get("reconciliation_id", "") or ""),
                    int(is_primary),
                ),
            )
            for slot in ("assigned_user", "used_by", "owner"):
                self._insert_person(connection, lookup_id, position, slot, 0, device.get(slot))
            for slot in ("people", "primary_users", "profile_users"):
                for item_position, person in enumerate(_list(device.get(slot))):
                    self._insert_person(connection, lookup_id, position, slot, item_position, person)
            hardware = device.get("hardware")
            freshness = device.get("sccm_freshness")
            if isinstance(hardware, dict):
                connection.execute(
                    "INSERT INTO pc_toolkit_lookup_hardware "
                    "(lookup_id, device_position, chassis, memory_gb, operating_system, os_build, bitlocker, "
                    "tpm_enabled, reboot_needed, hardware_inventory_days, health_check_days, software_inventory_days) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lookup_id, position,
                        str(hardware.get("chassis", "") or ""),
                        self._number(hardware.get("memory_gb")),
                        str(hardware.get("operating_system", "") or ""),
                        str(hardware.get("os_build", "") or ""),
                        str(hardware.get("bitlocker", "") or ""),
                        _bool(hardware.get("tpm_enabled")),
                        _bool(hardware.get("reboot_needed")),
                        self._number(_mapping(freshness).get("hardware_inventory_days")),
                        self._number(_mapping(freshness).get("health_check_days")),
                        self._number(_mapping(freshness).get("software_inventory_days")),
                    ),
                )
            model = str(device.get("model", "") or "").strip()
            if model:
                self._remember_pc_model(connection, model)
        if primary and not primary_marked and devices:
            connection.execute(
                "UPDATE pc_toolkit_lookup_devices SET is_primary = 1 "
                "WHERE lookup_id = ? AND position = 0",
                (lookup_id,),
            )

    @staticmethod
    def _remember_pc_model(connection: sqlite3.Connection, model: str) -> None:
        display = " ".join(str(model or "").split()).strip()
        key = _normalise(display)
        if not key:
            return
        now = _now()
        connection.execute(
            "INSERT INTO pc_toolkit_models(model_key, model_name, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(model_key) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (key, display, now, now),
        )

    def _save_pc_lookup(
        self,
        connection: sqlite3.Connection,
        result: dict[str, Any],
        *,
        fetched_at: float | None = None,
        cache_entry: bool = False,
        source: str = "request_snapshot",
    ) -> int:
        value = _mapping(result)
        query = str(value.get("query", "") or "").strip()
        query_key = _normalise(query)
        if not query_key:
            raise DatabaseError("A PC Toolkit lookup is missing its query key.")
        try:
            stored_at = float(fetched_at if fetched_at is not None else time.time())
        except (TypeError, ValueError):
            stored_at = time.time()
        found = bool(value.get("found", bool(_list(value.get("devices")))))
        primary = _mapping(value.get("primary"))
        if not primary and _list(value.get("devices")):
            primary = _mapping(_list(value.get("devices"))[0])
        row_values = (
            query_key, query, stored_at,
            str(value.get("lookup_kind", "device") or "device"),
            int(found), int(value.get("active_count", 0) or 0),
            int(value.get("record_count", len(_list(value.get("devices")))) or 0),
            int(bool(value.get("ambiguous"))), str(value.get("warning", "") or ""),
            int(value["duration_ms"]) if value.get("duration_ms") is not None else None,
            int(bool(value.get("cached"))), int(bool(value.get("stale"))),
            int(value["age_seconds"]) if value.get("age_seconds") is not None else None,
            int(cache_entry), source,
        )
        row = None
        if cache_entry:
            row = connection.execute(
                "SELECT lookup_id FROM pc_toolkit_lookups WHERE query_key = ? AND is_cache_entry = 1",
                (query_key,),
            ).fetchone()
        lookup_id = int(row["lookup_id"]) if row else None
        if lookup_id is None:
            cursor = connection.execute(
                "INSERT INTO pc_toolkit_lookups "
                "(query_key, query_text, fetched_at, lookup_kind, found, active_count, record_count, ambiguous, warning, "
                "duration_ms, cached_result, stale, age_seconds, is_cache_entry, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row_values,
            )
            lookup_id = int(cursor.lastrowid)
        else:
            connection.execute(
                "UPDATE pc_toolkit_lookups SET query_key=?, query_text=?, fetched_at=?, lookup_kind=?, found=?, "
                "active_count=?, record_count=?, ambiguous=?, warning=?, duration_ms=?, cached_result=?, stale=?, "
                "age_seconds=?, is_cache_entry=?, source=? WHERE lookup_id=?",
                (*row_values, lookup_id),
            )
            connection.execute("DELETE FROM pc_toolkit_lookup_devices WHERE lookup_id = ?", (lookup_id,))
        self._write_lookup_devices(connection, lookup_id, {**value, "primary": primary})
        return lookup_id

    def _load_pc_lookup(self, connection: sqlite3.Connection, lookup_id: int) -> dict[str, Any] | None:
        lookup = connection.execute(
            "SELECT * FROM pc_toolkit_lookups WHERE lookup_id = ?", (lookup_id,)
        ).fetchone()
        if lookup is None:
            return None
        result = {
            "query": lookup["query_text"],
            "lookup_kind": lookup["lookup_kind"],
            "found": bool(lookup["found"]),
            "active_count": int(lookup["active_count"]),
            "record_count": int(lookup["record_count"]),
            "ambiguous": bool(lookup["ambiguous"]),
            "warning": lookup["warning"],
            "devices": [],
        }
        if lookup["duration_ms"] is not None:
            result["duration_ms"] = int(lookup["duration_ms"])
        if lookup["cached_result"]:
            result["cached"] = True
        if lookup["stale"]:
            result["stale"] = True
        if lookup["age_seconds"] is not None:
            result["age_seconds"] = int(lookup["age_seconds"])
        for device_row in connection.execute(
            "SELECT * FROM pc_toolkit_lookup_devices WHERE lookup_id = ? ORDER BY position",
            (lookup_id,),
        ):
            device_position = int(device_row["position"])
            device = {
                "serial": device_row["serial"], "name": device_row["name"],
                "model": device_row["model"], "manufacturer": device_row["manufacturer"],
                "status": device_row["status"], "active": bool(device_row["active"]),
                "has_sccm": bool(device_row["has_sccm"]), "has_cmdb": bool(device_row["has_cmdb"]),
                "location": device_row["location"], "organisation": device_row["organisation"],
                "department": device_row["department"], "description": device_row["description"],
                "legal_hold": device_row["legal_hold"], "ci_id": device_row["ci_id"],
                "reconciliation_id": device_row["reconciliation_id"],
                "people": [], "primary_users": [], "profile_users": [],
                "assigned_user": None, "used_by": None, "owner": None,
                "sccm_freshness": None, "hardware": None,
            }
            for person in connection.execute(
                "SELECT slot, position, login, full_name, role FROM pc_toolkit_lookup_people "
                "WHERE lookup_id = ? AND device_position = ? ORDER BY slot, position",
                (lookup_id, device_position),
            ):
                person_value = {
                    "login": person["login"], "name": person["full_name"], "role": person["role"]
                }
                if person["slot"] in {"people", "primary_users", "profile_users"}:
                    device[person["slot"]].append(person_value)
                else:
                    device[person["slot"]] = person_value
            hardware = connection.execute(
                "SELECT * FROM pc_toolkit_lookup_hardware WHERE lookup_id = ? AND device_position = ?",
                (lookup_id, device_position),
            ).fetchone()
            if hardware is not None:
                device["hardware"] = {
                    "chassis": hardware["chassis"],
                    "memory_gb": hardware["memory_gb"],
                    "operating_system": hardware["operating_system"],
                    "os_build": hardware["os_build"],
                    "bitlocker": hardware["bitlocker"],
                    "tpm_enabled": _bool_value(hardware["tpm_enabled"]),
                    "reboot_needed": _bool_value(hardware["reboot_needed"]),
                }
                device["sccm_freshness"] = {
                    "hardware_inventory_days": hardware["hardware_inventory_days"],
                    "health_check_days": hardware["health_check_days"],
                    "software_inventory_days": hardware["software_inventory_days"],
                }
            result["devices"].append(device)
            if device_row["is_primary"]:
                result["primary"] = device
        if "primary" not in result:
            result["primary"] = result["devices"][0] if result["devices"] else None
        return result

    def load_pc_toolkit_cache(self) -> dict[str, Any]:
        with self._read_snapshot() as connection:
            rows = connection.execute(
                "SELECT lookup_id, query_key, fetched_at FROM pc_toolkit_lookups "
                "WHERE is_cache_entry = 1 ORDER BY fetched_at DESC LIMIT ?",
                (MAX_PC_TOOLKIT_ITEMS,),
            ).fetchall()
            entries: dict[str, Any] = {}
            for row in reversed(rows):
                result = self._load_pc_lookup(connection, int(row["lookup_id"]))
                if result is not None:
                    entries[str(row["query_key"])] = {
                        "fetched_at": float(row["fetched_at"]),
                        "result": result,
                    }
            models = [
                row["model_name"]
                for row in connection.execute(
                    "SELECT model_name FROM pc_toolkit_models ORDER BY model_name COLLATE NOCASE"
                )
            ]
            return {"version": 2, "entries": entries, "models": models}

    def load_pc_toolkit_cache_entries(self, query_keys: list[str]) -> dict[str, dict[str, Any]]:
        """Load only requested cached lookups through SQLite's query-key index."""
        keys = list(dict.fromkeys(_normalise(key) for key in query_keys if _normalise(key)))
        if not keys:
            return {}
        entries: dict[str, dict[str, Any]] = {}
        # Stay below SQLite builds with the traditional 999-variable limit.
        with self._read_snapshot() as connection:
            for offset in range(0, len(keys), 400):
                chunk = keys[offset:offset + 400]
                placeholders = ", ".join("?" for _ in chunk)
                rows = connection.execute(
                    "SELECT lookup_id, query_key, fetched_at FROM pc_toolkit_lookups "
                    f"WHERE is_cache_entry=1 AND query_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    result = self._load_pc_lookup(connection, int(row["lookup_id"]))
                    if result is not None:
                        entries[str(row["query_key"])] = {
                            "fetched_at": float(row["fetched_at"]),
                            "result": result,
                        }
        return entries

    def pc_toolkit_cache_count(self) -> int:
        with self._read_snapshot() as connection:
            return int(connection.execute(
                "SELECT count(*) FROM pc_toolkit_lookups WHERE is_cache_entry=1"
            ).fetchone()[0])

    def load_pc_toolkit_models(self) -> list[str]:
        with self._read_snapshot() as connection:
            return [
                row["model_name"]
                for row in connection.execute(
                    "SELECT model_name FROM pc_toolkit_models ORDER BY model_name COLLATE NOCASE"
                )
            ]

    def save_pc_toolkit_cache(
        self,
        entries: dict[str, Any],
        models: set[str] | list[str],
        *,
        max_entries: int,
        reason: str = "update",
    ) -> None:
        with self._write() as connection:
            if reason == "clear_cache":
                connection.execute("UPDATE pc_toolkit_lookups SET is_cache_entry = 0")
                connection.execute(
                    "DELETE FROM pc_toolkit_lookups WHERE is_cache_entry = 0 AND lookup_id NOT IN "
                    "(SELECT lookup_id FROM request_pc_toolkit_links)"
                )
            else:
                for key, cached in list(entries.items())[-max_entries:]:
                    wrapper = _mapping(cached)
                    result = _mapping(wrapper.get("result"))
                    if not result:
                        continue
                    if not result.get("query"):
                        result = {**result, "query": key}
                    self._save_pc_lookup(
                        connection,
                        result,
                        fetched_at=self._number(wrapper.get("fetched_at")) or time.time(),
                        cache_entry=True,
                        source="cache",
                    )
            if reason == "clear_models":
                connection.execute("DELETE FROM pc_toolkit_models")
            else:
                for model in models:
                    self._remember_pc_model(connection, str(model))
            overflow = connection.execute(
                "SELECT lookup_id FROM pc_toolkit_lookups WHERE is_cache_entry = 1 "
                "ORDER BY fetched_at DESC LIMIT -1 OFFSET ?", (max_entries,)
            ).fetchall()
            if overflow:
                connection.executemany(
                    "UPDATE pc_toolkit_lookups SET is_cache_entry = 0 WHERE lookup_id = ?",
                    [(row["lookup_id"],) for row in overflow],
                )
                # Request/history snapshots retain their lookup row through a
                # foreign-key link; expired, unreferenced cache rows can be
                # collected instead of accumulating forever.
                connection.execute(
                    "DELETE FROM pc_toolkit_lookups WHERE is_cache_entry = 0 AND lookup_id NOT IN "
                    "(SELECT lookup_id FROM request_pc_toolkit_links)"
                )

    @staticmethod
    def _person_columns(
        connection: sqlite3.Connection,
        request_id: str,
        role: str,
        raw: Any,
    ) -> None:
        if not isinstance(raw, dict):
            return
        connection.execute(
            "INSERT INTO request_person_details(request_id, role, login) VALUES (?, ?, ?)",
            (request_id, role, str(raw.get("login", "") or "")),
        )
        connection.executemany(
            "INSERT INTO request_person_columns(request_id, role, position, column_value) VALUES (?, ?, ?, ?)",
            [
                (request_id, role, position, str(value))
                for position, value in enumerate(_list(raw.get("columns")))
            ],
        )

    def _request_pc_link(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        field_name: str,
        slot: str,
        result: Any,
    ) -> None:
        if not isinstance(result, dict) or not result:
            return
        lookup_id = self._save_pc_lookup(connection, result, source="request_snapshot")
        connection.execute(
            "INSERT INTO request_pc_toolkit_links(request_id, field_name, slot, lookup_id) "
            "VALUES (?, ?, ?, ?)",
            (request_id, field_name, slot, lookup_id),
        )

    def _store_pc_links(self, connection: sqlite3.Connection, request_id: str, raw: dict[str, Any]) -> None:
        pc = _mapping(raw.get("pc_toolkit"))
        if pc:
            for field in ("serial", "user"):
                self._request_pc_link(connection, request_id, "pc_toolkit", field, pc.get(field))
            for field in ("serials", "bulk"):
                for key, result in _mapping(pc.get(field)).items():
                    self._request_pc_link(connection, request_id, "pc_toolkit", f"{field}:{key}", result)
        manual = raw.get("manual_return_pc_toolkit")
        self._request_pc_link(connection, request_id, "manual_return_pc_toolkit", "", manual)

    def _insert_request_record(
        self,
        connection: sqlite3.Connection,
        raw: dict[str, Any],
        *,
        scope: str,
        queue_position: int | None = None,
        draft_id: str | None = None,
        draft_position: int | None = None,
        job_id: str | None = None,
        submission_position: int | None = None,
    ) -> str:
        request_id = str(raw.get("id") or raw.get("client_id") or "").strip()
        if not request_id:
            raise DatabaseError("A persisted request is missing its identifier.")
        signature = _content_signature(raw)
        existing = connection.execute(
            "SELECT scope, content_signature, queue_position, draft_id, draft_position, job_id, "
            "submission_position FROM request_records WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if (
            existing is not None
            and existing["scope"] == scope
            and existing["content_signature"] == signature
        ):
            # Most queue autosaves and resumable-draft saves contain few or no
            # edits. Move the indexed parent row only and leave its serial,
            # validation, person, portal, and PC Toolkit relationships intact.
            target_location = (queue_position, draft_id, draft_position, job_id, submission_position)
            current_location = tuple(
                existing[name]
                for name in (
                    "queue_position", "draft_id", "draft_position", "job_id", "submission_position"
                )
            )
            if current_location != target_location:
                connection.execute(
                    "UPDATE request_records SET queue_position=?, draft_id=?, draft_position=?, "
                    "job_id=?, submission_position=? WHERE request_id=?",
                    (*target_location, request_id),
                )
            return request_id
        location = _mapping(raw.get("location"))
        serial_values = _list(raw.get("serials"))
        if not serial_values and raw.get("serial"):
            serial_values = [raw.get("serial")]
        serial_values = [str(value).strip() for value in serial_values if str(value or "").strip()]
        serial_key = str(raw.get("serial", "") or "").strip()
        if not serial_key and serial_values:
            serial_key = serial_values[0]
        conflict = _mapping(raw.get("pc_toolkit_conflict"))
        values: dict[str, Any] = {
            "request_id": request_id,
            "scope": scope,
            "queue_position": queue_position,
            "draft_id": draft_id,
            "draft_position": draft_position,
            "job_id": job_id,
            "submission_position": submission_position,
            "kind": str(raw.get("kind", "") or ""),
            "status": str(raw.get("status", "") or ""),
            "user_login": str(raw.get("user", raw.get("username", "")) or ""),
            "returning_requested": int(bool(raw.get("returning", raw.get("returning_requested", False)))),
            "returning_user_login": str(raw.get("returning_user", "") or ""),
            "group_name": str(raw.get("group", "") or ""),
            "source": str(raw.get("source", "") or ""),
            "device_allocation": str(raw.get("device_allocation", "") or ""),
            "first_name": str(raw.get("first_name", "") or ""),
            "last_name": str(raw.get("last_name", "") or ""),
            "location_city": str(location.get("city", "") or ""),
            "location_building": str(location.get("building", "") or ""),
            "location_floor": str(location.get("floor", "") or ""),
            "location_room": str(location.get("room", "") or ""),
            "location_cabinet": str(location.get("cabinet", "") or ""),
            "primary_serial": serial_key,
            "username": str(raw.get("username", raw.get("user", "")) or ""),
            "deployment_date": str(raw.get("deployment_date", raw.get("date", "")) or ""),
            "current_status": str(raw.get("current_status", "") or ""),
            "new_asset_status": str(raw.get("new_asset_status", "") or ""),
            "pc_toolkit_enriched_at": str(_mapping(raw.get("pc_toolkit")).get("enriched_at", "") or ""),
            "pc_toolkit_conflict_level": str(conflict.get("level", "") or ""),
            "pc_toolkit_conflict_message": str(conflict.get("text", "") or ""),
            "returned_serial_match": _bool(raw.get("returned_serial_match")),
            "content_signature": signature,
        }
        int_aliases = {
            "alm_row_number": "alm_row_number",
            "username_occurrence": "username_occurrence",
            "username_occurrence_total": "username_occurrence_total",
            "serial_validation_epoch": "serial_validation_epoch",
            "user_validation_epoch": "user_validation_epoch",
            "pc_toolkit_epoch": "pc_toolkit_epoch",
            "import_validation_epoch": "import_validation_epoch",
            "backlog_validation_epoch": "backlog_validation_epoch",
        }
        bool_aliases = {
            "attending": "attending",
            "new_joiner": "new_joiner",
            "has_returned_device_serial": "has_returned_device_serial",
            "has_pending_return_serial": "has_pending_return_serial",
            "returning_user_selected": "returning_user_selected",
            "user_selected": "user_selected",
            "cached_serial_verification": "cached_serial_verification",
            "cached_user_verification": "cached_user_verification",
            "included": "included",
            "default_excluded": "default_excluded",
            "backlog_ignored": "backlog_ignored",
            "manual_return_dismissed": "manual_return_dismissed",
            "pc_toolkit_default_excluded": "pc_toolkit_default_excluded",
        }
        for column, key in int_aliases.items():
            try:
                values[column] = int(raw[key]) if raw.get(key) is not None else 0
            except (TypeError, ValueError):
                values[column] = 0
        for column, key in bool_aliases.items():
            values[column] = _bool(raw.get(key))
        for column in (
            "import_validation", "import_error", "serial_validation",
            "serial_validation_error", "user_validation", "user_validation_error",
            "returning_user_validation", "returning_user_validation_error",
            "bulk_validation", "bulk_validation_error", "bulk_serial_mode",
            "manual_return_id", "manual_return_serial", "manual_return_type",
            "manual_return_status", "manual_return_auto_status", "manual_return_error",
            "manual_return_lookup_serial", "manual_return_lookup_state",
            "manual_return_lookup_error", "manual_return_model", "manual_return_source_id",
            "manual_username_source_id", "pc_toolkit_suggested_status",
        ):
            values[column] = str(raw.get(column, "") or "")
        request_columns = list(values)
        connection.execute(
            "DELETE FROM request_records WHERE request_id = ?", (request_id,)
        )
        connection.execute(
            f"INSERT INTO request_records ({', '.join(request_columns)}) "
            f"VALUES ({', '.join('?' for _ in request_columns)})",
            [values[column] for column in request_columns],
        )
        serial_states = _mapping(raw.get("bulk_serial_states"))
        serial_errors = _mapping(raw.get("bulk_serial_errors"))
        for position, serial in enumerate(serial_values):
            key = _normalise(serial)
            connection.execute(
                "INSERT INTO request_serials "
                "(request_id, position, serial, normalized_serial, validation_state, validation_error, queue_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id, position, serial, key,
                    str(serial_states.get(serial, "") or ""),
                    str(serial_errors.get(serial, "") or ""),
                    int(scope == "queue"),
                ),
            )
        for role, detail_key in (("user", "user_info"), ("returning_user", "returning_user_info")):
            self._person_columns(connection, request_id, role, raw.get(detail_key))
        for position, serial in enumerate(_list(raw.get("bulk_validation_missing"))):
            connection.execute(
                "INSERT INTO request_validation_items(request_id, group_name, item_key, position) VALUES (?, 'bulk_missing', ?, ?)",
                (request_id, str(serial), position),
            )
        for position, (serial, state) in enumerate(serial_states.items()):
            connection.execute(
                "INSERT INTO request_validation_items(request_id, group_name, item_key, state, error, position) "
                "VALUES (?, 'bulk_serial', ?, ?, ?, ?)",
                (request_id, str(serial), str(state or ""), str(serial_errors.get(serial, "") or ""), position),
            )
        for position, field_name in enumerate(_list(raw.get("import_failed_fields"))):
            connection.execute(
                "INSERT INTO request_import_failed_fields(request_id, field_name, position) VALUES (?, ?, ?)",
                (request_id, str(field_name), position),
            )
        for position, message in enumerate(_list(raw.get("errors"))):
            connection.execute(
                "INSERT INTO request_error_messages(request_id, position, message) VALUES (?, ?, ?)",
                (request_id, position, str(message)),
            )
        self._store_pc_links(connection, request_id, raw)
        max_portal = _mapping(raw.get("max_portal"))
        if max_portal:
            association = {
                field: (int(bool(max_portal.get(field))) if field in {"detail_loaded", "details_expanded"} else str(max_portal.get(field, "") or ""))
                for field in _MAX_PORTAL_FIELDS
            }
            connection.execute(
                f"INSERT INTO max_portal_associations(request_id, {', '.join(_MAX_PORTAL_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in range(len(_MAX_PORTAL_FIELDS) + 1))})",
                [request_id, *(association[field] for field in _MAX_PORTAL_FIELDS)],
            )
        return request_id

    def _load_request_record(self, connection: sqlite3.Connection, request_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM request_records WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return {}
        result: dict[str, Any] = {
            "id": row["request_id"],
            "kind": row["kind"],
            "status": row["status"],
            "user": row["user_login"],
            "returning": bool(row["returning_requested"]),
            "returning_user": row["returning_user_login"],
            "group": row["group_name"],
            "source": row["source"],
            "device_allocation": row["device_allocation"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "serials": [
                item["serial"]
                for item in connection.execute(
                    "SELECT serial FROM request_serials WHERE request_id = ? ORDER BY position",
                    (request_id,),
                )
            ],
        }
        if row["location_city"] or row["location_building"] or row["location_floor"] or row["location_room"]:
            location = {
                "city": row["location_city"], "building": row["location_building"],
                "floor": row["location_floor"], "room": row["location_room"],
                "cabinet": row["location_cabinet"],
            }
            location["display"] = " → ".join(
                value for value in (
                    location["city"], location["building"], location["floor"],
                    location["room"], location["cabinet"],
                ) if value
            )
            result["location"] = location
        else:
            result["location"] = None
        if row["scope"] == "alm":
            result.update({
                "username": row["username"], "serial": row["primary_serial"],
                "deployment_date": row["deployment_date"], "date": row["deployment_date"],
                "current_status": row["current_status"], "new_asset_status": row["new_asset_status"],
                "alm_row_number": row["alm_row_number"],
                "username_occurrence": row["username_occurrence"],
                "username_occurrence_total": row["username_occurrence_total"],
                "attending": _bool_value(row["attending"]),
                "new_joiner": _bool_value(row["new_joiner"]),
                "has_returned_device_serial": _bool_value(row["has_returned_device_serial"]),
                "has_pending_return_serial": _bool_value(row["has_pending_return_serial"]),
                "included": _bool_value(row["included"]),
                "default_excluded": _bool_value(row["default_excluded"]),
                "backlog_ignored": _bool_value(row["backlog_ignored"]),
                "import_validation": row["import_validation"],
                "import_error": row["import_error"],
                "import_validation_epoch": row["import_validation_epoch"],
                "backlog_validation_epoch": row["backlog_validation_epoch"],
                "serial_validation": row["serial_validation"],
                "serial_validation_error": row["serial_validation_error"],
                "user_validation": row["user_validation"],
                "user_validation_error": row["user_validation_error"],
                "returning_user_validation": row["returning_user_validation"],
                "returning_user_validation_error": row["returning_user_validation_error"],
                "cached_serial_verification": _bool_value(row["cached_serial_verification"]),
                "cached_user_verification": _bool_value(row["cached_user_verification"]),
                "manual_return_id": row["manual_return_id"],
                "manual_return_serial": row["manual_return_serial"],
                "manual_return_type": row["manual_return_type"],
                "manual_return_status": row["manual_return_status"],
                "manual_return_auto_status": row["manual_return_auto_status"],
                "manual_return_error": row["manual_return_error"],
                "manual_return_dismissed": _bool_value(row["manual_return_dismissed"]),
                "manual_return_lookup_serial": row["manual_return_lookup_serial"],
                "manual_return_lookup_state": row["manual_return_lookup_state"],
                "manual_return_lookup_error": row["manual_return_lookup_error"],
                "manual_return_model": row["manual_return_model"],
                "manual_return_source_id": row["manual_return_source_id"],
                "manual_username_source_id": row["manual_username_source_id"],
                "pc_toolkit_suggested_status": row["pc_toolkit_suggested_status"],
                "pc_toolkit_default_excluded": _bool_value(row["pc_toolkit_default_excluded"]),
                "returned_serial_match": _bool_value(row["returned_serial_match"]),
            })
            if row["kind"] == "" and row["group_name"] == "":
                result.pop("kind", None)
                result.pop("group", None)
        else:
            for field in (
                "serial_validation", "serial_validation_error", "serial_validation_epoch",
                "user_validation", "user_validation_error", "user_validation_epoch",
                "returning_user_validation", "returning_user_validation_error",
                "returning_user_selected", "user_selected", "cached_serial_verification",
                "cached_user_verification", "bulk_validation", "bulk_validation_error",
                "bulk_serial_mode", "pc_toolkit_epoch",
            ):
                column = field
                value = row[column]
                if field in _REQUEST_BOOL_FIELDS:
                    value = _bool_value(value)
                result[field] = value
        for role, target in (("user", "user_info"), ("returning_user", "returning_user_info")):
            detail = connection.execute(
                "SELECT login FROM request_person_details WHERE request_id = ? AND role = ?",
                (request_id, role),
            ).fetchone()
            if detail is not None:
                result[target] = {
                    "login": detail["login"],
                    "columns": [
                        item["column_value"]
                        for item in connection.execute(
                            "SELECT column_value FROM request_person_columns "
                            "WHERE request_id = ? AND role = ? ORDER BY position",
                            (request_id, role),
                        )
                    ],
                }
            else:
                result[target] = None
        missing = [
            item["item_key"]
            for item in connection.execute(
                "SELECT item_key FROM request_validation_items "
                "WHERE request_id = ? AND group_name = 'bulk_missing' ORDER BY position",
                (request_id,),
            )
        ]
        states: dict[str, str] = {}
        errors: dict[str, str] = {}
        for item in connection.execute(
            "SELECT item_key, state, error FROM request_validation_items "
            "WHERE request_id = ? AND group_name = 'bulk_serial' ORDER BY position",
            (request_id,),
        ):
            states[item["item_key"]] = item["state"]
            if item["error"]:
                errors[item["item_key"]] = item["error"]
        if missing or row["bulk_validation"]:
            result["bulk_validation_missing"] = missing
            result["bulk_serial_states"] = states
            result["bulk_serial_errors"] = errors
        failed = [
            item["field_name"]
            for item in connection.execute(
                "SELECT field_name FROM request_import_failed_fields "
                "WHERE request_id = ? ORDER BY position", (request_id,)
            )
        ]
        if failed:
            result["import_failed_fields"] = failed
        errors_list = [
            item["message"]
            for item in connection.execute(
                "SELECT message FROM request_error_messages WHERE request_id = ? ORDER BY position",
                (request_id,),
            )
        ]
        if errors_list:
            result["errors"] = errors_list
        links = connection.execute(
            "SELECT field_name, slot, lookup_id FROM request_pc_toolkit_links WHERE request_id = ?",
            (request_id,),
        ).fetchall()
        pc: dict[str, Any] = {}
        manual_return_pc = None
        for link in links:
            value = self._load_pc_lookup(connection, int(link["lookup_id"]))
            if link["field_name"] == "manual_return_pc_toolkit":
                manual_return_pc = value
            elif link["field_name"] == "pc_toolkit" and value is not None:
                field_name, _, slot = str(link["slot"]).partition(":")
                if field_name in {"serial", "user"}:
                    pc[field_name] = value
                elif field_name in {"serials", "bulk"}:
                    pc.setdefault(field_name, {})[slot] = value
        if pc:
            if row["pc_toolkit_enriched_at"]:
                pc["enriched_at"] = row["pc_toolkit_enriched_at"]
            result["pc_toolkit"] = pc
        elif row["scope"] != "alm":
            result["pc_toolkit"] = None
        if manual_return_pc is not None:
            result["manual_return_pc_toolkit"] = manual_return_pc
        portal = connection.execute(
            "SELECT * FROM max_portal_associations WHERE request_id = ?", (request_id,)
        ).fetchone()
        if portal is not None:
            result["max_portal"] = {
                field: (_bool_value(portal[field]) if field in {"detail_loaded", "details_expanded"} else portal[field])
                for field in _MAX_PORTAL_FIELDS
            }
        elif row["scope"] != "alm":
            result["max_portal"] = None
        if row["scope"] == "submission":
            serials = result["serials"]
            if result["kind"] == "user":
                destination = result.get("user", "")
            else:
                location = result.get("location") or {}
                destination = location.get("display", "")
            result["destination"] = destination
            result["device_count"] = len(serials)
        return result

    def _read_request_queue(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT request_id FROM request_records WHERE scope = 'queue' ORDER BY queue_position"
        ).fetchall()
        return [self._load_request_record(connection, row["request_id"]) for row in rows]

    @staticmethod
    def _deduplicate_queue(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique_requests: dict[str, dict[str, Any]] = {}
        for raw in requests:
            item = dict(raw)
            request_id = str(item.get("id", "") or "").strip()
            if request_id:
                # Match the browser merge helper: the last version of a
                # request wins, while its first position is retained.
                unique_requests[request_id] = item
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in unique_requests.values():
            serials = _list(item.get("serials"))
            if not serials and item.get("serial"):
                serials = [item["serial"]]
            unique = []
            for serial in serials:
                value = str(serial or "").strip()
                key = _normalise(value)
                if key and key not in seen:
                    unique.append(value)
                    seen.add(key)
            if serials and not unique:
                continue
            if "serials" in item:
                item["serials"] = unique
            elif unique:
                item["serial"] = unique[0]
            result.append(item)
        return result

    def _replace_request_queue(
        self,
        connection: sqlite3.Connection,
        requests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        cleaned = self._deduplicate_queue(requests)
        # Stale browser windows are reconciled against the latest committed
        # queue. Keep unchanged rows and their child relations; only remove
        # requests that are genuinely absent from the merged queue.
        active: list[dict[str, Any]] = []
        for item in cleaned:
            request_id = str(item.get("id", "") or "")
            if not request_id:
                continue
            # A request already in submission history must never be
            # resurrected by a stale browser window.
            submitted = connection.execute(
                "SELECT 1 FROM submission_entries WHERE request_id = ?", (request_id,)
            ).fetchone()
            if not submitted:
                active.append(item)
        final_ids = [str(item["id"]) for item in active]
        keep_ids = set(final_ids)
        existing_order = [
            str(row["request_id"])
            for row in connection.execute(
                "SELECT request_id FROM request_records WHERE scope = 'queue' ORDER BY queue_position"
            )
        ]
        existing_ids = set(existing_order)
        removed_ids = existing_ids - keep_ids
        if removed_ids:
            connection.executemany(
                "DELETE FROM request_records WHERE request_id = ?",
                [(request_id,) for request_id in removed_ids],
            )
        retained_order = [request_id for request_id in existing_order if request_id in keep_ids]
        if retained_order != final_ids:
            # Positions are unique. Stage them only when insertions or a
            # reorder can collide with existing positions. A simple removal
            # compacts positions leftward and can be updated without staging.
            connection.execute(
                "UPDATE request_records SET queue_position = -queue_position - 1 WHERE scope = 'queue'"
            )
        saved_position = 0
        for item in active:
            self._insert_request_record(
                connection, item, scope="queue", queue_position=saved_position
            )
            saved_position += 1
        return self._read_request_queue(connection)

    def load_request_queue(self) -> list[dict[str, Any]]:
        with self._read_snapshot() as connection:
            return self._read_request_queue(connection)

    def save_request_queue(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._write() as connection:
            return self._replace_request_queue(connection, requests)

    def mutate_request_queue(
        self,
        update: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        with self._write() as connection:
            current = self._read_request_queue(connection)
            return self._replace_request_queue(connection, update(current))

    # Workbook contents are the one intentional binary payload in the schema.
    # Their metadata, heading maps, dates, colour groups, and review decisions
    # remain queryable relational data.
    @staticmethod
    def _workbook_format(filename: str, summary: dict[str, Any] | None = None) -> str:
        configured = str(_mapping(summary).get("format", "") or "").casefold()
        return "csv" if configured == "csv" or filename.casefold().endswith(".csv") else "excel"

    def _save_alm_workbook_connection(
        self,
        connection: sqlite3.Connection,
        import_id: str,
        filename: str,
        content: bytes,
        columns: dict[str, Any] | None,
        *,
        saved_at: str | None = None,
    ) -> None:
        import_key = str(import_id).strip()
        if not import_key:
            raise DatabaseError("The ALM workbook import is missing its identifier.")
        content = bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        source_format = self._workbook_format(filename)
        now = saved_at or _now()
        connection.execute(
            "INSERT INTO alm_imports(import_id, filename, content, sha256, byte_count, source_format, "
            "has_sheets, supports_formatting, default_sheet, needs_mapping, saved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?) "
            "ON CONFLICT(import_id) DO UPDATE SET filename=excluded.filename, content=excluded.content, "
            "sha256=excluded.sha256, byte_count=excluded.byte_count, source_format=excluded.source_format, "
            "has_sheets=excluded.has_sheets, supports_formatting=excluded.supports_formatting, "
            "summary_signature='', saved_at=excluded.saved_at",
            (import_key, filename, content, digest, len(content), source_format,
             int(source_format != "csv"), int(source_format != "csv"), now),
        )
        connection.execute("DELETE FROM alm_import_columns WHERE import_id = ?", (import_key,))
        connection.executemany(
            "INSERT INTO alm_import_columns(import_id, field_name, heading) VALUES (?, ?, ?)",
            [
                (import_key, field_name, str(_mapping(columns).get(field_name, "") or ""))
                for field_name in _IMPORT_COLUMN_NAMES
            ] if columns else [],
        )

    def save_alm_workbook(
        self,
        import_id: str,
        filename: str,
        content: bytes,
        columns: dict[str, Any] | None = None,
        *,
        saved_at: str | None = None,
    ) -> None:
        with self._write() as connection:
            self._save_alm_workbook_connection(
                connection, import_id, filename, content, columns, saved_at=saved_at
            )

    def load_alm_workbook(self, import_id: str) -> tuple[str, bytes, dict[str, str]] | None:
        with self._read_snapshot() as connection:
            row = connection.execute(
                "SELECT filename, content FROM alm_imports WHERE import_id = ?",
                (str(import_id),),
            ).fetchone()
            if row is None:
                return None
            columns = {
                item["field_name"]: item["heading"]
                for item in connection.execute(
                    "SELECT field_name, heading FROM alm_import_columns WHERE import_id = ?",
                    (str(import_id),),
                )
            }
            return row["filename"], bytes(row["content"]), columns

    def _store_workbook_summary(
        self,
        connection: sqlite3.Connection,
        workbook: dict[str, Any],
    ) -> None:
        import_id = str(workbook.get("import_id", "") or "").strip()
        if not import_id:
            return
        filename = str(workbook.get("filename", "ALM Workbook") or "ALM Workbook")
        signature = _content_signature(workbook)
        existing = connection.execute(
            "SELECT content, summary_signature FROM alm_imports WHERE import_id = ?", (import_id,)
        ).fetchone()
        if existing is not None and existing["summary_signature"] == signature:
            return
        if existing is None:
            # Draft migrations may contain a workbook summary whose source
            # file was already removed. Keep the review state, but make the
            # missing payload explicit so resume can report it cleanly.
            content = b""
            source_format = self._workbook_format(filename, workbook)
            connection.execute(
                "INSERT INTO alm_imports(import_id, filename, content, sha256, byte_count, source_format, "
                "has_sheets, supports_formatting, default_sheet, needs_mapping, has_inspection, "
                "inspection_default_sheet, saved_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, '', ?)",
                (
                    import_id, filename, content, hashlib.sha256(content).hexdigest(), source_format,
                    int(bool(workbook.get("has_sheets", source_format != "csv"))),
                    int(bool(workbook.get("supports_formatting", source_format != "csv"))),
                    str(workbook.get("default_sheet", "") or ""),
                    int(bool(workbook.get("needs_mapping"))), _now(),
                ),
            )
        else:
            source_format = self._workbook_format(filename, workbook)
            connection.execute(
                "UPDATE alm_imports SET filename=?, source_format=?, has_sheets=?, supports_formatting=?, "
                "default_sheet=?, needs_mapping=?, has_inspection=? WHERE import_id=?",
                (
                    filename, source_format,
                    int(bool(workbook.get("has_sheets", source_format != "csv"))),
                    int(bool(workbook.get("supports_formatting", source_format != "csv"))),
                    str(workbook.get("default_sheet", "") or ""),
                    int(bool(workbook.get("needs_mapping"))),
                    int(isinstance(workbook.get("inspection"), dict)), import_id,
                ),
            )
        connection.execute("DELETE FROM alm_sheets WHERE import_id = ?", (import_id,))
        connection.execute("DELETE FROM alm_inspection_sheets WHERE import_id = ?", (import_id,))
        sheets = _list(workbook.get("sheets"))
        for position, sheet in enumerate(sheets):
            sheet_data = _mapping(sheet)
            name = str(sheet_data.get("name", "") or "")
            if not name:
                continue
            connection.execute(
                "INSERT INTO alm_sheets(import_id, sheet_name, position) VALUES (?, ?, ?)",
                (import_id, name, position),
            )
            connection.executemany(
                "INSERT INTO alm_sheet_headings(import_id, sheet_name, position, heading) VALUES (?, ?, ?, ?)",
                [
                    (import_id, name, index, str(heading))
                    for index, heading in enumerate(_list(sheet_data.get("headings")))
                ],
            )
            for date_data in _list(sheet_data.get("dates")):
                date_info = _mapping(date_data)
                date_value = str(date_info.get("value", "") or "")
                if not date_value:
                    continue
                connection.execute(
                    "INSERT INTO alm_sheet_dates(import_id, sheet_name, date_value, label, deployment_count, "
                    "missing_username_deployment_count, returned_device_count, pending_return_count, row_count, eligible_row_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        import_id, name, date_value, str(date_info.get("label", "") or ""),
                        int(date_info.get("deployment_count", 0) or 0),
                        int(date_info.get("missing_username_deployment_count", 0) or 0),
                        int(date_info.get("returned_device_count", 0) or 0),
                        int(date_info.get("pending_return_count", 0) or 0),
                        int(date_info.get("row_count", 0) or 0),
                        int(date_info.get("eligible_row_count", 0) or 0),
                    ),
                )
                for group in _list(date_info.get("groups")):
                    group_info = _mapping(group)
                    connection.execute(
                        "INSERT INTO alm_sheet_date_groups(import_id, sheet_name, date_value, group_value, row_count, "
                        "eligible_row_count, deployment_count, missing_username_deployment_count, returned_device_count, pending_return_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            import_id, name, date_value, str(group_info.get("value", "") or ""),
                            int(group_info.get("row_count", 0) or 0),
                            int(group_info.get("eligible_row_count", 0) or 0),
                            int(group_info.get("deployment_count", 0) or 0),
                            int(group_info.get("missing_username_deployment_count", 0) or 0),
                            int(group_info.get("returned_device_count", 0) or 0),
                            int(group_info.get("pending_return_count", 0) or 0),
                        ),
                    )
                for warning in _list(date_info.get("warnings")):
                    warning_info = _mapping(warning)
                    row_number = int(warning_info.get("row_number", 0) or 0)
                    if row_number <= 0:
                        continue
                    connection.execute(
                        "INSERT INTO alm_sheet_warnings(import_id, sheet_name, date_value, row_number, username, missing_returned, missing_pending) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            import_id, name, date_value, row_number,
                            str(warning_info.get("username", "") or ""),
                            int(bool(warning_info.get("missing_returned"))),
                            int(bool(warning_info.get("missing_pending"))),
                        ),
                    )
        inspection = _mapping(workbook.get("inspection"))
        for position, sheet in enumerate(_list(inspection.get("sheets"))):
            sheet_data = _mapping(sheet)
            name = str(sheet_data.get("name", "") or "")
            if not name:
                continue
            connection.execute(
                "INSERT INTO alm_inspection_sheets(import_id, sheet_name, position) VALUES (?, ?, ?)",
                (import_id, name, position),
            )
            connection.executemany(
                "INSERT INTO alm_inspection_headings(import_id, sheet_name, position, heading) VALUES (?, ?, ?, ?)",
                [
                    (import_id, name, index, str(heading))
                    for index, heading in enumerate(_list(sheet_data.get("headings")))
                ],
            )
        if inspection:
            connection.execute(
                "UPDATE alm_imports SET has_inspection=1, inspection_default_sheet=? WHERE import_id=?",
                (str(inspection.get("default_sheet", "") or ""), import_id),
            )
        connection.execute(
            "UPDATE alm_imports SET summary_signature=? WHERE import_id=?",
            (signature, import_id),
        )

    def _load_workbook_summary(
        self, connection: sqlite3.Connection, import_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM alm_imports WHERE import_id = ?", (import_id,)
        ).fetchone()
        if row is None:
            return None
        workbook: dict[str, Any] = {
            "import_id": import_id,
            "filename": row["filename"],
            "format": row["source_format"],
            "has_sheets": bool(row["has_sheets"]),
            "supports_formatting": bool(row["supports_formatting"]),
            "default_sheet": row["default_sheet"],
        }
        if row["needs_mapping"]:
            workbook["needs_mapping"] = True
        sheet_values = []
        for sheet in connection.execute(
            "SELECT sheet_name FROM alm_sheets WHERE import_id = ? ORDER BY position",
            (import_id,),
        ):
            name = sheet["sheet_name"]
            value: dict[str, Any] = {"name": name}
            headings = [
                item["heading"]
                for item in connection.execute(
                    "SELECT heading FROM alm_sheet_headings WHERE import_id=? AND sheet_name=? ORDER BY position",
                    (import_id, name),
                )
            ]
            if headings:
                value["headings"] = headings
            dates = []
            for date_row in connection.execute(
                "SELECT * FROM alm_sheet_dates WHERE import_id=? AND sheet_name=? ORDER BY date_value DESC",
                (import_id, name),
            ):
                date_value = date_row["date_value"]
                groups = [
                    {
                        "value": group["group_value"], "row_count": group["row_count"],
                        "eligible_row_count": group["eligible_row_count"],
                        "deployment_count": group["deployment_count"],
                        "missing_username_deployment_count": group["missing_username_deployment_count"],
                        "returned_device_count": group["returned_device_count"],
                        "pending_return_count": group["pending_return_count"],
                    }
                    for group in connection.execute(
                        "SELECT * FROM alm_sheet_date_groups WHERE import_id=? AND sheet_name=? AND date_value=? ORDER BY CAST(group_value AS INTEGER)",
                        (import_id, name, date_value),
                    )
                ]
                warnings = [
                    {
                        "row_number": warning["row_number"], "username": warning["username"],
                        "missing_returned": bool(warning["missing_returned"]),
                        "missing_pending": bool(warning["missing_pending"]),
                    }
                    for warning in connection.execute(
                        "SELECT * FROM alm_sheet_warnings WHERE import_id=? AND sheet_name=? AND date_value=? ORDER BY row_number",
                        (import_id, name, date_value),
                    )
                ]
                dates.append({
                    "value": date_value, "label": date_row["label"],
                    "deployment_count": date_row["deployment_count"],
                    "missing_username_deployment_count": date_row["missing_username_deployment_count"],
                    "returned_device_count": date_row["returned_device_count"],
                    "pending_return_count": date_row["pending_return_count"],
                    "row_count": date_row["row_count"],
                    "eligible_row_count": date_row["eligible_row_count"],
                    "groups": groups, "warnings": warnings,
                })
            if dates:
                value["dates"] = dates
            sheet_values.append(value)
        workbook["sheets"] = sheet_values
        if row["has_inspection"]:
            inspection = {
                "filename": workbook["filename"],
                "format": workbook["format"],
                "has_sheets": workbook["has_sheets"],
                "supports_formatting": workbook["supports_formatting"],
                "needs_mapping": True,
                "default_sheet": row["inspection_default_sheet"],
                "sheets": [],
            }
            for sheet in connection.execute(
                "SELECT sheet_name FROM alm_inspection_sheets WHERE import_id=? ORDER BY position",
                (import_id,),
            ):
                name = sheet["sheet_name"]
                inspection["sheets"].append({
                    "name": name,
                    "headings": [
                        item["heading"]
                        for item in connection.execute(
                            "SELECT heading FROM alm_inspection_headings WHERE import_id=? AND sheet_name=? ORDER BY position",
                            (import_id, name),
                        )
                    ],
                })
            workbook["inspection"] = inspection
        return workbook

    def _save_alm_draft_connection(
        self,
        connection: sqlite3.Connection,
        draft: dict[str, Any],
        *,
        limit: int,
    ) -> None:
        draft_id = str(draft.get("id", "") or "").strip()
        workbook = _mapping(draft.get("workbook"))
        import_id = str(draft.get("import_id") or workbook.get("import_id") or "").strip()
        if not draft_id or not import_id:
            raise DatabaseError("The ALM draft is missing its draft or workbook identifier.")
        self._store_workbook_summary(connection, workbook)
        settings = _mapping(draft.get("settings"))
        location = _mapping(settings.get("location"))
        phase = str(draft.get("phase", "import") or "import")
        if phase not in {"mapping", "options", "review", "import"}:
            phase = "import"
        saved_at = str(draft.get("saved_at", "") or _now())
        preview = _mapping(draft.get("preview"))
        incoming_requests = [
            _mapping(item) for item in _list(preview.get("requests"))
            if _mapping(item).get("id")
        ]
        incoming_ids = {str(item.get("id")) for item in incoming_requests}
        existing_order = [
            str(row["request_id"])
            for row in connection.execute(
                "SELECT request_id FROM request_records WHERE scope='alm' AND draft_id=? "
                "ORDER BY draft_position",
                (draft_id,),
            )
        ]
        existing_ids = set(existing_order)
        incoming_order = [str(item.get("id")) for item in incoming_requests]
        keep_ids = set(incoming_order)
        removed_ids = existing_ids - incoming_ids
        if removed_ids:
            connection.executemany(
                "DELETE FROM request_records WHERE request_id = ?",
                [(request_id,) for request_id in removed_ids],
            )
        # Save the draft header in place. Replacing it would cascade-delete all
        # of its related rows (including potentially thousands of reviewed
        # requests) on every autosave.
        connection.execute(
            "INSERT INTO alm_import_drafts(draft_id, import_id, filename, phase, saved_at, mode, selected_sheet, "
            "location_city, location_building, location_floor, location_room, location_cabinet, returned_serials_on_hand, "
            "backlog_days, backlog_include_today) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(draft_id) DO UPDATE SET import_id=excluded.import_id, filename=excluded.filename, "
            "phase=excluded.phase, saved_at=excluded.saved_at, mode=excluded.mode, "
            "selected_sheet=excluded.selected_sheet, location_city=excluded.location_city, "
            "location_building=excluded.location_building, location_floor=excluded.location_floor, "
            "location_room=excluded.location_room, location_cabinet=excluded.location_cabinet, "
            "returned_serials_on_hand=excluded.returned_serials_on_hand, backlog_days=excluded.backlog_days, "
            "backlog_include_today=excluded.backlog_include_today",
            (
                draft_id, import_id, str(draft.get("filename", workbook.get("filename", "ALM Workbook")) or "ALM Workbook"),
                phase, saved_at, str(settings.get("mode", "") or ""),
                str(settings.get("sheet", "") or ""),
                str(location.get("city", "") or ""), str(location.get("building", "") or ""),
                str(location.get("floor", "") or ""), str(location.get("room", "") or ""),
                str(location.get("cabinet", "") or ""),
                str(settings.get("returned_serials_on_hand", "") or ""),
                max(1, min(3650, int(settings.get("backlog_days", 30) or 30))),
                int(bool(settings.get("backlog_include_today"))),
            ),
        )
        for table in (
            "alm_draft_dates", "alm_draft_groups", "alm_draft_modes", "alm_draft_columns",
            "alm_draft_ignored_reasons", "alm_draft_selected_dates",
            "alm_draft_missing_user_warnings",
        ):
            connection.execute(f"DELETE FROM {table} WHERE draft_id = ?", (draft_id,))
        connection.execute("DELETE FROM alm_draft_preview WHERE draft_id = ?", (draft_id,))
        retained_order = [request_id for request_id in existing_order if request_id in keep_ids]
        if retained_order != incoming_order:
            connection.execute(
                "UPDATE request_records SET draft_position = -draft_position - 1 "
                "WHERE scope='alm' AND draft_id=?",
                (draft_id,),
            )
        connection.executemany(
            "INSERT INTO alm_draft_dates(draft_id, position, date_value) VALUES (?, ?, ?)",
            [(draft_id, pos, str(value)) for pos, value in enumerate(_list(settings.get("dates")))],
        )
        connection.executemany(
            "INSERT INTO alm_draft_groups(draft_id, date_value, group_value) VALUES (?, ?, ?)",
            [
                (draft_id, str(key), str(value))
                for key, value in _mapping(settings.get("groups")).items()
            ],
        )
        connection.executemany(
            "INSERT INTO alm_draft_modes(draft_id, position, mode) VALUES (?, ?, ?)",
            [(draft_id, pos, str(value)) for pos, value in enumerate(_list(settings.get("modes")))],
        )
        connection.executemany(
            "INSERT INTO alm_draft_columns(draft_id, field_name, heading) VALUES (?, ?, ?)",
            [
                (draft_id, str(key), str(value or ""))
                for key, value in _mapping(settings.get("columns")).items()
            ],
        )
        if preview:
            counts = _mapping(preview.get("counts"))
            connection.execute(
                "INSERT INTO alm_draft_preview(draft_id, mode, sheet_name, days_back, include_today, start_date, end_date, "
                "ignored_count, returned_serials_on_hand, request_count, deployment_count, returned_device_count, "
                "pending_return_count, candidate_count, already_deployed_count, ignored_row_count, outside_range_count, "
                "today_excluded_count, missing_serial_count, missing_username_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft_id, str(preview.get("mode", "") or ""), str(preview.get("sheet", "") or ""),
                    int(preview.get("days_back", 0) or 0), int(bool(preview.get("include_today"))),
                    str(preview.get("start_date", "") or ""), str(preview.get("end_date", "") or ""),
                    int(preview.get("ignored_count", 0) or 0),
                    str(preview.get("returned_serials_on_hand", "") or ""),
                    int(counts.get("requests", 0) or 0), int(counts.get("deployments", 0) or 0),
                    int(counts.get("returned_devices", 0) or 0), int(counts.get("pending_returns", 0) or 0),
                    int(counts.get("candidates", 0) or 0), int(counts.get("already_deployed", 0) or 0),
                    int(counts.get("ignored", 0) or 0), int(counts.get("outside_range", 0) or 0),
                    int(counts.get("today_excluded", 0) or 0), int(counts.get("missing_serial", 0) or 0),
                    int(counts.get("missing_username", 0) or 0),
                ),
            )
            connection.executemany(
                "INSERT INTO alm_draft_ignored_reasons(draft_id, reason, item_count) VALUES (?, ?, ?)",
                [
                    (draft_id, str(_mapping(value).get("reason", "") or ""), int(_mapping(value).get("count", 0) or 0))
                    for value in _list(preview.get("ignored"))
                    if _mapping(value).get("reason")
                ],
            )
            connection.executemany(
                "INSERT INTO alm_draft_selected_dates(draft_id, position, date_value) VALUES (?, ?, ?)",
                [(draft_id, pos, str(value)) for pos, value in enumerate(_list(preview.get("dates")))],
            )
            warning_rows = _list(_mapping(preview.get("warnings")).get("missing_username_deployments"))
            for position, warning in enumerate(warning_rows):
                item = _mapping(warning)
                row_number = int(item.get("row_number", 0) or 0)
                if row_number <= 0:
                    continue
                connection.execute(
                    "INSERT INTO alm_draft_missing_user_warnings(draft_id, position, row_number, date_value, serial, status, "
                    "status_preselected, device_allocation, new_asset_status, has_returned_device_serial, has_pending_return_serial, "
                    "new_joiner, first_name, last_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        draft_id, position, row_number, str(item.get("date", "") or ""),
                        str(item.get("serial", "") or ""), str(item.get("status", "") or ""),
                        int(bool(item.get("status_preselected"))), str(item.get("device_allocation", "") or ""),
                        str(item.get("new_asset_status", "") or ""),
                        int(bool(item.get("has_returned_device_serial"))),
                        int(bool(item.get("has_pending_return_serial"))), int(bool(item.get("new_joiner"))),
                        str(item.get("first_name", "") or ""), str(item.get("last_name", "") or ""),
                    ),
                )
            for position, request in enumerate(incoming_requests):
                self._insert_request_record(
                    connection, request, scope="alm", draft_id=draft_id,
                    draft_position=position,
                )
        excess = connection.execute(
            "SELECT draft_id FROM alm_import_drafts ORDER BY saved_at DESC LIMIT -1 OFFSET ?",
            (max(1, limit),),
        ).fetchall()
        if excess:
            connection.executemany(
                "DELETE FROM alm_import_drafts WHERE draft_id = ?",
                [(row["draft_id"],) for row in excess],
            )

    def save_alm_draft(self, draft: dict[str, Any], *, limit: int = MAX_DRAFTS) -> None:
        with self._write() as connection:
            self._save_alm_draft_connection(connection, draft, limit=limit)

    def replace_alm_drafts(
        self, drafts: list[dict[str, Any]], *, limit: int = MAX_DRAFTS
    ) -> None:
        with self._write() as connection:
            connection.execute("DELETE FROM alm_import_drafts")
            for draft in _list(drafts)[:max(1, limit)]:
                self._save_alm_draft_connection(connection, _mapping(draft), limit=limit)

    def _load_alm_draft(self, connection: sqlite3.Connection, draft_row: sqlite3.Row) -> dict[str, Any]:
        draft_id = str(draft_row["draft_id"])
        import_id = str(draft_row["import_id"])
        workbook = self._load_workbook_summary(connection, import_id) or {
            "import_id": import_id, "filename": draft_row["filename"], "sheets": []
        }
        settings = {
            "mode": draft_row["mode"], "sheet": draft_row["selected_sheet"],
            "dates": [
                item["date_value"] for item in connection.execute(
                    "SELECT date_value FROM alm_draft_dates WHERE draft_id=? ORDER BY position", (draft_id,)
                )
            ],
            "groups": {
                item["date_value"]: item["group_value"]
                for item in connection.execute(
                    "SELECT date_value, group_value FROM alm_draft_groups WHERE draft_id=?", (draft_id,)
                )
            },
            "modes": [
                item["mode"] for item in connection.execute(
                    "SELECT mode FROM alm_draft_modes WHERE draft_id=? ORDER BY position", (draft_id,)
                )
            ],
            "location": {
                "city": draft_row["location_city"],
                "building": draft_row["location_building"],
                "floor": draft_row["location_floor"],
                "room": draft_row["location_room"],
                "cabinet": draft_row["location_cabinet"],
            } if any(draft_row[field] for field in (
                "location_city", "location_building", "location_floor", "location_room", "location_cabinet"
            )) else None,
            "columns": {
                item["field_name"]: item["heading"]
                for item in connection.execute(
                    "SELECT field_name, heading FROM alm_draft_columns WHERE draft_id=?", (draft_id,)
                )
            },
            "returned_serials_on_hand": draft_row["returned_serials_on_hand"],
            "backlog_days": draft_row["backlog_days"],
            "backlog_include_today": bool(draft_row["backlog_include_today"]),
        }
        preview_row = connection.execute(
            "SELECT * FROM alm_draft_preview WHERE draft_id=?", (draft_id,)
        ).fetchone()
        preview: dict[str, Any] | None = None
        if preview_row is not None:
            preview = {
                "mode": preview_row["mode"],
                "filename": draft_row["filename"],
                "sheet": preview_row["sheet_name"],
                "days_back": preview_row["days_back"],
                "include_today": bool(preview_row["include_today"]),
                "start_date": preview_row["start_date"],
                "end_date": preview_row["end_date"],
                "ignored_count": preview_row["ignored_count"],
                "returned_serials_on_hand": preview_row["returned_serials_on_hand"],
                "counts": {
                    "requests": preview_row["request_count"],
                    "deployments": preview_row["deployment_count"],
                    "returned_devices": preview_row["returned_device_count"],
                    "pending_returns": preview_row["pending_return_count"],
                    "candidates": preview_row["candidate_count"],
                    "already_deployed": preview_row["already_deployed_count"],
                    "ignored": preview_row["ignored_row_count"],
                    "outside_range": preview_row["outside_range_count"],
                    "today_excluded": preview_row["today_excluded_count"],
                    "missing_serial": preview_row["missing_serial_count"],
                    "missing_username": preview_row["missing_username_count"],
                },
                "ignored": [
                    {"reason": row["reason"], "count": row["item_count"]}
                    for row in connection.execute(
                        "SELECT reason, item_count FROM alm_draft_ignored_reasons WHERE draft_id=? ORDER BY reason",
                        (draft_id,),
                    )
                ],
                "dates": [
                    row["date_value"] for row in connection.execute(
                        "SELECT date_value FROM alm_draft_selected_dates WHERE draft_id=? ORDER BY position",
                        (draft_id,),
                    )
                ],
                "warnings": {"missing_username_deployments": [
                    {
                        "row_number": row["row_number"], "date": row["date_value"],
                        "serial": row["serial"], "status": row["status"],
                        "status_preselected": bool(row["status_preselected"]),
                        "device_allocation": row["device_allocation"],
                        "new_asset_status": row["new_asset_status"],
                        "has_returned_device_serial": bool(row["has_returned_device_serial"]),
                        "has_pending_return_serial": bool(row["has_pending_return_serial"]),
                        "new_joiner": bool(row["new_joiner"]),
                        "first_name": row["first_name"], "last_name": row["last_name"],
                    }
                    for row in connection.execute(
                        "SELECT * FROM alm_draft_missing_user_warnings WHERE draft_id=? ORDER BY position",
                        (draft_id,),
                    )
                ]},
                "requests": [
                    self._load_request_record(connection, row["request_id"])
                    for row in connection.execute(
                        "SELECT request_id FROM request_records WHERE scope='alm' AND draft_id=? ORDER BY draft_position",
                        (draft_id,),
                    )
                ],
            }
            if preview["mode"] == "backlog":
                preview["candidates"] = [dict(item) for item in preview["requests"]]
        return {
            "id": draft_id,
            "filename": draft_row["filename"],
            "import_id": import_id,
            "workbook": workbook,
            "phase": draft_row["phase"],
            "settings": settings,
            "preview": preview,
            "saved_at": draft_row["saved_at"],
        }

    def load_alm_drafts(self, *, limit: int = MAX_DRAFTS) -> list[dict[str, Any]]:
        with self._read_snapshot() as connection:
            rows = connection.execute(
                "SELECT * FROM alm_import_drafts ORDER BY saved_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
            return [self._load_alm_draft(connection, row) for row in rows]

    def delete_alm_draft(self, draft_id: str) -> None:
        with self._write() as connection:
            connection.execute("DELETE FROM alm_import_drafts WHERE draft_id = ?", (draft_id,))

    def load_backlog_ignores(self) -> dict[str, dict[str, str]]:
        with self._read_snapshot() as connection:
            return {
                f"{row['serial_normalized']}\0{row['username_normalized']}": {
                    "serial": row["serial"], "username": row["username"]
                }
                for row in connection.execute(
                    "SELECT * FROM alm_backlog_ignores ORDER BY ignored_at"
                )
            }

    def set_backlog_ignore(self, serial: str, username: str, ignored: bool) -> None:
        serial_display = " ".join(str(serial or "").split())
        user_display = " ".join(str(username or "").split())
        serial_key, user_key = _normalise(serial_display), _normalise(user_display)
        if not serial_key or not user_key:
            raise DatabaseError("An ignored ALM row needs both a serial and username.")
        with self._write() as connection:
            if ignored:
                connection.execute(
                    "INSERT INTO alm_backlog_ignores(serial_normalized, username_normalized, serial, username, ignored_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(serial_normalized, username_normalized) DO UPDATE SET "
                    "serial=excluded.serial, username=excluded.username, ignored_at=excluded.ignored_at",
                    (serial_key, user_key, serial_display, user_display, _now()),
                )
            else:
                connection.execute(
                    "DELETE FROM alm_backlog_ignores WHERE serial_normalized=? AND username_normalized=?",
                    (serial_key, user_key),
                )

    def clear_backlog_ignores(self) -> None:
        with self._write() as connection:
            connection.execute("DELETE FROM alm_backlog_ignores")

    def replace_backlog_ignores(self, values: dict[str, dict[str, str]]) -> None:
        with self._write() as connection:
            connection.execute("DELETE FROM alm_backlog_ignores")
            for entry in values.values():
                item = _mapping(entry)
                serial = " ".join(str(item.get("serial", "") or "").split())
                username = " ".join(str(item.get("username", "") or "").split())
                serial_key, username_key = _normalise(serial), _normalise(username)
                if not serial_key or not username_key:
                    continue
                connection.execute(
                    "INSERT INTO alm_backlog_ignores(serial_normalized, username_normalized, serial, username, ignored_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (serial_key, username_key, serial, username, _now()),
                )

    def load_verification_cache(self) -> dict[str, dict[str, dict[str, Any]]]:
        values: dict[str, dict[str, dict[str, Any]]] = {"serials": {}, "usernames": {}}
        with self._read_snapshot() as connection:
            rows = connection.execute(
                "SELECT category, canonical_key, display_value, device_type, verified_at "
                "FROM verification_records ORDER BY last_used_at"
            ).fetchall()
            aliases_by_record: dict[tuple[str, str], list[str]] = {}
            for alias in connection.execute(
                "SELECT category, canonical_key, alias_display FROM verification_aliases "
                "WHERE alias_key <> canonical_key ORDER BY ordinal"
            ):
                aliases_by_record.setdefault(
                    (alias["category"], alias["canonical_key"]), []
                ).append(alias["alias_display"])
            for row in rows:
                entry: dict[str, Any] = {"value": row["display_value"], "columns": []}
                if row["category"] == "serials":
                    entry["device_type"] = row["device_type"]
                if row["verified_at"]:
                    entry["verified_at"] = row["verified_at"]
                entry["columns"] = aliases_by_record.get(
                    (row["category"], row["canonical_key"]), []
                )
                values[row["category"]][row["canonical_key"]] = entry
        return values

    def lookup_verification(self, category: str, value: str) -> dict[str, Any] | None:
        """Look up one serial or username through its indexed canonical/alias key."""
        if category not in {"serials", "usernames"}:
            return None
        alias_key = _normalise(value)
        if not alias_key:
            return None
        with self._write() as connection:
            row = connection.execute(
                "SELECT r.category, r.canonical_key, r.display_value, r.device_type, r.verified_at "
                "FROM verification_aliases AS a "
                "JOIN verification_records AS r "
                "ON r.category = a.category AND r.canonical_key = a.canonical_key "
                "WHERE a.category = ? AND a.alias_key = ?",
                (category, alias_key),
            ).fetchone()
            if row is None:
                # Also support records written before aliases were introduced.
                row = connection.execute(
                    "SELECT category, canonical_key, display_value, device_type, verified_at "
                    "FROM verification_records WHERE category = ? AND canonical_key = ?",
                    (category, alias_key),
                ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE verification_records SET last_used_at = ? "
                "WHERE category = ? AND canonical_key = ?",
                (_now(), row["category"], row["canonical_key"]),
            )
            aliases = [
                item["alias_display"]
                for item in connection.execute(
                    "SELECT alias_display FROM verification_aliases "
                    "WHERE category = ? AND canonical_key = ? AND alias_key <> canonical_key "
                    "ORDER BY ordinal",
                    (row["category"], row["canonical_key"]),
                )
            ]
            result: dict[str, Any] = {
                "value": row["display_value"],
                "columns": aliases,
            }
            if row["category"] == "serials":
                result["device_type"] = row["device_type"]
            if row["verified_at"]:
                result["verified_at"] = row["verified_at"]
            return result

    def merge_verification_cache(
        self,
        cache: dict[str, dict[str, dict[str, Any]]],
        *,
        limit: int = MAX_VERIFICATION_ITEMS,
    ) -> None:
        now = _now()
        with self._write() as connection:
            for category in ("serials", "usernames"):
                incoming = _mapping(cache.get(category))
                for canonical_key, raw in incoming.items():
                    entry = _mapping(raw)
                    display = str(entry.get("value", canonical_key) or canonical_key).strip()
                    key = _normalise(canonical_key or display)
                    if not key:
                        continue
                    verified_at = str(entry.get("verified_at", "") or now)
                    connection.execute(
                        "INSERT INTO verification_records(category, canonical_key, display_value, device_type, verified_at, last_used_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(category, canonical_key) DO UPDATE SET "
                        "display_value=excluded.display_value, device_type=excluded.device_type, "
                        "verified_at=excluded.verified_at, last_used_at=excluded.last_used_at",
                        (
                            category, key, display,
                            str(entry.get("device_type", "") or "") if category == "serials" else "",
                            verified_at, now,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM verification_aliases WHERE category=? AND canonical_key=?",
                        (category, key),
                    )
                    aliases = [display, *[str(value) for value in _list(entry.get("columns"))]]
                    unique_aliases: list[tuple[str, str]] = []
                    seen: set[str] = set()
                    for alias in aliases:
                        alias_key = _normalise(alias)
                        if alias_key and alias_key not in seen:
                            unique_aliases.append((alias_key, alias))
                            seen.add(alias_key)
                    # The stored canonical query is itself a searchable alias.
                    unique_aliases.insert(0, (key, display))
                    connection.executemany(
                        "INSERT INTO verification_aliases(category, alias_key, alias_display, canonical_key, ordinal) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(category, alias_key) DO UPDATE SET "
                        "canonical_key=excluded.canonical_key, ordinal=excluded.ordinal, alias_display=excluded.alias_display",
                        [
                            (category, alias_key, alias_display, key, position)
                            for position, (alias_key, alias_display) in enumerate(unique_aliases)
                        ],
                    )
                excess = connection.execute(
                    "SELECT canonical_key FROM verification_records WHERE category=? "
                    "ORDER BY last_used_at DESC LIMIT -1 OFFSET ?",
                    (category, limit),
                ).fetchall()
                if excess:
                    connection.executemany(
                        "DELETE FROM verification_records WHERE category=? AND canonical_key=?",
                        [(category, row["canonical_key"]) for row in excess],
                    )
        return None

    def load_request_history(self, *, limit: int = MAX_HISTORY_JOBS) -> list[dict[str, Any]]:
        with self._read_snapshot() as connection:
            jobs = connection.execute(
                "SELECT * FROM submission_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
            history = []
            for job in jobs:
                entries = []
                for entry in connection.execute(
                    "SELECT * FROM submission_entries WHERE job_id=? ORDER BY position",
                    (job["job_id"],),
                ):
                    item = self._load_request_record(connection, entry["request_id"])
                    item.update({
                        "state": entry["state"], "message": entry["message"],
                        "step": entry["step"], "step_count": entry["step_count"],
                        "progress_percent": round((entry["step"] / entry["step_count"]) * 100)
                        if entry["step_count"] else 0,
                        "request_id": entry["request_id_remote"] or None,
                        "order_id": entry["order_id"] or None,
                        "elapsed_seconds": entry["elapsed_seconds"],
                    })
                    entries.append(item)
                counts: dict[str, int] = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0}
                for entry in entries:
                    state = str(entry.get("state", ""))
                    counts[state] = counts.get(state, 0) + 1
                counts["total"] = len(entries)
                counts["devices"] = sum(len(_list(entry.get("serials"))) for entry in entries)
                history.append({
                    "job_id": job["job_id"], "state": job["state"],
                    "created_at": job["created_at"], "finished_at": job["finished_at"],
                    "request_for": job["request_for"], "simulation": bool(job["simulation"]),
                    "counts": counts, "entries": entries,
                })
            return history

    def _save_history_job(
        self,
        connection: sqlite3.Connection,
        job: dict[str, Any],
    ) -> None:
        job_id = str(job.get("job_id", "") or "").strip()
        if not job_id:
            return
        connection.execute("DELETE FROM submission_jobs WHERE job_id=?", (job_id,))
        connection.execute(
            "INSERT INTO submission_jobs(job_id, state, created_at, finished_at, request_for, simulation) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id, str(job.get("state", "finished") or "finished"),
                str(job.get("created_at", "") or _now()), job.get("finished_at"),
                str(job.get("request_for", "") or ""), int(bool(job.get("simulation"))),
            ),
        )
        for position, raw in enumerate(_list(job.get("entries"))):
            entry = _mapping(raw)
            request_id = str(entry.get("id", "") or "").strip()
            if not request_id:
                continue
            connection.execute(
                "DELETE FROM request_records WHERE request_id=? AND scope='queue'", (request_id,)
            )
            self._insert_request_record(
                connection, entry, scope="submission", job_id=job_id,
                submission_position=position,
            )
            try:
                step = max(0, int(entry.get("step", 0) or 0))
                step_count = max(0, int(entry.get("step_count", 3) or 0))
            except (TypeError, ValueError):
                step, step_count = 0, 3
            try:
                elapsed = float(entry["elapsed_seconds"]) if entry.get("elapsed_seconds") is not None else None
            except (TypeError, ValueError):
                elapsed = None
            connection.execute(
                "INSERT INTO submission_entries(job_id, request_id, position, state, message, step, step_count, "
                "request_id_remote, order_id, elapsed_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id, request_id, position, str(entry.get("state", "queued") or "queued"),
                    str(entry.get("message", "") or ""), step, step_count,
                    str(entry.get("request_id", "") or ""),
                    str(entry.get("order_id", "") or ""), elapsed,
                ),
            )

    def replace_request_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._write() as connection:
            connection.execute("DELETE FROM submission_jobs")
            seen: set[str] = set()
            for job in _list(history):
                item = _mapping(job)
                job_id = str(item.get("job_id", "") or "")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                self._save_history_job(connection, item)
            excess = connection.execute(
                "SELECT job_id FROM submission_jobs ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (MAX_HISTORY_JOBS,),
            ).fetchall()
            if excess:
                connection.executemany(
                    "DELETE FROM submission_jobs WHERE job_id=?",
                    [(row["job_id"],) for row in excess],
                )
        return self.load_request_history()

    def upsert_request_history(
        self,
        job: dict[str, Any],
        *,
        limit: int = MAX_HISTORY_JOBS,
    ) -> list[dict[str, Any]]:
        with self._write() as connection:
            self._save_history_job(connection, job)
            excess = connection.execute(
                "SELECT job_id FROM submission_jobs ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (max(1, limit),),
            ).fetchall()
            if excess:
                connection.executemany(
                    "DELETE FROM submission_jobs WHERE job_id=?",
                    [(row["job_id"],) for row in excess],
                )
        return self.load_request_history(limit=limit)

    @staticmethod
    def _read_legacy_json(path: Path) -> tuple[Any | None, str | None]:
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except FileNotFoundError:
            return None, None
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            return None, type(exc).__name__

    @staticmethod
    def _legacy_preferences(raw: Any) -> dict[str, Any]:
        source = _mapping(raw)
        statuses = source.get("request_statuses")
        if isinstance(statuses, dict):
            statuses = [*_list(statuses.get("user")), *_list(statuses.get("location"))]
        default_statuses = [
            "Deployed - Existing Stock", "Deployed - New Stock", "Loan", "Pending Return",
            "Unmanaged", "Donated", "Hold", "New Stock", "Loan Stock", "Used Stock",
            "Pending Decom", "Pending Disposal", "Pending Pickup", "Pending Repair",
            "Pending Rebuild", "Under Repair", "Vendor Collected", "Stolen/Lost",
        ]
        allowed = set(default_statuses)
        if not isinstance(statuses, list):
            statuses = default_statuses
        statuses = list(dict.fromkeys(str(value) for value in statuses if str(value) in allowed))
        if not any(value in default_statuses[:5] for value in statuses):
            statuses.insert(0, "Deployed - Existing Stock")
        if not any(value in default_statuses[5:] for value in statuses):
            statuses.append("New Stock")
        try:
            concurrency = min(200, max(1, int(source.get("concurrency", 10))))
        except (TypeError, ValueError):
            concurrency = 10
        import_defaults = {
            "username": "Username", "deployment_serial": "SN", "returned_device": "",
            "pending_return": "OLD Device SN", "enabled": "",
            "device_allocation": "Device(s) Allocation", "new_asset_status": "New Asset Status",
            "first_name": "First Name", "last_name": "Last Name",
        }
        import_columns = dict(import_defaults)
        for field_name in _IMPORT_COLUMN_NAMES:
            if field_name in _mapping(source.get("import_columns")):
                import_columns[field_name] = str(source["import_columns"].get(field_name) or "").strip()
        values: dict[str, Any] = {
            "concurrency": concurrency,
            "update_channel": "development" if source.get("update_channel") == "development" else "stable",
            "pc_toolkit_transport": str(source.get("pc_toolkit_transport", "browser"))
            if source.get("pc_toolkit_transport") in {"browser", "puppeteer", "api"} else "browser",
            "request_statuses": statuses,
            "import_columns": import_columns,
            "pc_toolkit_model_mappings": [],
        }
        for field in _BOOL_SETTINGS:
            values[field] = bool(source.get(field, field in {
                "validate_editor_serials", "validate_editor_users", "validate_bulk_serials",
                "validate_quick_import", "validate_workbook_import", "save_alm_import_drafts",
                "show_returned_serials_on_hand",
            }))
        for item in _list(source.get("pc_toolkit_model_mappings")):
            mapping = _mapping(item)
            model = " ".join(str(mapping.get("model", "") or "").split()).strip()
            user_status = str(mapping.get("user_status", "") or "")
            location_status = str(mapping.get("location_status", "") or "")
            if model and (user_status or location_status):
                values["pc_toolkit_model_mappings"].append({
                    "model": model, "user_status": user_status, "location_status": location_status,
                })
        return values

    def _migration_issue(
        self,
        connection: sqlite3.Connection,
        migration_name: str,
        source_file: str,
        message: str,
    ) -> None:
        connection.execute(
            "INSERT INTO migration_issues(migration_name, source_file, message, occurred_at) VALUES (?, ?, ?, ?)",
            (migration_name, source_file, message, _now()),
        )

    def migrate_legacy_results(self, source: Path | str) -> None:
        """Import legacy results/ files once, atomically, without deleting them."""
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            return
        migration_name = f"legacy-results-v1:{root}"
        with self._connection() as connection:
            done = connection.execute(
                "SELECT 1 FROM app_migrations WHERE name=?", (migration_name,)
            ).fetchone()
        if done:
            return

        migration_issues = 0

        def savepoint(connection: sqlite3.Connection, label: str, action: Callable[[], None]) -> None:
            nonlocal migration_issues
            token = "legacy_item"
            connection.execute(f"SAVEPOINT {token}")
            try:
                action()
                connection.execute(f"RELEASE SAVEPOINT {token}")
            except Exception as exc:
                connection.execute(f"ROLLBACK TO SAVEPOINT {token}")
                connection.execute(f"RELEASE SAVEPOINT {token}")
                migration_issues += 1
                self._migration_issue(
                    connection, migration_name, label,
                    f"Could not import this item ({type(exc).__name__}). The original file was kept.",
                )

        def json_file(path: Path) -> Any | None:
            nonlocal migration_issues
            value, error = self._read_legacy_json(path)
            if error:
                migration_issues += 1
                self._migration_issue(
                    connection, migration_name, path.name,
                    f"Could not read this JSON file ({error}). The original file was kept.",
                )
            return value

        with self._write() as connection:
            if connection.execute(
                "SELECT 1 FROM app_migrations WHERE name=?", (migration_name,)
            ).fetchone():
                return

            settings_path = root / "web-settings.json"
            settings = json_file(settings_path)
            if settings is not None:
                def import_settings() -> None:
                    self._save_preferences_connection(
                        connection, self._legacy_preferences(settings)
                    )
                savepoint(connection, settings_path.name, import_settings)

            imports_root = root / "web-alm-imports"
            if imports_root.is_dir():
                for payload_path in sorted(imports_root.glob("*.workbook")):
                    metadata_path = payload_path.with_suffix(".json")
                    metadata = _mapping(json_file(metadata_path)) if metadata_path.exists() else {}
                    import_id = str(metadata.get("import_id") or payload_path.stem)
                    filename = str(metadata.get("filename") or "ALM Workbook")
                    try:
                        payload = payload_path.read_bytes()
                    except OSError as exc:
                        migration_issues += 1
                        self._migration_issue(
                            connection, migration_name, payload_path.name,
                            f"Could not read workbook bytes ({type(exc).__name__}). The original file was kept.",
                        )
                        continue
                    columns = _mapping(metadata.get("columns")) or None
                    saved_at = str(metadata.get("saved_at") or _now())
                    savepoint(
                        connection, payload_path.name,
                        lambda import_id=import_id, filename=filename, payload=payload,
                        columns=columns, saved_at=saved_at: self._save_alm_workbook_connection(
                            connection, import_id, filename, payload, columns, saved_at=saved_at
                        ),
                    )

            pc_path = root / "pc-toolkit-cache.json"
            pc_cache = _mapping(json_file(pc_path)) if pc_path.exists() else {}
            for key, entry in _mapping(pc_cache.get("entries", pc_cache)).items():
                wrapper = _mapping(entry)
                result = _mapping(wrapper.get("result"))
                if result:
                    if not result.get("query"):
                        result = {**result, "query": key}
                    savepoint(
                        connection, pc_path.name,
                        lambda result=result, wrapper=wrapper: self._save_pc_lookup(
                            connection, result,
                            fetched_at=self._number(wrapper.get("fetched_at")) or time.time(),
                            cache_entry=True, source="legacy_cache",
                        ),
                    )
            for model in _list(pc_cache.get("models")):
                self._remember_pc_model(connection, str(model))

            draft_path = root / "web-alm-import-drafts.json"
            drafts = _list(json_file(draft_path)) if draft_path.exists() else []
            for draft in drafts:
                item = _mapping(draft)
                if not item:
                    continue
                savepoint(
                    connection, draft_path.name,
                    lambda item=item: self._save_alm_draft_connection(
                        connection, item, limit=MAX_DRAFTS
                    ),
                )

            histories: dict[str, dict[str, Any]] = {}
            for filename in ("request-history.json", "web-request-history.json"):
                path = root / filename
                if not path.exists():
                    continue
                raw_history = json_file(path)
                if isinstance(raw_history, dict):
                    raw_history = raw_history.get("runs", [])
                for item in _list(raw_history):
                    job = _mapping(item)
                    job_id = str(job.get("job_id", "") or "")
                    if job_id:
                        histories[job_id] = job
            for job in sorted(
                histories.values(),
                key=lambda item: str(item.get("created_at", "")), reverse=True,
            )[:MAX_HISTORY_JOBS]:
                savepoint(
                    connection, "request-history.json",
                    lambda job=job: self._save_history_job(connection, job),
                )

            queue_path = root / "web-request-queue.json"
            queue = _list(json_file(queue_path)) if queue_path.exists() else []
            if queue:
                savepoint(
                    connection, queue_path.name,
                    lambda: self._replace_request_queue(connection, queue),
                )

            verification_path = root / "web-verification-cache.json"
            cache = _mapping(json_file(verification_path)) if verification_path.exists() else {}
            for category in ("serials", "usernames"):
                entries = _mapping(cache.get(category))
                for key, raw_entry in entries.items():
                    entry = _mapping(raw_entry)
                    display = str(entry.get("value", key) or key).strip()
                    canonical = _normalise(key or display)
                    if not canonical:
                        continue
                    connection.execute(
                        "INSERT INTO verification_records(category, canonical_key, display_value, device_type, verified_at, last_used_at) "
                        "VALUES (?, ?, ?, ?, NULL, ?) ON CONFLICT(category, canonical_key) DO UPDATE SET "
                        "display_value=excluded.display_value, device_type=excluded.device_type, last_used_at=excluded.last_used_at",
                        (
                            category, canonical, display,
                            str(entry.get("device_type", "") or "") if category == "serials" else "",
                            _now(),
                        ),
                    )
                    connection.execute(
                        "DELETE FROM verification_aliases WHERE category=? AND canonical_key=?",
                        (category, canonical),
                    )
                    aliases = [display, *[str(value) for value in _list(entry.get("columns"))]]
                    normalized_aliases: list[tuple[str, str]] = []
                    seen_aliases: set[str] = set()
                    for alias in aliases:
                        alias_key = _normalise(alias)
                        if alias_key and alias_key not in seen_aliases:
                            normalized_aliases.append((alias_key, alias))
                            seen_aliases.add(alias_key)
                    normalized_aliases.insert(0, (canonical, display))
                    connection.executemany(
                        "INSERT INTO verification_aliases(category, alias_key, alias_display, canonical_key, ordinal) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(category, alias_key) DO UPDATE SET "
                        "alias_display=excluded.alias_display, canonical_key=excluded.canonical_key, ordinal=excluded.ordinal",
                        [
                            (category, alias_key, alias, canonical, position)
                            for position, (alias_key, alias) in enumerate(normalized_aliases)
                        ],
                    )

            ignore_path = root / "web-alm-backlog-ignored.json"
            ignored = json_file(ignore_path) if ignore_path.exists() else {}
            ignored_values = _mapping(_mapping(ignored).get("ignored") or ignored)
            for entry in ignored_values.values():
                item = _mapping(entry)
                serial = " ".join(str(item.get("serial", "") or "").split())
                username = " ".join(str(item.get("username", "") or "").split())
                serial_key, user_key = _normalise(serial), _normalise(username)
                if serial_key and user_key:
                    connection.execute(
                        "INSERT INTO alm_backlog_ignores(serial_normalized, username_normalized, serial, username, ignored_at) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(serial_normalized, username_normalized) DO NOTHING",
                        (serial_key, user_key, serial, username, _now()),
                    )

            connection.execute(
                "INSERT INTO app_migrations(name, completed_at, source_path, issue_count) VALUES (?, ?, ?, ?)",
                (migration_name, _now(), str(root), migration_issues),
            )

        # Keep old diagnostics available in the new per-instance support area.
        # This is intentionally a copy: results/ remains the rollback/recovery
        # source and is never emptied by migration.
        self._copy_legacy_diagnostics(root)

    def _copy_legacy_diagnostics(self, root: Path) -> None:
        import shutil

        diagnostic_names = ("alm-workbook-load-logs", "pc-toolkit-logs")
        for name in diagnostic_names:
            source = root / name
            destination = self.path.parent / name
            if not source.is_dir():
                continue
            try:
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                for item in source.rglob("*"):
                    if not item.is_file() or item.is_symlink():
                        continue
                    target = destination / item.relative_to(source)
                    if target.exists():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(item, target)
            except OSError:
                continue
