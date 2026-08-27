"""OpenAPI 3.0 specification for the Cairn REST API (``/api/v1``).

Kept in pure Python so we don't add a heavyweight schema framework
just to document the existing hand-rolled API. The spec is served at
``/api/v1/openapi.json`` and rendered by Swagger UI at
``/api/v1/docs``.
"""
from application import APP_VERSION


def build_openapi_spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Cairn REST API",
            "version": APP_VERSION,
            "description": (
                "Token-authenticated REST interface for the Cairn task /"
                " project manager. Mint a token via "
                "`./cairn issue-token <username>` and pass it in either "
                "the `Authorization: Bearer <token>` header or the "
                "`X-API-Token: <token>` header."
            ),
        },
        "servers": [
            {"url": "/api/v1", "description": "Current instance"},
        ],
        "security": [{"BearerAuth": []}, {"TokenHeader": []}],
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "API token issued via `cairn issue-token`.",
                },
                "TokenHeader": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Token",
                },
            },
            "schemas": {
                "Error": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                    "required": ["error"],
                },
                "Task": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "format": "objectid"},
                        "kind": {
                            "type": "string",
                            "enum": ["task", "project", "section", "folder"],
                        },
                        "title": {"type": "string"},
                        "note": {"type": "string"},
                        "color": {"type": "string", "nullable": True},
                        "location": {"type": "string", "nullable": True},
                        "sort_order": {"type": "integer"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "open", "next", "waiting", "waiting_reply",
                                "review", "scheduled", "someday", "done",
                                "dropped",
                            ],
                        },
                        "flagged": {"type": "boolean"},
                        "priority": {
                            "type": "integer", "minimum": 1, "maximum": 4,
                            "nullable": True,
                        },
                        "project_id": {"type": "string", "nullable": True},
                        "parent_id": {
                            "type": "string", "nullable": True,
                            "description": "Alias of project_id (project == parent). "
                                            "May point at another task → a sub-task.",
                        },
                        "related_ids": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Symmetric 'see also' links to other items.",
                        },
                        "checklist": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/ChecklistItem"},
                        },
                        "waiting_on": {"type": "string", "nullable": True},
                        "followup_at": {"type": "string", "format": "date-time", "nullable": True},
                        "followup_every_days": {"type": "integer", "nullable": True},
                        "tags": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "external_tags": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Tags managed by an importer; "
                                            "user-added tags = tags − external_tags.",
                        },
                        "due_date": {"type": "string", "format": "date-time", "nullable": True},
                        "defer_date": {"type": "string", "format": "date-time", "nullable": True},
                        "plan_date": {"type": "string", "format": "date-time", "nullable": True},
                        "completed_at": {"type": "string", "format": "date-time", "nullable": True},
                        "estimated_minutes": {"type": "integer", "nullable": True},
                        "repeat_every_days": {"type": "integer", "nullable": True},
                        "repeat_unit": {
                            "type": "string",
                            "enum": ["day", "weekday", "weekend", "week", "month", "year"],
                        },
                        "repeat_weekdays": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 6},
                            "description": "Weekdays the task repeats on (0=Mon .. 6=Sun).",
                        },
                        "repeat_monthday": {
                            "type": "integer", "nullable": True,
                            "minimum": 1, "maximum": 31,
                            "description": "Fixed day of month for a monthly repeat (1..31).",
                        },
                        "repeat_from_completion": {"type": "boolean"},
                        "is_internal": {"type": "boolean"},
                        "assigned_to": {
                            "type": "object", "nullable": True,
                            "description": "Brief user record.",
                        },
                        "created_at": {"type": "string", "format": "date-time"},
                        "updated_at": {"type": "string", "format": "date-time"},
                        "external_id": {
                            "type": "string", "nullable": True,
                            "description": "Prefixed internal id (csv:…, zammad:…).",
                        },
                        "external_ref": {
                            "type": "string", "nullable": True,
                            "description": "Bare ticket reference shown in the UI (e.g. CHMK-417).",
                        },
                        "source": {
                            "type": "string", "nullable": True,
                            "enum": ["todoist", "zammad", "csv", "jira", None],
                        },
                        "attachments": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Attachment"},
                            "description": "Files dropped onto the note. Fetch the "
                                            "binary from each item's `url`.",
                        },
                        "note_images": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Attachment"},
                            "description": "Images pasted inline into the note "
                                            "(no filename). Fetch from `url`.",
                        },
                    },
                },
                "Attachment": {
                    "type": "object",
                    "properties": {
                        "token": {"type": "string"},
                        "filename": {
                            "type": "string", "nullable": True,
                            "description": "Absent for pasted note_images.",
                        },
                        "content_type": {"type": "string"},
                        "size": {"type": "integer", "description": "Bytes."},
                        "created_at": {"type": "string", "format": "date-time", "nullable": True},
                        "url": {
                            "type": "string",
                            "description": "GET (token-authenticated) to download the binary.",
                        },
                    },
                },
                "TaskCreate": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "note": {"type": "string"},
                        "color": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "open", "next", "waiting", "waiting_reply",
                                "review", "scheduled", "someday", "done",
                                "dropped",
                            ],
                        },
                        "flagged": {"type": "boolean"},
                        "priority": {"type": "integer", "minimum": 1, "maximum": 4},
                        "location": {"type": "string"},
                        "sort_order": {"type": "integer"},
                        "project_id": {"type": "string"},
                        "parent_id": {
                            "type": "string",
                            "description": "Alias of project_id. Point at another task "
                                            "to make this a sub-task; null/empty for Inbox.",
                        },
                        "related_ids": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Full desired set of symmetric 'see also' "
                                            "links — replaces the existing set (both sides "
                                            "kept in sync). Omit to leave links untouched.",
                        },
                        "checklist": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/ChecklistItem"},
                            "description": "Replaces the whole checklist. Plain strings "
                                            "are accepted as unchecked items.",
                        },
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "due_date": {"type": "string", "format": "date-time"},
                        "defer_date": {"type": "string", "format": "date-time"},
                        "plan_date": {"type": "string", "format": "date-time"},
                        "waiting_on": {"type": "string"},
                        "followup_at": {"type": "string", "format": "date-time"},
                        "followup_every_days": {"type": "integer"},
                        "estimated_minutes": {"type": "integer"},
                        "repeat_every_days": {"type": "integer"},
                        "repeat_unit": {
                            "type": "string",
                            "enum": ["day", "weekday", "weekend", "week", "month", "year"],
                        },
                        "repeat_weekdays": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 6},
                        },
                        "repeat_monthday": {
                            "type": "integer", "nullable": True,
                            "minimum": 1, "maximum": 31,
                        },
                        "repeat_from_completion": {"type": "boolean"},
                        "is_internal": {"type": "boolean"},
                        "assigned_to_id": {
                            "type": "string",
                            "description": "User pk to assign this task to. Null/empty to clear.",
                        },
                    },
                },
                "ChecklistItem": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string", "maxLength": 500},
                        "done": {"type": "boolean", "default": False},
                    },
                },
                "TaskUpdate": {
                    "allOf": [{"$ref": "#/components/schemas/TaskCreate"}],
                    "description": "Same shape as TaskCreate but every field is optional.",
                },
                "TaskList": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Task"},
                        },
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                },
                "Project": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "note": {"type": "string"},
                        "color": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["project", "folder", "section"],
                        },
                        "project_type": {
                            "type": "string",
                            "enum": ["parallel", "sequential", "single_actions"],
                        },
                        "status": {
                            "type": "string",
                            "enum": ["active", "on_hold", "completed", "dropped"],
                        },
                        "parent_id": {"type": "string", "nullable": True},
                        "related_ids": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Symmetric 'see also' links to other items.",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "review_interval_days": {"type": "integer"},
                        "next_review_at": {
                            "type": "string", "format": "date-time",
                            "nullable": True,
                        },
                    },
                },
                "ProjectCreate": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "note": {"type": "string"},
                        "color": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["project", "folder", "section"],
                            "default": "project",
                        },
                        "project_type": {
                            "type": "string",
                            "enum": ["parallel", "sequential", "single_actions"],
                        },
                        "parent_id": {"type": "string"},
                        "related_ids": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Replaces the symmetric 'see also' link set.",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "review_interval_days": {"type": "integer"},
                    },
                },
                "Tag": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "color": {"type": "string"},
                        "is_context": {"type": "boolean"},
                        "on_hold": {"type": "boolean"},
                        "parent": {"type": "string", "nullable": True},
                        "exclusive_children": {"type": "boolean"},
                        "maps_to_status": {
                            "type": "string", "nullable": True,
                            "description": "Auto-status mapping — when this tag is applied, task status snaps to this value.",
                        },
                    },
                },
                "TagCreate": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "color": {"type": "string"},
                        "is_context": {"type": "boolean"},
                        "on_hold": {"type": "boolean"},
                        "parent": {"type": "string"},
                        "exclusive_children": {"type": "boolean"},
                        "maps_to_status": {"type": "string"},
                    },
                },
                "BulkRequest": {
                    "type": "object",
                    "required": ["ids", "action"],
                    "properties": {
                        "ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "action": {"type": "string"},
                        "payload": {"type": "object"},
                    },
                },
                "BulkResponse": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "integer"},
                        "skipped": {"type": "integer"},
                    },
                },
                # --- time tracking. Money is in cents, durations in minutes,
                # exactly as stored — no float ever touches a rate.
                "TimeTeam": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "format": "objectid"},
                        "name": {"type": "string"},
                    },
                },
                "TimeCustomer": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "format": "objectid"},
                        "team_id": {"type": "string", "format": "objectid"},
                        "name": {"type": "string"},
                        "note": {"type": "string"},
                        "archived": {"type": "boolean"},
                        "external_id": {
                            "type": "string", "nullable": True,
                            "description": ("The key the importer stamped when "
                                            "it created this record, e.g. "
                                            "`mite:41` or `zammad:org:7`."),
                        },
                        "external_ids": {
                            "type": "array", "items": {"type": "string"},
                            "description": ("Further keys the same customer "
                                            "answers to, adopted from records "
                                            "an importer found it already had."),
                        },
                        "legal_name": {"type": "string", "nullable": True},
                        "address": {"type": "string"},
                        "vat_id": {"type": "string", "nullable": True},
                        "tax_number": {"type": "string", "nullable": True},
                        "invoice_email": {"type": "string", "nullable": True},
                        "contact_name": {"type": "string", "nullable": True},
                        "phone": {"type": "string", "nullable": True},
                        "payment_terms_days": {"type": "integer", "nullable": True},
                        "customer_number": {"type": "string", "nullable": True},
                        "billing_note": {"type": "string"},
                        "created_at": {"type": "string", "format": "date-time"},
                    },
                },
                "TimeProject": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "format": "objectid"},
                        "team_id": {"type": "string", "format": "objectid"},
                        "customer_id": {"type": "string", "format": "objectid"},
                        "name": {"type": "string"},
                        "note": {"type": "string"},
                        "hourly_rate_cents": {"type": "integer", "nullable": True},
                        "budget_value": {"type": "integer", "nullable": True},
                        "budget_type": {
                            "type": "string", "nullable": True,
                            "enum": ["minutes", "cents",
                                     "minutes_per_month", "cents_per_month"],
                        },
                        "archived": {"type": "boolean"},
                        "intake": {
                            "type": "boolean",
                            "description": ("Incoming external time for this "
                                            "customer books here. At most one "
                                            "project per customer carries it."),
                        },
                        "time_mode": {"type": "string",
                                      "enum": ["duration", "clock"]},
                        "external_id": {"type": "string", "nullable": True},
                        "external_ids": {"type": "array",
                                         "items": {"type": "string"}},
                        "custom_fields": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "label": {"type": "string"},
                                "field_type": {"type": "string",
                                               "enum": ["text", "select"]},
                                "default": {"type": "string"},
                                "options": {"type": "array",
                                            "items": {"type": "string"}},
                            },
                        }},
                        "created_at": {"type": "string", "format": "date-time"},
                    },
                },
                "TimeEntry": {
                    "type": "object",
                    "description": ("One logged chunk of time. Addressed by "
                                    "`bucket_id` + `entry_id`; entries live "
                                    "embedded in a per-customer-per-day "
                                    "document, so there is no list index to "
                                    "rely on."),
                    "properties": {
                        "entry_id": {"type": "string"},
                        "bucket_id": {"type": "string", "format": "objectid"},
                        "customer_id": {"type": "string", "nullable": True},
                        "customer": {"type": "string"},
                        "project_id": {"type": "string"},
                        "project": {"type": "string"},
                        "user_id": {"type": "string", "nullable": True},
                        "user_name": {"type": "string"},
                        "external_user": {
                            "type": "string", "nullable": True,
                            "description": ("Who booked it in the source "
                                            "system, for imported time whose "
                                            "author has no Cairn account yet."),
                        },
                        "spent_on": {"type": "string", "format": "date"},
                        "start_time": {"type": "string", "nullable": True},
                        "end_time": {"type": "string", "nullable": True},
                        "minutes": {"type": "integer"},
                        "note": {"type": "string"},
                        "description": {"type": "string"},
                        "source_label": {"type": "string", "nullable": True},
                        "service": {"type": "string", "nullable": True},
                        "billable": {"type": "boolean"},
                        "amount_cents": {"type": "integer", "nullable": True},
                        "locked": {
                            "type": "boolean",
                            "description": "Billed, and therefore frozen.",
                        },
                        "status": {"type": "string",
                                   "enum": ["active", "cancelled"]},
                        "archived_project": {"type": "boolean"},
                        "custom": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                            },
                        }},
                        "external_ref": {"type": "string", "nullable": True},
                        "external_url": {"type": "string", "nullable": True},
                    },
                },
                "DuplicateCustomerGroup": {
                    "type": "object",
                    "description": ("Records that look like one client filed "
                                    "twice. Heaviest first — the one carrying "
                                    "the history is normally the survivor."),
                    "properties": {
                        "name": {"type": "string"},
                        "customers": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "format": "objectid"},
                                "name": {"type": "string"},
                                "archived": {"type": "boolean"},
                                "external_id": {"type": "string",
                                                "nullable": True},
                                "external_ids": {"type": "array",
                                                 "items": {"type": "string"}},
                                "minutes": {"type": "integer"},
                                "entries": {"type": "integer"},
                                "projects": {"type": "array", "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "intake": {"type": "boolean"},
                                        "archived": {"type": "boolean"},
                                    },
                                }},
                            },
                        }},
                    },
                },
                "MergeResult": {
                    "type": "object",
                    "properties": {
                        "merged": {"type": "string",
                                   "description": "Name of the customer that was folded in."},
                        "into": {"$ref": "#/components/schemas/TimeCustomer"},
                        "stats": {
                            "type": "object",
                            "properties": {
                                "projects_moved": {"type": "integer"},
                                "projects_folded": {
                                    "type": "integer",
                                    "description": ("Projects of the same name "
                                                    "merged into the survivor's."),
                                },
                                "entries_moved": {"type": "integer"},
                                "duplicates_cancelled": {
                                    "type": "integer",
                                    "description": ("Bookings the target already "
                                                    "carried; they moved but "
                                                    "arrived cancelled, so no "
                                                    "hour is counted twice."),
                                },
                                "duplicates_kept": {
                                    "type": "integer",
                                    "description": ("Duplicates that were already "
                                                    "billed — frozen, left active, "
                                                    "reported for a human."),
                                },
                                "relinked": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        },
        "paths": {
            "/time/teams": {
                "get": {
                    "summary": "Teams this token may act in",
                    "description": ("The billing module is team-scoped. Every "
                                    "/time endpoint takes an optional `team` "
                                    "query parameter and falls back to "
                                    "`default_team_id` from here."),
                    "tags": ["time"],
                    "responses": {
                        "200": {"description": "Teams",
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "properties": {
                                        "items": {"type": "array", "items": {
                                            "$ref": "#/components/schemas/TimeTeam"}},
                                        "default_team_id": {"type": "string",
                                                            "nullable": True},
                                    },
                                }}}},
                        "403": {"description": "Not a member of any team"},
                    },
                },
            },
            "/time/customers": {
                "get": {
                    "summary": "List billing customers",
                    "tags": ["time"],
                    "parameters": [
                        {"name": "team", "in": "query", "schema": {"type": "string", "format": "objectid"}, "description": "Team to operate on. Omitted → the caller's default team (see GET /time/teams)."},
                        {"name": "q", "in": "query", "schema": {"type": "string"},
                         "description": "Case-insensitive substring of the name."},
                        {"name": "include_archived", "in": "query",
                         "schema": {"type": "string", "enum": ["1"]}},
                    ],
                    "responses": {
                        "200": {"description": "Customers",
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "properties": {"items": {
                                        "type": "array", "items": {
                                            "$ref": "#/components/schemas/TimeCustomer"}}},
                                }}}},
                    },
                },
            },
            "/time/customers/duplicates": {
                "get": {
                    "summary": "Customers that look like one client filed twice",
                    "description": ("An importer that met a client under a key "
                                    "it did not recognise opened a second "
                                    "record on it. The importers adopt by name "
                                    "now, so this returns what they created "
                                    "before they did — and nothing merges those "
                                    "on its own, because the logged hours under "
                                    "a customer mean it cannot simply be "
                                    "deleted. Feed a group to "
                                    "POST /time/customers/{id}/merge."),
                    "tags": ["time"],
                    "parameters": [{"name": "team", "in": "query", "schema": {"type": "string", "format": "objectid"}, "description": "Team to operate on. Omitted → the caller's default team (see GET /time/teams)."}],
                    "responses": {
                        "200": {"description": "Duplicate groups",
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "properties": {
                                        "team_id": {"type": "string"},
                                        "count": {"type": "integer"},
                                        "items": {"type": "array", "items": {
                                            "$ref": "#/components/schemas/DuplicateCustomerGroup"}},
                                    },
                                }}}},
                    },
                },
            },
            "/time/customers/{customer_id}": {
                "get": {
                    "summary": "Get one customer",
                    "tags": ["time"],
                    "parameters": [
                        {"name": "customer_id", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                        {"name": "team", "in": "query", "schema": {"type": "string", "format": "objectid"}, "description": "Team to operate on. Omitted → the caller's default team (see GET /time/teams)."},
                    ],
                    "responses": {
                        "200": {"description": "Customer",
                                "content": {"application/json": {"schema": {
                                    "$ref": "#/components/schemas/TimeCustomer"}}}},
                        "404": {"description": "Not found"},
                    },
                },
            },
            "/time/customers/{customer_id}/merge": {
                "post": {
                    "summary": "Fold this customer into another and delete it",
                    "description": ("Everything moves: the projects (one of the "
                                    "same name is folded into the survivor's), "
                                    "every logged hour, and the import keys — so "
                                    "the next sync goes to the survivor instead "
                                    "of re-creating what was just merged. A "
                                    "booking the target already carries arrives "
                                    "cancelled rather than doubling the total; "
                                    "one already billed is frozen, left active "
                                    "and reported. Irreversible: team managers "
                                    "only, and read-only tokens cannot call it."),
                    "tags": ["time"],
                    "parameters": [
                        {"name": "customer_id", "in": "path", "required": True,
                         "schema": {"type": "string"},
                         "description": "The customer that is folded in and deleted."},
                        {"name": "team", "in": "query", "schema": {"type": "string", "format": "objectid"}, "description": "Team to operate on. Omitted → the caller's default team (see GET /time/teams)."},
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {"into": {
                                "type": "string", "format": "objectid",
                                "description": "The customer that survives."}},
                            "required": ["into"],
                        }}},
                    },
                    "responses": {
                        "200": {"description": "What moved",
                                "content": {"application/json": {"schema": {
                                    "$ref": "#/components/schemas/MergeResult"}}}},
                        "400": {"description": "`into` missing or the same customer"},
                        "403": {"description": "Not a team manager, or a read-only token"},
                        "404": {"description": "Unknown customer"},
                    },
                },
            },
            "/time/projects": {
                "get": {
                    "summary": "List billing projects",
                    "description": ("Every billing project of the team — the "
                                    "same structure the module's management "
                                    "page shows any member. The narrower "
                                    "billing scope governs logged time, so it "
                                    "applies to /time/entries, not here."),
                    "tags": ["time"],
                    "parameters": [
                        {"name": "team", "in": "query", "schema": {"type": "string", "format": "objectid"}, "description": "Team to operate on. Omitted → the caller's default team (see GET /time/teams)."},
                        {"name": "customer_id", "in": "query",
                         "schema": {"type": "string"}},
                        {"name": "include_archived", "in": "query",
                         "schema": {"type": "string", "enum": ["1"]}},
                    ],
                    "responses": {
                        "200": {"description": "Projects",
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "properties": {"items": {
                                        "type": "array", "items": {
                                            "$ref": "#/components/schemas/TimeProject"}}},
                                }}}},
                    },
                },
            },
            "/time/entries": {
                "get": {
                    "summary": "List logged time",
                    "description": ("The same reader and the same filters as "
                                    "the report page. Range: `period` (a "
                                    "preset) or `from`/`to`, which ARE the "
                                    "period when given without one; the "
                                    "default is this month."),
                    "tags": ["time"],
                    "parameters": [
                        {"name": "team", "in": "query", "schema": {"type": "string", "format": "objectid"}, "description": "Team to operate on. Omitted → the caller's default team (see GET /time/teams)."},
                        {"name": "period", "in": "query",
                         "schema": {"type": "string",
                                    "enum": ["today", "yesterday", "this_week",
                                             "last_week", "this_month",
                                             "last_month", "this_year",
                                             "custom"]}},
                        {"name": "from", "in": "query",
                         "schema": {"type": "string", "format": "date"}},
                        {"name": "to", "in": "query",
                         "schema": {"type": "string", "format": "date"}},
                        {"name": "customer_id", "in": "query",
                         "schema": {"type": "string"}},
                        {"name": "project_id", "in": "query",
                         "schema": {"type": "string"}},
                        {"name": "user_id", "in": "query",
                         "schema": {"type": "string"},
                         "description": ("`none` selects imported time whose "
                                         "author has no Cairn account.")},
                        {"name": "state", "in": "query",
                         "schema": {"type": "string",
                                    "enum": ["active", "cancelled", "all"],
                                    "default": "active"},
                         "description": ("Deleted time is cancelled, never "
                                         "dropped, so it stays findable.")},
                        {"name": "billable", "in": "query",
                         "schema": {"type": "string", "enum": ["1", "0"]}},
                        {"name": "locked", "in": "query",
                         "schema": {"type": "string", "enum": ["1", "0"]}},
                    ],
                    "responses": {
                        "200": {"description": "Entries",
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "properties": {
                                        "team_id": {"type": "string"},
                                        "from": {"type": "string", "format": "date"},
                                        "to": {"type": "string", "format": "date"},
                                        "count": {"type": "integer"},
                                        "total_minutes": {
                                            "type": "integer",
                                            "description": "Active entries only."},
                                        "items": {"type": "array", "items": {
                                            "$ref": "#/components/schemas/TimeEntry"}},
                                    },
                                }}}},
                    },
                },
            },
            "/me": {
                "get": {
                    "summary": "Identify the token holder",
                    "tags": ["meta"],
                    "responses": {
                        "200": {
                            "description": "User info",
                            "content": {"application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "username": {"type": "string"},
                                        "email": {"type": "string"},
                                        "display_name": {"type": "string"},
                                        "level": {"type": "string"},
                                    },
                                },
                            }},
                        },
                    },
                },
            },
            "/tasks": {
                "get": {
                    "summary": "List tasks",
                    "tags": ["tasks"],
                    "parameters": [
                        {"name": "status", "in": "query", "schema": {"type": "string"}},
                        {"name": "open", "in": "query", "schema": {"type": "boolean"},
                         "description": "true → only open tasks (status not done/dropped). Ignored when 'status' is given."},
                        {"name": "project_id", "in": "query", "schema": {"type": "string"}},
                        {"name": "tag_id", "in": "query", "schema": {"type": "string"},
                         "description": "Tag NAME (the field is a list of strings)."},
                        {"name": "flagged", "in": "query", "schema": {"type": "boolean"}},
                        {"name": "assigned_to_me", "in": "query", "schema": {"type": "boolean"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 500, "default": 100}},
                        {"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0}},
                    ],
                    "responses": {
                        "200": {
                            "description": "Tasks",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/TaskList"},
                            }},
                        },
                    },
                },
                "post": {
                    "summary": "Create a task",
                    "tags": ["tasks"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/TaskCreate"},
                        }},
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/Task"},
                            }},
                        },
                        "400": {
                            "description": "Validation error",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/Error"},
                            }},
                        },
                    },
                },
            },
            "/tasks/{task_id}": {
                "parameters": [
                    {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "get": {
                    "summary": "Get a task",
                    "tags": ["tasks"],
                    "responses": {
                        "200": {"description": "Task",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}},
                        "404": {"description": "Not found"},
                    },
                },
                "patch": {
                    "summary": "Update a task",
                    "tags": ["tasks"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/TaskUpdate"},
                        }},
                    },
                    "responses": {
                        "200": {"description": "Updated",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}},
                        "404": {"description": "Not found"},
                    },
                },
                "delete": {
                    "summary": "Delete a task",
                    "tags": ["tasks"],
                    "responses": {
                        "204": {"description": "Deleted"},
                        "404": {"description": "Not found"},
                    },
                },
            },
            "/tasks/{task_id}/complete": {
                "parameters": [
                    {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "post": {
                    "summary": "Mark a task done (with recurrence handling)",
                    "tags": ["tasks"],
                    "responses": {
                        "200": {
                            "description": "Completed task plus next occurrence if recurring",
                            "content": {"application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "completed": {"$ref": "#/components/schemas/Task"},
                                        "next_occurrence": {
                                            "nullable": True,
                                            "anyOf": [{"$ref": "#/components/schemas/Task"}],
                                        },
                                    },
                                },
                            }},
                        },
                    },
                },
            },
            "/tasks/{task_id}/assign": {
                "parameters": [
                    {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "post": {
                    "summary": "Assign / unassign a task",
                    "tags": ["tasks"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "user_id": {
                                        "type": "string", "nullable": True,
                                        "description": "Pass null/omit to unassign.",
                                    },
                                },
                            },
                        }},
                    },
                    "responses": {
                        "200": {"description": "Task",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}},
                    },
                },
            },
            "/tasks/{task_id}/comments": {
                "parameters": [
                    {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "post": {
                    "summary": "Add a comment",
                    "tags": ["tasks"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {
                                "type": "object", "required": ["body"],
                                "properties": {
                                    "body": {"type": "string"},
                                    "is_public": {"type": "boolean"},
                                },
                            },
                        }},
                    },
                    "responses": {
                        "201": {"description": "Created"},
                    },
                },
            },
            "/tasks/{task_id}/attachments/{token}": {
                "parameters": [
                    {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "token", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "get": {
                    "summary": "Download a task attachment (file or pasted image)",
                    "tags": ["tasks"],
                    "description": "Streams the raw bytes of a note file or pasted "
                                   "image. Tokens come from a task's `attachments[]` "
                                   "/ `note_images[]`.",
                    "responses": {
                        "200": {
                            "description": "Binary content",
                            "content": {"application/octet-stream": {
                                "schema": {"type": "string", "format": "binary"},
                            }},
                        },
                        "404": {"description": "Not found"},
                    },
                },
            },
            "/tasks/bulk": {
                "post": {
                    "summary": "Bulk action on tasks",
                    "tags": ["tasks"],
                    "description": "Actions: `complete`, `reopen`, `status` (payload.status), `move` (payload.project_id), `tag` (payload.tags + payload.mode = add|replace), `delete`, `archive`.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/BulkRequest"},
                        }},
                    },
                    "responses": {
                        "200": {"description": "Result",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BulkResponse"}}}},
                    },
                },
            },
            "/projects": {
                "get": {
                    "summary": "List projects",
                    "tags": ["projects"],
                    "responses": {
                        "200": {
                            "description": "Projects",
                            "content": {"application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "items": {"type": "array",
                                                   "items": {"$ref": "#/components/schemas/Project"}},
                                    },
                                },
                            }},
                        },
                    },
                },
                "post": {
                    "summary": "Create a project / folder / section",
                    "tags": ["projects"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ProjectCreate"},
                        }},
                    },
                    "responses": {
                        "201": {"description": "Created",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Project"}}}},
                    },
                },
            },
            "/projects/{project_id}": {
                "parameters": [
                    {"name": "project_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "get": {
                    "summary": "Get a project",
                    "tags": ["projects"],
                    "responses": {
                        "200": {"description": "Project",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Project"}}}},
                    },
                },
                "patch": {
                    "summary": "Update a project",
                    "tags": ["projects"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ProjectCreate"},
                        }},
                    },
                    "responses": {"200": {"description": "Updated"}},
                },
                "delete": {
                    "summary": "Delete a project (cascade)",
                    "tags": ["projects"],
                    "responses": {"204": {"description": "Deleted"}},
                },
            },
            "/projects/{project_id}/review": {
                "parameters": [
                    {"name": "project_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "post": {
                    "summary": "Mark project as reviewed (re-arms the review clock)",
                    "tags": ["projects"],
                    "responses": {"200": {"description": "Reviewed"}},
                },
            },
            "/projects/bulk": {
                "post": {
                    "summary": "Bulk action on projects",
                    "tags": ["projects"],
                    "description": "Actions: `complete`, `archive`, `delete`, `set_review` (payload.review_interval_days).",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/BulkRequest"},
                        }},
                    },
                    "responses": {
                        "200": {"description": "Result",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BulkResponse"}}}},
                    },
                },
            },
            "/tags": {
                "get": {
                    "summary": "List tags",
                    "tags": ["tags"],
                    "responses": {
                        "200": {
                            "description": "Tags",
                            "content": {"application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "items": {"type": "array",
                                                   "items": {"$ref": "#/components/schemas/Tag"}},
                                    },
                                },
                            }},
                        },
                    },
                },
                "post": {
                    "summary": "Create a tag",
                    "tags": ["tags"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/TagCreate"},
                        }},
                    },
                    "responses": {
                        "201": {"description": "Created",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Tag"}}}},
                    },
                },
            },
            "/tags/{tag_name}": {
                "parameters": [
                    {"name": "tag_name", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "patch": {
                    "summary": "Update a tag",
                    "tags": ["tags"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/TagCreate"},
                        }},
                    },
                    "responses": {"200": {"description": "Updated"}},
                },
                "delete": {
                    "summary": "Delete a tag (removes from every task)",
                    "tags": ["tags"],
                    "responses": {"204": {"description": "Deleted"}},
                },
            },
            "/scratchpad": {
                "get": {
                    "summary": "Get the saved scratchpad text",
                    "tags": ["scratchpad"],
                    "responses": {
                        "200": {"description": "Text",
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                }}}},
                    },
                },
                "put": {
                    "summary": "Save the scratchpad text",
                    "tags": ["scratchpad"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        }}},
                    },
                    "responses": {"200": {"description": "Saved"}},
                },
            },
            "/scratchpad/convert": {
                "post": {
                    "summary": "Convert dumped text into tasks",
                    "description": ("One task per line, into ``project_id`` "
                                    "(omit for the Inbox); indentation nests "
                                    "sub-tasks and the quick-add tokens work "
                                    "per line. Idempotent: a task with the same "
                                    "title in the same parent is updated, not "
                                    "duplicated."),
                    "tags": ["scratchpad"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "project_id": {"type": "string"},
                            },
                            "required": ["text"],
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Conversion stats",
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "properties": {
                                        "ok": {"type": "boolean"},
                                        "stats": {
                                            "type": "object",
                                            "properties": {
                                                "tasks_created": {"type": "integer"},
                                                "tasks_updated": {"type": "integer"},
                                            },
                                        },
                                    },
                                }}}},
                    },
                },
            },
        },
    }
