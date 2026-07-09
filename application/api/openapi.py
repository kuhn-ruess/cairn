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
                                "open", "next", "waiting", "scheduled",
                                "someday", "done", "dropped",
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
                                "open", "next", "waiting", "scheduled",
                                "someday", "done", "dropped",
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
            },
        },
        "paths": {
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
            "/braindump": {
                "get": {
                    "summary": "Get the saved braindump scratch-pad text",
                    "tags": ["braindump"],
                    "responses": {
                        "200": {"description": "Text",
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                }}}},
                    },
                },
                "put": {
                    "summary": "Save the braindump scratch-pad text",
                    "tags": ["braindump"],
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
            "/braindump/convert": {
                "post": {
                    "summary": "Convert the Markdown dump into projects + tasks",
                    "description": ("Headings become projects (existing ones "
                                    "reused by name), list items become tasks, "
                                    "indentation nests sub-tasks. Idempotent: a "
                                    "task with the same title in the same project "
                                    "is updated, not duplicated."),
                    "tags": ["braindump"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
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
                                                "projects_created": {"type": "integer"},
                                                "projects_matched": {"type": "integer"},
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
