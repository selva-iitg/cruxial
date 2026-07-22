"""Realistic demo tools + stress prompts for any Cruxial integration.

Why this exists
---------------
Many production tool registries have schemas that are too simple to expose
hallucinations on top-tier models — a `{query: string}` schema is trivially
satisfiable. If you integrate Cruxial and see 0% interception in your own
stack, that's often *not* because the product is broken; it's because your
schemas don't have enough surface area for the LLM to slip.

This module is a portable, copy-pasteable set of realistic tools whose
schemas DO have enough surface — enums, formats, datetime, regex tags,
nested objects, additionalProperties=false — to surface interception on
any model tier. Use it to:

  1. Validate your Cruxial integration end-to-end with a visible rate
  2. Generate demo data for a sales call / video / cold DM
  3. Provide a baseline to compare your own tool schemas against

What's in here
--------------
    DEMO_TOOL_SCHEMAS       — {name: JSON Schema dict}
    DEMO_TOOL_EXECUTORS     — {name: async no-op stub that returns {"ok": True, ...}}
    DEMO_TOOL_DESCRIPTIONS  — {name: human-readable description for the LLM}
    DEMO_OPENAI_TOOLS       — OpenAI-format tools list (drop into chat.completions.create)
    DEMO_ANTHROPIC_TOOLS    — Anthropic-format tools list (drop into messages.create)
    DEMO_PROMPTS            — list[{text, expected_tool, designed_to_trip}]

Usage in a host app:
    from cruxial.demo import (
        DEMO_TOOL_SCHEMAS, DEMO_TOOL_EXECUTORS, DEMO_TOOL_DESCRIPTIONS, DEMO_PROMPTS,
    )
    # Register each in your app's tool registry (so the LLM sees them).
    # Extend your cruxial guard's schemas dict.
    # Send DEMO_PROMPTS through your chat API.
    # Run `cruxial stats --since 10m` to see the interception breakdown.

Naming
------
Every tool is prefixed `demo_` so they're obvious in telemetry and easy to
remove. No side effects — the executors just echo back the args.
"""

from __future__ import annotations

from typing import Any


# ─── 1. demo_create_incident — SRE / IR ──────────────────────────────


_INCIDENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 8, "maxLength": 80},
        "summary": {"type": "string", "minLength": 20, "maxLength": 500},
        "severity": {
            "type": "string",
            "enum": ["sev0", "sev1", "sev2", "sev3", "sev4"],
        },
        "impacted_service": {
            "type": "string",
            "enum": [
                "payments-api",
                "auth-service",
                "notification-service",
                "search-index",
                "billing-jobs",
                "analytics-pipeline",
                "media-cdn",
                "feature-flags",
            ],
        },
        "detected_at": {"type": "string", "format": "date-time"},
        "mttr_target_minutes": {"type": "integer", "minimum": 5, "maximum": 240},
        "ic_email": {"type": "string", "format": "email"},
        "tags": {
            "type": "array",
            "items": {
                "type": "string",
                "pattern": "^[a-z0-9][a-z0-9-]{1,30}$",
            },
            "minItems": 1,
            "maxItems": 5,
        },
        "rollback_attempted": {"type": "boolean"},
    },
    "required": [
        "title", "summary", "severity", "impacted_service",
        "detected_at", "ic_email", "tags",
    ],
}


# ─── 2. demo_schedule_meeting — calendar ─────────────────────────────


_MEETING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 3, "maxLength": 100},
        "attendees": {
            "type": "array",
            "items": {"type": "string", "format": "email"},
            "minItems": 1,
            "maxItems": 15,
        },
        "start_time": {"type": "string", "format": "date-time"},
        "duration_minutes": {
            "type": "integer",
            "minimum": 15,
            "maximum": 480,
            "multipleOf": 15,
        },
        "meeting_type": {
            "type": "string",
            "enum": [
                "standup",
                "review",
                "planning",
                "one_on_one",
                "all_hands",
                "training",
                "customer_call",
            ],
        },
        "timezone": {
            "type": "string",
            "enum": [
                "UTC",
                "America/New_York",
                "America/Los_Angeles",
                "Europe/London",
                "Europe/Berlin",
                "Asia/Singapore",
                "Asia/Kolkata",
                "Australia/Sydney",
            ],
        },
        "location": {"type": "string", "maxLength": 200},
        "agenda_items": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 10,
        },
    },
    "required": [
        "title", "attendees", "start_time", "duration_minutes",
        "meeting_type", "timezone",
    ],
}


# ─── 3. demo_send_invoice — billing ──────────────────────────────────


_INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "customer_id": {
            "type": "string",
            "pattern": "^cus_[a-z0-9]{8,16}$",
        },
        "amount_cents": {
            "type": "integer",
            "minimum": 100,
            "maximum": 100_000_000,
        },
        "currency": {
            "type": "string",
            "enum": ["USD", "EUR", "GBP", "INR", "JPY", "AUD", "CAD"],
        },
        "line_items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "description": {"type": "string", "minLength": 1, "maxLength": 200},
                    "unit_price_cents": {"type": "integer", "minimum": 1},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["description", "unit_price_cents", "quantity"],
            },
        },
        "due_date": {"type": "string", "format": "date"},
        "payment_terms": {
            "type": "string",
            "enum": ["immediate", "on_receipt", "net_15", "net_30", "net_60"],
        },
        "notes": {"type": "string", "maxLength": 1000},
    },
    "required": [
        "customer_id", "amount_cents", "currency",
        "line_items", "due_date", "payment_terms",
    ],
}


# ─── 4. demo_create_pull_request — devops ────────────────────────────


