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
                        "title": {"type": "string"},
                        "note": {"type": "string"},
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
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "due_date": {
                            "type": "string", "format": "date-time",
                            "nullable": True,
                        },
                        "defer_date": {
                            "type": "string", "format": "date-time",
                            "nullable": True,
                        },
                        "plan_date": {
                            "type": "string", "format": "date-time",
                            "nullable": True,
                        },
                        "completed_at": {
                            "type": "string", "format": "date-time",
                            "nullable": True,
                        },
                        "assigned_to": {
                            "type": "string", "nullable": True,
                            "description": "User ID this task is assigned to.",
                        },
                        "created_at": {"type": "string", "format": "date-time"},
                        "updated_at": {"type": "string", "format": "date-time"},
                        "external_id": {"type": "string", "nullable": True},
                        "external_ref": {"type": "string", "nullable": True},
                        "source": {"type": "string", "nullable": True},
                    },
                },
                "TaskCreate": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "note": {"type": "string"},
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
                        },
                        "project_id": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "due_date": {
                            "type": "string", "format": "date-time",
                        },
                        "defer_date": {
                            "type": "string", "format": "date-time",
                        },
                        "plan_date": {
                            "type": "string", "format": "date-time",
                        },
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
        },
    }