_PR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "repo": {
            "type": "string",
            "pattern": "^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$",
        },
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {"type": "string", "maxLength": 10000},
        "source_branch": {
            "type": "string",
            "pattern": "^[a-zA-Z0-9._/-]{1,100}$",
        },
        "target_branch": {
            "type": "string",
            "enum": ["main", "develop", "staging", "production", "release"],
        },
        "reviewers": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 40},
            "minItems": 1,
            "maxItems": 5,
        },
        "labels": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "bug", "feature", "chore", "security",
                    "breaking", "docs", "refactor", "test",
                ],
            },
            "maxItems": 5,
        },
        "draft": {"type": "boolean"},
        "assignees": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
    },
    "required": ["repo", "title", "source_branch", "target_branch", "reviewers"],
}


# ─── 5. demo_search_inventory — ecommerce ────────────────────────────


_INVENTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
        "filters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "electronics", "books", "clothing", "food",
                        "beauty", "sports", "home", "toys",
                    ],
                },
                "price_min": {"type": "number", "minimum": 0},
                "price_max": {"type": "number", "minimum": 0, "maximum": 1_000_000},
                "in_stock_only": {"type": "boolean"},
                "color": {"type": "string", "maxLength": 50},
            },
        },
        "sort_by": {
            "type": "string",
            "enum": ["relevance", "price_low_high", "price_high_low", "newest", "popular"],
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "required": ["query"],
}


# ─── 6. demo_send_slack_message — messaging ──────────────────────────


_SLACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "channel": {"type": "string", "pattern": "^#[a-z0-9_-]{1,80}$"},
        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
        "thread_ts": {"type": "string", "pattern": "^[0-9]{10}\\.[0-9]{6}$"},
        "mentions": {
            "type": "array",
            "items": {"type": "string", "pattern": "^@[a-z0-9._-]{1,40}$"},
            "maxItems": 20,
        },
        "broadcast_to_channel": {"type": "boolean"},
        "icon_emoji": {"type": "string", "pattern": "^:[a-z0-9_+-]+:$"},
    },
    "required": ["channel", "text"],
}


# ─── 7. demo_run_sql_query — analytics ───────────────────────────────


_SQL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 10000},
        "dialect": {
            "type": "string",
            "enum": ["postgres", "mysql", "snowflake", "bigquery", "redshift", "duckdb"],
        },
        "database": {"type": "string", "minLength": 1, "maxLength": 80},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
        "max_rows": {"type": "integer", "minimum": 1, "maximum": 100000},
        "read_only": {"type": "boolean"},
        "tags": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[a-z0-9-]+$"},
            "maxItems": 5,
        },
    },
    "required": ["query", "dialect", "database"],
}


# ─── 8. demo_create_calendar_event — calendar w/ recurrence ──────────


_CAL_EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "start_at": {"type": "string", "format": "date-time"},
        "end_at": {"type": "string", "format": "date-time"},
        "calendar_id": {"type": "string", "format": "email"},
        "location": {"type": "string", "maxLength": 500},
        "visibility": {
            "type": "string",
            "enum": ["default", "public", "private", "confidential"],
        },
        "recurrence_rule": {
            "type": "string",
            "pattern": "^RRULE:FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;.+)?$",
        },
        "reminders_minutes_before": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 40320},
            "maxItems": 5,
        },
    },
    "required": ["title", "start_at", "end_at", "calendar_id"],
}


# ─── 9. demo_deploy_application — devops ─────────────────────────────


_DEPLOY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "service_name": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9-]{1,40}$",
        },
        "version": {
            "type": "string",
            "pattern": "^v?\\d+\\.\\d+\\.\\d+(-[a-zA-Z0-9.]+)?$",
        },
        "environment": {
            "type": "string",
            "enum": ["dev", "staging", "production", "canary"],
        },
        "region": {
            "type": "string",
            "enum": [
                "us-east-1", "us-west-2", "eu-west-1",
                "ap-southeast-1", "ap-south-1",
            ],
        },
        "replicas": {"type": "integer", "minimum": 1, "maximum": 100},
        "strategy": {
            "type": "string",
            "enum": ["rolling", "blue_green", "canary", "recreate"],
        },
        "auto_rollback": {"type": "boolean"},
        "wait_for_health": {"type": "boolean"},
    },
    "required": ["service_name", "version", "environment", "region"],
}


# ─── 10. demo_create_jira_ticket — project management ────────────────


_JIRA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_key": {"type": "string", "pattern": "^[A-Z][A-Z0-9]{1,9}$"},
        "summary": {"type": "string", "minLength": 1, "maxLength": 255},
        "description": {"type": "string", "maxLength": 32000},
        "issue_type": {
            "type": "string",
            "enum": ["Bug", "Task", "Story", "Epic", "Sub-task"],
        },
        "priority": {
            "type": "string",
            "enum": ["Highest", "High", "Medium", "Low", "Lowest"],
        },
        "story_points": {
            "type": "number",
            "enum": [0, 1, 2, 3, 5, 8, 13, 21],
        },
        "assignee_email": {"type": "string", "format": "email"},
        "labels": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[a-z0-9-]+$"},
            "maxItems": 10,
        },
        "due_date": {"type": "string", "format": "date"},
    },
    "required": ["project_key", "summary", "issue_type"],
}


# ─── 11. demo_upload_file — storage ──────────────────────────────────


_UPLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "filename": {
            "type": "string",
            "pattern": "^[a-zA-Z0-9._-]{1,255}$",
        },
        "bucket": {
            "type": "string",
            "enum": ["user-uploads", "media-assets", "exports", "backups"],
        },
        "content_type": {
            "type": "string",
            "enum": [
                "image/png", "image/jpeg", "image/webp", "image/gif",
                "application/pdf", "application/json", "text/plain",
                "text/csv", "video/mp4",
            ],
        },
        "size_bytes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1_073_741_824,
        },
        "expiration_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 365,
        },
        "public_read": {"type": "boolean"},
    },
    "required": ["filename", "bucket", "content_type", "size_bytes"],
}


# ─── 12. demo_send_email_campaign — marketing ────────────────────────


_CAMPAIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 200},
        "subject": {"type": "string", "minLength": 1, "maxLength": 200},
        "template_id": {
            "type": "string",
            "pattern": "^tmpl_[a-z0-9]{8,16}$",
        },
        "audience_segment": {
            "type": "string",
            "enum": [
                "all_users", "active_30d", "trial",
                "paid", "churned", "new_signups",
            ],
        },
        "scheduled_at": {"type": "string", "format": "date-time"},
        "from_email": {"type": "string", "format": "email"},
        "reply_to": {"type": "string", "format": "email"},
        "ab_test_variant": {
            "type": "string",
            "enum": ["A", "B", "control"],
        },
    },
    "required": [
        "name", "subject", "template_id",
        "audience_segment", "from_email",
    ],
}


# ─── 13. demo_create_database_backup — ops ───────────────────────────


_BACKUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "database_id": {"type": "string", "pattern": "^db_[a-z0-9]{8,16}$"},
        "backup_type": {
            "type": "string",
            "enum": ["full", "incremental", "differential"],
        },
        "retention_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3650,
        },
        "storage_region": {
            "type": "string",
            "enum": [
                "us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1",
            ],
        },
        "encryption_key_id": {
            "type": "string",
            "pattern": "^key_[a-zA-Z0-9]{16,}$",
        },
        "compress": {"type": "boolean"},
        "notify_on_completion": {
            "type": "array",
            "items": {"type": "string", "format": "email"},
            "maxItems": 5,
        },
    },
    "required": [
        "database_id", "backup_type", "retention_days", "storage_region",
    ],
}


# ─── 14. demo_book_flight — travel ───────────────────────────────────


_FLIGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "origin_airport": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "destination_airport": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "departure_date": {"type": "string", "format": "date"},
        "return_date": {"type": "string", "format": "date"},
        "passengers": {"type": "integer", "minimum": 1, "maximum": 9},
        "cabin_class": {
            "type": "string",
            "enum": ["economy", "premium_economy", "business", "first"],
        },
        "max_stops": {"type": "integer", "minimum": 0, "maximum": 3},
        "baggage_count": {"type": "integer", "minimum": 0, "maximum": 4},
        "loyalty_program": {
            "type": "string",
            "enum": [
                "united_mileage", "delta_skymiles", "aa_advantage",
                "british_airways_executive", "none",
            ],
        },
    },
    "required": [
        "origin_airport", "destination_airport",
        "departure_date", "passengers",
    ],
}


# ─── 15. demo_register_user — auth ───────────────────────────────────


_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "email": {"type": "string", "format": "email"},
        "phone": {"type": "string", "pattern": "^\\+[1-9]\\d{6,14}$"},
        "password": {"type": "string", "minLength": 12, "maxLength": 128},
        "full_name": {"type": "string", "minLength": 1, "maxLength": 100},
        "role": {
            "type": "string",
            "enum": ["admin", "member", "viewer", "billing"],
        },
        "tenant_id": {
            "type": "string",
            "pattern": "^tenant_[a-z0-9]{8,}$",
        },
        "preferred_language": {
            "type": "string",
            "enum": ["en", "es", "fr", "de", "ja", "zh", "hi"],
        },
        "marketing_opt_in": {"type": "boolean"},
        "invited_by_email": {"type": "string", "format": "email"},
    },
    "required": [
        "email", "password", "full_name", "role", "tenant_id",
    ],
}


# ─── public registry ─────────────────────────────────────────────────


DEMO_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "demo_create_incident": _INCIDENT_SCHEMA,
    "demo_schedule_meeting": _MEETING_SCHEMA,
    "demo_send_invoice": _INVOICE_SCHEMA,
    "demo_create_pull_request": _PR_SCHEMA,
    "demo_search_inventory": _INVENTORY_SCHEMA,
    "demo_send_slack_message": _SLACK_SCHEMA,
    "demo_run_sql_query": _SQL_SCHEMA,
    "demo_create_calendar_event": _CAL_EVENT_SCHEMA,
    "demo_deploy_application": _DEPLOY_SCHEMA,
    "demo_create_jira_ticket": _JIRA_SCHEMA,
    "demo_upload_file": _UPLOAD_SCHEMA,
    "demo_send_email_campaign": _CAMPAIGN_SCHEMA,
    "demo_create_database_backup": _BACKUP_SCHEMA,
    "demo_book_flight": _FLIGHT_SCHEMA,
    "demo_register_user": _USER_SCHEMA,
}


DEMO_TOOL_DESCRIPTIONS: dict[str, str] = {
    "demo_create_incident": (
        "Open a production incident in the IR/SRE system. Severity is one of "
        "sev0–sev4 (sev0 = total outage). All tags must be lowercase kebab-case. "
        "mttr_target_minutes is bounded by 5–240."
    ),
    "demo_schedule_meeting": (
        "Schedule a calendar meeting. duration_minutes must be a multiple of 15 "
        "(15–480). timezone must be a full IANA name like 'America/New_York'. "
        "meeting_type is from a fixed set."
    ),
    "demo_send_invoice": (
        "Issue an invoice to a customer. customer_id format is cus_<8-16 lowercase "
        "alphanumerics>. amount_cents is in CENTS (not dollars). Each line_item "
        "needs description, unit_price_cents, quantity."
    ),
    "demo_create_pull_request": (
        "Open a pull request. repo format is org/name. target_branch is one of "
        "main/develop/staging/production/release. labels must come from a fixed enum."
    ),
    "demo_search_inventory": (
        "Search the product inventory. category is from a fixed enum. price filters "
        "are in dollars (numbers, not strings). limit is 1–100."
    ),
    "demo_send_slack_message": (
        "Post a message to Slack. channel must start with '#' and be lowercase "
        "kebab-case. Mentions must start with '@'. icon_emoji is :name: format."
    ),
    "demo_run_sql_query": (
        "Run an analytical SQL query. dialect is one of postgres/mysql/snowflake/"
        "bigquery/redshift/duckdb (lowercase). timeout_seconds is bounded 1–600."
    ),
    "demo_create_calendar_event": (
        "Create a calendar event. calendar_id is an email address. visibility is "
        "default/public/private/confidential. recurrence_rule is an RFC 5545 RRULE "
        "string starting with RRULE:FREQ=..."
    ),
    "demo_deploy_application": (
        "Deploy a service to an environment. service_name is lowercase kebab. "
        "version follows semver (e.g., 1.2.3 or v1.2.3-beta). environment is "
        "dev/staging/production/canary. region is a fixed AWS region enum."
    ),
    "demo_create_jira_ticket": (
        "Create a Jira issue. project_key is uppercase alphanumeric like 'PROD' or "
        "'ENG2'. issue_type is Bug/Task/Story/Epic/Sub-task (exact case). "
        "story_points must be a Fibonacci number from {0,1,2,3,5,8,13,21}."
    ),
    "demo_upload_file": (
        "Upload a file to object storage. bucket is one of user-uploads/"
        "media-assets/exports/backups. content_type must be an exact MIME "
        "type (e.g., image/png, not 'png'). size_bytes is in bytes (max 1GB)."
    ),
    "demo_send_email_campaign": (
        "Send a marketing email campaign. template_id format is tmpl_<8-16 lowercase "
        "alphanumerics>. audience_segment is from a fixed enum (e.g., 'paid', not "
        "'paying-customers'). ab_test_variant is A/B/control."
    ),
    "demo_create_database_backup": (
        "Create a database backup. database_id format is db_<8-16 lowercase "
        "alphanumerics>. backup_type is full/incremental/differential. "
        "retention_days bounded 1–3650."
    ),
    "demo_book_flight": (
        "Book a flight. origin_airport and destination_airport are IATA 3-letter "
        "uppercase codes (e.g., 'BLR', not 'Bangalore'). cabin_class is one of "
        "economy/premium_economy/business/first. passengers is 1–9."
    ),
    "demo_register_user": (
        "Register a new user account. phone is E.164 format starting with '+'. "
        "password must be at least 12 characters. role is admin/member/viewer/billing. "
        "tenant_id format is tenant_<8+ lowercase alphanumerics>. "
        "preferred_language is a 2-letter ISO code (e.g., 'en', not 'English')."
    ),
}


# ─── no-op async executors ──────────────────────────────────────────


async def _noop(name: str, **kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "demo": True, "tool": name, "args_echoed": kwargs}


# Use a lambda factory so each executor knows its own name.
def _make_executor(name: str):
    async def _exec(**kwargs):
        return await _noop(name, **kwargs)
    _exec.__name__ = f"demo_{name}_executor"
    return _exec


DEMO_TOOL_EXECUTORS: dict[str, Any] = {
    name: _make_executor(name) for name in DEMO_TOOL_SCHEMAS
}


# Sync versions for codebases that don't use async dispatch.
def _make_sync_executor(name: str):
    def _exec(**kwargs):
        return {"ok": True, "demo": True, "tool": name, "args_echoed": kwargs}
    _exec.__name__ = f"demo_{name}_sync_executor"
    return _exec


DEMO_TOOL_EXECUTORS_SYNC: dict[str, Any] = {
    name: _make_sync_executor(name) for name in DEMO_TOOL_SCHEMAS
}


# ─── native-format tools (drop into your LLM client) ─────────────────


DEMO_OPENAI_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": DEMO_TOOL_DESCRIPTIONS[name],
            "parameters": schema,
        },
    }
    for name, schema in DEMO_TOOL_SCHEMAS.items()
]


DEMO_ANTHROPIC_TOOLS: list[dict[str, Any]] = [
    {
        "name": name,
        "description": DEMO_TOOL_DESCRIPTIONS[name],
        "input_schema": schema,
    }
    for name, schema in DEMO_TOOL_SCHEMAS.items()
]


# ─── stress prompts ──────────────────────────────────────────────────
# Designed to mix clean baselines, ambiguous prompts, and adversarial cases.
# `designed_to_trip` is documentation only — the actual outcome depends on
# the model. Use it to compare expected vs actual failures.


DEMO_PROMPTS: list[dict[str, str]] = [
    # ── demo_create_incident (4) ────────────────────────────────────
    {
        "text": (
            "URGENT: payments-api has been totally down for 15 minutes. "
            "All checkout flows failing. Open a sev1 incident, IC is "
            "oncall@cruxial.ai, target MTTR 30 min. Tags: payments-outage, "
            "customer-facing."
        ),
        "expected_tool": "demo_create_incident",
        "designed_to_trip": "clean baseline — should pass",
    },
    {
        "text": (
            "Critical issue, customer-facing, fix needed in two weeks. "
            "Title 'Email digests not sending'. Service: notification-service. "
            "IC: notify@cruxial.ai. Tags: email-broken."
        ),
        "expected_tool": "demo_create_incident",
        "designed_to_trip": "constraint_violation — 'two weeks' → 20160 min, max is 240",
    },
    {
        "text": (
            "File this as sev5: signup flow throwing 500s for new users. "
            "Service: auth-service. IC: signup@cruxial.ai. MTTR 60 min. "
            "Detected just now. Tags: 'Signup Errors', '500s'."
        ),
        "expected_tool": "demo_create_incident",
        "designed_to_trip": "enum_violation on severity (sev5 not in enum), pattern_violation on tags (uppercase + space)",
    },
    {
        "text": (
            "Open an incident: media CDN totally broken but low impact "
            "since only beta users. IC 'Arnav from media platform', detected "
            "this morning. Service: media-cdn. MTTR 2h. Tags: cdn-degraded, beta-only."
        ),
        "expected_tool": "demo_create_incident",
        "designed_to_trip": "format_violation on ic_email (model may emit a name not an email)",
    },

    # ── demo_schedule_meeting (4) ───────────────────────────────────
    {
        "text": (
            "Schedule a 45-minute planning meeting with riya@cruxial.ai and "
            "mira@cruxial.ai next Tuesday at 10am IST. Title: 'Q3 product planning'."
        ),
        "expected_tool": "demo_schedule_meeting",
        "designed_to_trip": "constraint_violation — 45 not a multiple of 15; enum_violation on timezone if model says 'IST'",
    },
    {
        "text": (
            "Set up a quick standup with the eng team — alex, jordan, sam, "
            "priya — for tomorrow 9am PST, 25 minutes."
        ),
        "expected_tool": "demo_schedule_meeting",
        "designed_to_trip": "format_violation on attendees (names not emails), enum on timezone (PST not IANA), constraint on duration (25 not multiple of 15)",
    },
    {
        "text": (
            "Book a 2-hour all-hands for Friday at 4pm UK time. Just me and "
            "the leadership: ceo@cruxial.ai, cto@cruxial.ai, cfo@cruxial.ai."
        ),
        "expected_tool": "demo_schedule_meeting",
        "designed_to_trip": "enum on timezone if model says 'UK time' or 'BST' instead of 'Europe/London'",
    },
    {
        "text": (
            "Schedule a customer demo with the Acme team next Wednesday 2pm Eastern, "
            "30 minutes. Attendees: acme.alice@acme.io, acme.bob@acme.io."
        ),
        "expected_tool": "demo_schedule_meeting",
        "designed_to_trip": "enum on timezone if model picks 'EST' or 'EDT'; enum on meeting_type if model picks 'demo' (not in list)",
    },

    # ── demo_send_invoice (4) ───────────────────────────────────────
    {
        "text": (
            "Send invoice to customer cus_acme2031bx for $1,250.50 USD. "
            "Line items: 'Pro plan – June' $999, 'Overage charges' $251.50. "
            "Due in 30 days."
        ),
        "expected_tool": "demo_send_invoice",
        "designed_to_trip": "constraint on amount_cents (model often sends dollars not cents); enum on payment_terms if model says 'net30'",
    },
    {
        "text": (
            "Bill acme-corp 2500 dollars for the consulting hours last month. "
            "Net 60 terms. Line items: 'Consulting – Jan 2026' 25 hours at $100/hour."
        ),
        "expected_tool": "demo_send_invoice",
        "designed_to_trip": "pattern on customer_id (model says 'acme-corp' not cus_<id>); nested object on line_items",
    },
    {
        "text": (
            "Generate an invoice for customer cus_indianstartup99 for ₹50,000. "
            "Single line item: 'Annual subscription'. Due immediately."
        ),
        "expected_tool": "demo_send_invoice",
        "designed_to_trip": "enum on currency (model may emit 'INR' correctly or 'Rupees'); nested object missing required fields on line_items",
    },
    {
        "text": (
            "Invoice cus_yenpayer07 for ¥250,000 with payment due on receipt. "
            "Line items: 'Tokyo office setup – consulting', 1 unit."
        ),
        "expected_tool": "demo_send_invoice",
        "designed_to_trip": "nested object validation — model may omit unit_price_cents",
    },

    # ── demo_create_pull_request (4) ────────────────────────────────
    {
        "text": (
            "Open a PR in selva-iitg/cruxial-sdk from branch feat/runtime-register "
            "into main. Title: 'Add runtime tool registration', reviewers: "
            "selva and arnav. Labels: feature, refactor. Not a draft."
        ),
        "expected_tool": "demo_create_pull_request",
        "designed_to_trip": "clean if model handles reviewers as bare names; pattern on repo if model adds github.com prefix",
    },
    {
        "text": (
            "Create a PR for github.com/myorg/myrepo from develop into master. "
            "Title 'Refactor auth flow', reviewers @alice and @bob, "
            "labels: bug-fix, high-priority, security."
        ),
        "expected_tool": "demo_create_pull_request",
        "designed_to_trip": "pattern on repo (github.com prefix); enum on target_branch (master not in list); enum on labels (bug-fix and high-priority not in enum)",
    },
    {
        "text": (
            "Open a draft PR in acme/internal-tools from selva/new feature branch "
            "into production. Title 'Stub for review'. Reviewer: review-team. "
            "Label: chore."
        ),
        "expected_tool": "demo_create_pull_request",
        "designed_to_trip": "pattern on source_branch (space in branch name); reviewers minimum of 1 should pass",
    },
    {
        "text": (
            "PR: repo cruxial/landing, source: hotfix/typo-in-readme, target: "
            "staging. Title 'Fix typo'. Reviewer: docs-lead. Labels: docs, test."
        ),
        "expected_tool": "demo_create_pull_request",
        "designed_to_trip": "clean baseline — should mostly pass; tests label edge case",
    },

    # ── demo_search_inventory (4) ───────────────────────────────────
    {
        "text": (
            "Search for blue running shoes under $80 in stock. Limit 20 results, "
            "sort by price ascending."
        ),
        "expected_tool": "demo_search_inventory",
        "designed_to_trip": "enum on sort_by (model may emit 'price_asc' not 'price_low_high'); enum on category if model invents 'shoes'",
    },
    {
        "text": (
            "Find electronics priced between $100 and $500, newest first, "
            "show me 50 items. Only in-stock."
        ),
        "expected_tool": "demo_search_inventory",
        "designed_to_trip": "type_mismatch on price filters if model sends '$100' as string; nested object validation",
    },
    {
        "text": (
            "Search books about LLMs, most popular, top 200 results."
        ),
        "expected_tool": "demo_search_inventory",
        "designed_to_trip": "constraint on limit (200 > max 100)",
    },
    {
        "text": (
            "Browse the home & garden category for outdoor furniture, "
            "any price, 10 results, sort by relevance."
        ),
        "expected_tool": "demo_search_inventory",
        "designed_to_trip": "enum on category — 'home & garden' / 'outdoor' not in the fixed enum (only 'home')",
    },

    # ── demo_send_slack_message (5) ─────────────────────────────────
    {
        "text": "Post to #engineering: 'Deploy starting now, ETA 15 minutes'. Mention @riya.",
        "expected_tool": "demo_send_slack_message",
        "designed_to_trip": "clean baseline — should pass with correct # and @ prefixes",
    },
    {
        "text": "Send a message to the engineering channel saying deploy is starting, ETA 15 min. Mention Riya.",
        "expected_tool": "demo_send_slack_message",
        "designed_to_trip": "pattern on channel ('engineering' without #); pattern on mentions (bare 'Riya')",
    },
    {
        "text": "Post a heads-up in #ENG-Team: ship it 🚀. Use icon :rocket:.",
        "expected_tool": "demo_send_slack_message",
        "designed_to_trip": "pattern on channel (uppercase chars in #ENG-Team)",
    },
    {
        "text": "Reply in the prod-outage thread (ts 1717023600.123456) on #incidents: 'Mitigation in progress'.",
        "expected_tool": "demo_send_slack_message",
        "designed_to_trip": "clean baseline — exercises thread_ts pattern correctly",
    },
    {
        "text": "Broadcast in #all-hands: quarter results are out. Tag everyone: @alice, @bob, @carol, @dave, @eve.",
        "expected_tool": "demo_send_slack_message",
        "designed_to_trip": "clean if model handles mentions correctly; boolean coercion on broadcast_to_channel",
    },

    # ── demo_run_sql_query (5) ──────────────────────────────────────
    {
        "text": "Run this on the analytics postgres db: SELECT COUNT(*) FROM events WHERE ts > NOW() - INTERVAL '7 days'. Timeout 30 seconds.",
        "expected_tool": "demo_run_sql_query",
        "designed_to_trip": "clean baseline — should pass",
    },
    {
        "text": "Query Snowflake (warehouse: analytics_wh): SELECT user_id, COUNT(*) FROM sessions GROUP BY 1 LIMIT 1000. Allow 1 hour to run.",
        "expected_tool": "demo_run_sql_query",
        "designed_to_trip": "enum on dialect ('Snowflake' capitalized vs 'snowflake'); constraint on timeout_seconds (3600 > max 600)",
    },
    {
        "text": "Run a quick SELECT * FROM users LIMIT 10 in BigQuery, database is prod_analytics.",
        "expected_tool": "demo_run_sql_query",
        "designed_to_trip": "enum on dialect ('BigQuery' capitalized vs 'bigquery')",
    },
    {
        "text": "Query redshift on the prod_warehouse db, max 250000 rows: SELECT * FROM orders WHERE status = 'pending'.",
        "expected_tool": "demo_run_sql_query",
        "designed_to_trip": "constraint on max_rows (250000 > max 100000)",
    },
    {
        "text": "Run an ad-hoc query on our PostgreSQL DB called metrics_prod: SELECT MAX(latency_ms) FROM api_calls WHERE date = CURRENT_DATE.",
        "expected_tool": "demo_run_sql_query",
        "designed_to_trip": "enum on dialect ('PostgreSQL' vs 'postgres')",
    },

    # ── demo_create_calendar_event (5) ──────────────────────────────
    {
        "text": "Create a calendar event titled 'Sprint kickoff' on selva@cruxial.ai's calendar, 10am-11am UTC tomorrow.",
        "expected_tool": "demo_create_calendar_event",
        "designed_to_trip": "clean baseline — exercises date-time format",
    },
    {
        "text": "Add a recurring weekly standup on the team@cruxial.ai calendar, every Monday 9am Pacific, 30 minutes, starting next week.",
        "expected_tool": "demo_create_calendar_event",
        "designed_to_trip": "pattern on recurrence_rule (model may emit 'WEEKLY' not 'RRULE:FREQ=WEEKLY...')",
    },
    {
        "text": "Block off 'Deep work' on my calendar (selva@cruxial.ai) from 2-4pm tomorrow IST. Make it private.",
        "expected_tool": "demo_create_calendar_event",
        "designed_to_trip": "may pass — exercises visibility enum",
    },
    {
        "text": "Schedule a recurring monthly review on the leadership@cruxial.ai calendar — first of every month at 3pm, 90 minutes, with 1 day reminder.",
        "expected_tool": "demo_create_calendar_event",
        "designed_to_trip": "constraint on reminders_minutes_before (1 day = 1440 min, ok; but if model emits 2 days = 2880, ok; week = 10080, ok; month = 43200 > max 40320)",
    },
    {
        "text": "Create a half-day offsite next Friday on offsites@cruxial.ai calendar, mark as confidential.",
        "expected_tool": "demo_create_calendar_event",
        "designed_to_trip": "should pass with valid enum",
    },

    # ── demo_deploy_application (5) ─────────────────────────────────
    {
        "text": "Deploy api-gateway version 2.4.1 to production in us-east-1, blue-green strategy, 8 replicas, auto-rollback on.",
        "expected_tool": "demo_deploy_application",
        "designed_to_trip": "clean baseline — exercises version semver",
    },
    {
        "text": "Push the new payments service (v1.2) to staging in eu-west, rolling deploy, 4 replicas.",
        "expected_tool": "demo_deploy_application",
        "designed_to_trip": "pattern on version (1.2 missing patch); enum on region (eu-west not eu-west-1)",
    },
    {
        "text": "Deploy AuthService 3.0.0 to prod in AWS US-East-1 with canary strategy.",
        "expected_tool": "demo_deploy_application",
        "designed_to_trip": "pattern on service_name (AuthService has uppercase); enum on region (AWS US-East-1 format); enum on environment (prod vs production)",
    },
    {
        "text": "Roll out search-indexer v0.9.7-rc.1 to canary environment in ap-south-1 with 2 replicas.",
        "expected_tool": "demo_deploy_application",
        "designed_to_trip": "clean baseline — exercises pre-release version + canary env",
    },
    {
        "text": "Deploy notification-service version 1.0 to all environments at once, recreate strategy, 200 replicas.",
        "expected_tool": "demo_deploy_application",
        "designed_to_trip": "constraint on replicas (200 > max 100); enum on environment ('all' not in list)",
    },

    # ── demo_create_jira_ticket (5) ─────────────────────────────────
    {
        "text": "Create a Bug ticket in project ENG with summary 'Login fails on Safari 17'. Priority high, 3 story points, assign to qa@cruxial.ai.",
        "expected_tool": "demo_create_jira_ticket",
        "designed_to_trip": "enum on priority ('high' vs 'High' - case sensitive)",
    },
    {
        "text": "File a story in the payments project: 'Add Apple Pay support'. Medium priority, 8 points, due 2026-08-01.",
        "expected_tool": "demo_create_jira_ticket",
        "designed_to_trip": "pattern on project_key ('payments' lowercase vs uppercase required); enum on issue_type ('story' vs 'Story')",
    },
    {
        "text": "Create a sub-task under PROJ-1234 with summary 'Write migration script', task type, priority lowest, 1 point.",
        "expected_tool": "demo_create_jira_ticket",
        "designed_to_trip": "enum on issue_type — 'sub-task' (model lowercase) vs 'Sub-task' (mixed case in enum)",
    },
    {
        "text": "Open an epic in the PLAT project: 'Q3 platform reliability'. Priority highest, 13 points, labels: reliability, platform-team, q3-okr.",
        "expected_tool": "demo_create_jira_ticket",
        "designed_to_trip": "pattern on labels (q3-okr might be ok; but 'platform-team' has hyphen which is ok)",
    },
    {
        "text": "Create a task in INFRA project: 'Investigate flaky CI', priority medium, 4 story points.",
        "expected_tool": "demo_create_jira_ticket",
        "designed_to_trip": "enum on story_points (4 not in Fibonacci enum [0,1,2,3,5,8,13,21])",
    },

    # ── demo_upload_file (5) ────────────────────────────────────────
    {
        "text": "Upload report.pdf (2.5MB, PDF) to the exports bucket. Keep for 30 days, private.",
        "expected_tool": "demo_upload_file",
        "designed_to_trip": "clean baseline — exercises content_type + bucket enums",
    },
    {
        "text": "Save profile_pic.jpg to user-uploads. It's a JPEG, about 800KB, make it publicly readable.",
        "expected_tool": "demo_upload_file",
        "designed_to_trip": "enum on content_type (JPEG vs image/jpeg); size in 'KB' may convert wrong",
    },
    {
        "text": "Upload the marketing video promo_q3.mp4 (1.2GB) to media-assets, keep for a year.",
        "expected_tool": "demo_upload_file",
        "designed_to_trip": "constraint on size_bytes (1.2GB > max 1GB = 1073741824)",
    },
    {
        "text": "Store backup_2026_05_30.zip in the backups bucket, 500MB, retain 90 days.",
        "expected_tool": "demo_upload_file",
        "designed_to_trip": "enum on content_type ('zip' / 'application/zip' not in allowed enum)",
    },
    {
        "text": "Upload my data.csv export (about 50MB) to the analytics bucket, retain 7 days.",
        "expected_tool": "demo_upload_file",
        "designed_to_trip": "enum on bucket ('analytics' not in {user-uploads, media-assets, exports, backups})",
    },

    # ── demo_send_email_campaign (5) ────────────────────────────────
    {
        "text": "Send the 'Spring Sale' campaign to all paid users from hello@cruxial.ai, subject 'Spring Sale - 30% off'. Use template tmpl_springsale01.",
        "expected_tool": "demo_send_email_campaign",
        "designed_to_trip": "clean baseline — exercises template_id pattern + audience enum",
    },
    {
        "text": "Schedule the welcome email campaign for new signups, from welcome@cruxial.ai, subject 'Welcome!', template ID tmpl_welcome.",
        "expected_tool": "demo_send_email_campaign",
        "designed_to_trip": "pattern on template_id (tmpl_welcome too short — needs 8+ chars after underscore)",
    },
    {
        "text": "Run the re-engagement campaign for churned customers from hello@cruxial.ai, A/B test variant A, template tmpl_reengage99.",
        "expected_tool": "demo_send_email_campaign",
        "designed_to_trip": "may pass — exercises ab_test_variant",
    },
    {
        "text": "Send the Q3 product update to our active users in the last month, subject 'New in Cruxial Q3', from selva@cruxial.ai, template tmpl_q3update22.",
        "expected_tool": "demo_send_email_campaign",
        "designed_to_trip": "enum on audience_segment (model may say 'active_users_30d' vs 'active_30d')",
    },
    {
        "text": "Send a campaign to the paying customers, from marketing@cruxial.ai, subject 'Loyalty offer', template tmpl_loyal0001. Reply to support@cruxial.ai.",
        "expected_tool": "demo_send_email_campaign",
        "designed_to_trip": "enum on audience_segment (paying customers vs 'paid')",
    },

    # ── demo_create_database_backup (5) ─────────────────────────────
    {
        "text": "Create a full backup of database db_userdata99 in us-east-1, retain for 30 days, compress.",
        "expected_tool": "demo_create_database_backup",
        "designed_to_trip": "clean baseline — exercises database_id pattern + backup_type enum",
    },
    {
        "text": "Run an incremental backup of the userdata DB in us-east, keep for 90 days.",
        "expected_tool": "demo_create_database_backup",
        "designed_to_trip": "pattern on database_id (model may emit 'userdata' without db_ prefix); enum on storage_region (us-east vs us-east-1)",
    },
    {
        "text": "Create a daily incremental backup of db_metricsprod42 with 10 year retention, store in eu-west-1, encrypt with key_arn_abc123def456ghi.",
        "expected_tool": "demo_create_database_backup",
        "designed_to_trip": "constraint on retention_days (10 years = 3650, ok but borderline); pattern on encryption_key_id (model may invent 'key_arn_' format)",
    },
    {
        "text": "Make a differential backup of db_orderslive on Asia/Singapore region, 60 days retention, notify ops@cruxial.ai.",
        "expected_tool": "demo_create_database_backup",
        "designed_to_trip": "enum on storage_region ('Asia/Singapore' vs 'ap-southeast-1')",
    },
    {
        "text": "Backup the prod customer database for the next 5 years, compressed, full backup, store in us-west-2.",
        "expected_tool": "demo_create_database_backup",
        "designed_to_trip": "pattern on database_id (model may say 'prod_customer' or 'customers'); constraint on retention_days (5 years = 1825, ok)",
    },

    # ── demo_book_flight (5) ────────────────────────────────────────
    {
        "text": "Book a flight from BLR to SFO on 2026-08-15, 1 passenger, economy class, 1 bag.",
        "expected_tool": "demo_book_flight",
        "designed_to_trip": "clean baseline — exercises IATA codes correctly",
    },
    {
        "text": "Book 2 business class tickets from Bangalore to San Francisco for next month, return after 10 days, 2 bags each.",
        "expected_tool": "demo_book_flight",
        "designed_to_trip": "pattern on origin/destination (full city names not IATA codes)",
    },
    {
        "text": "I need a flight from LHR to JFK, departing 2026-07-20, return 2026-07-27, first-class for 1 person.",
        "expected_tool": "demo_book_flight",
        "designed_to_trip": "enum on cabin_class ('first-class' vs 'first')",
    },
    {
        "text": "Find me a flight from sfo to nrt for 4 passengers next Friday, premium economy, max 1 stop.",
        "expected_tool": "demo_book_flight",
        "designed_to_trip": "pattern on origin/destination (lowercase 'sfo' / 'nrt' vs uppercase IATA)",
    },
    {
        "text": "Book 12 economy tickets from DEL to DXB for our company trip on 2026-09-01, no checked bags.",
        "expected_tool": "demo_book_flight",
        "designed_to_trip": "constraint on passengers (12 > max 9)",
    },

    # ── demo_register_user (5) ──────────────────────────────────────
    {
        "text": "Register a new user: email alice@cruxial.ai, full name 'Alice Kapoor', role admin, password 'SecurePass2026!', tenant tenant_cruxial1.",
        "expected_tool": "demo_register_user",
        "designed_to_trip": "clean if all formats correct; password >= 12 chars",
    },
    {
        "text": "Sign up bob.smith@example.com as a viewer, password 'pass123', tenant 'acme'. Preferred language English. Phone 415-555-0100.",
        "expected_tool": "demo_register_user",
        "designed_to_trip": "constraint on password (pass123 = 7 chars < min 12); pattern on tenant_id (no tenant_ prefix); enum on preferred_language ('English' vs 'en'); pattern on phone (no + prefix)",
    },
    {
        "text": "Create an account: priya@indiaco.in, full name 'Priya Sharma', billing role, tenant_indiacorp01, password 'VerySecure!Pass2026', preferred language Hindi.",
        "expected_tool": "demo_register_user",
        "designed_to_trip": "pattern on tenant_id (may emit 'tenant_indiacorp01' correctly or 'indiacorp_01'); enum on preferred_language ('Hindi' vs 'hi')",
    },
    {
        "text": "Register manager@startup.io as member, password 'CorrectHorseBatteryStaple', tenant tenant_startup123, invited by founder@startup.io. They want marketing emails.",
        "expected_tool": "demo_register_user",
        "designed_to_trip": "should mostly pass — clean baseline with marketing_opt_in boolean",
    },
    {
        "text": "Register Tomás García from Spain — email tomas@empresa.es, French speaker, password 'EstaEsUnaContraseñaFuerte', role admin, tenant tenant_empresa99, phone +34 612 345 678.",
        "expected_tool": "demo_register_user",
        "designed_to_trip": "pattern on phone (E.164 must not have spaces — model often keeps spaces)",
    },
]


__all__ = [
    "DEMO_TOOL_SCHEMAS",
    "DEMO_TOOL_EXECUTORS",
    "DEMO_TOOL_EXECUTORS_SYNC",
    "DEMO_TOOL_DESCRIPTIONS",
    "DEMO_OPENAI_TOOLS",
    "DEMO_ANTHROPIC_TOOLS",
    "DEMO_PROMPTS",
]
