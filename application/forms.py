"""WTForms used by the web UI."""
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    IntegerField,
    PasswordField,
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, EqualTo, Length, NumberRange, Optional


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    focus = BooleanField("Choose a focus after signing in")
    submit = SubmitField("Sign in")


class ForgotPasswordForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    submit = SubmitField("Send reset link")


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "New password",
        validators=[
            DataRequired(),
            Length(min=8),
            EqualTo("password2", message="Passwords must match."),
        ],
    )
    password2 = PasswordField("Repeat password", validators=[DataRequired()])
    submit = SubmitField("Set new password")


class RegisterForm(FlaskForm):
    display_name = StringField(
        "Name", validators=[DataRequired(), Length(min=2, max=128)]
    )
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField(
        "Password",
        validators=[DataRequired(), Length(min=8), EqualTo("password2", message="Passwords must match.")],
    )
    password2 = PasswordField("Repeat password", validators=[DataRequired()])
    submit = SubmitField("Create account")


class TaskForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired(), Length(max=500)])
    note = TextAreaField("Note", validators=[Optional()])
    project = SelectField("Project", choices=[], validators=[Optional()])
    # validate_choice off: the inspector token field can type-create new tags,
    # so names that aren't in `choices` yet must reach the view (which creates
    # them on the user). See views/tasks.py::_apply_common_fields.
    tags = SelectMultipleField("Tags / Contexts", choices=[], validate_choice=False)
    status = SelectField(
        "Status",
        choices=[
            ("open", "Open"),
            ("next", "Next"),
            ("waiting", "Waiting"),
            ("waiting_reply", "Waiting on reply"),
            ("scheduled", "Scheduled"),
            ("someday", "Someday/Maybe"),
            ("done", "Done"),
            ("dropped", "Dropped"),
        ],
        default="open",
    )
    flagged = BooleanField("Flagged")
    priority = SelectField(
        "Priority",
        choices=[("", "—"), ("1", "P1"), ("2", "P2"), ("3", "P3"), ("4", "P4")],
        default="",
    )
    defer_date = DateTimeLocalField("Defer until", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    due_date = DateTimeLocalField("Due date", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    plan_date = DateTimeLocalField("Plan date", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    estimated_minutes = IntegerField("Estimate (minutes)", validators=[Optional()])
    location = StringField("Location", validators=[Optional(), Length(max=300)])
    repeat_every_days = IntegerField("Repeat every", validators=[Optional()])
    repeat_unit = SelectField(
        "Unit",
        choices=[
            ("day", "day(s)"),
            ("weekday", "weekday(s) (Mon–Fri)"),
            ("weekend", "weekend day(s) (Sat/Sun)"),
            ("week", "week(s)"),
            ("month", "month(s)"),
            ("year", "year(s)"),
        ],
        default="day",
    )
    repeat_from_completion = BooleanField("Repeat from completion")
    # Specific weekdays (0=Mon … 6=Sun) — when any are picked they drive the
    # recurrence instead of the every-N-units schedule. Coerce to int so the
    # submitted string values land as the ints the model stores.
    repeat_weekdays = SelectMultipleField(
        "Repeat on weekdays",
        choices=[
            ("0", "Mon"), ("1", "Tue"), ("2", "Wed"), ("3", "Thu"),
            ("4", "Fri"), ("5", "Sat"), ("6", "Sun"),
        ],
        coerce=int,
        validators=[Optional()],
    )
    # Fixed day of month (1–31) — when set, the task repeats monthly on that
    # calendar day, overriding the every-N-units / weekday schedules.
    repeat_monthday = IntegerField(
        "Repeat on day of month", validators=[Optional(), NumberRange(min=1, max=31)]
    )
    assigned_to = SelectField("Assign to", choices=[], validators=[Optional()])
    # Blank = inherit the parent project's review cadence; a number overrides
    # (0 = never review). See Item.effective_review_interval.
    review_interval_days = IntegerField(
        "Review interval (days)", validators=[Optional(), NumberRange(min=0)]
    )
    is_internal = BooleanField("Internal — hide from public share dashboard")
    # Follow-up: who you're chasing (free text, for external contacts), when
    # to nudge next, and the cadence the "Mark followed up" button rolls that
    # reminder forward by.
    waiting_on = StringField("Waiting on", validators=[Optional(), Length(max=300)])
    followup_at = DateTimeLocalField(
        "Follow up on", format="%Y-%m-%dT%H:%M", validators=[Optional()]
    )
    followup_every_days = IntegerField(
        "Follow up every (days)", validators=[Optional(), NumberRange(min=1)]
    )
    add_another = BooleanField("Add another after saving")
    open_after_create = BooleanField("Open the task after creating")
    submit = SubmitField("Save task")


class CommentForm(FlaskForm):
    body = TextAreaField("Comment", validators=[DataRequired()])
    submit = SubmitField("Post comment")


class ProjectForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=200)])
    note = TextAreaField("Note", validators=[Optional()])
    color = StringField("Color (hex)", validators=[Optional(), Length(max=16)])
    tags = SelectMultipleField("Labels", choices=[])
    defer_date = DateTimeLocalField("Defer until", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    due_date = DateTimeLocalField("Due date", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    plan_date = DateTimeLocalField("Plan date", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    kind = SelectField(
        "Type",
        choices=[
            ("project", "📁  Project — has actions and structure"),
            ("folder",  "🗂️  Folder — only groups other projects"),
            ("section", "🔖  Section — inline group within a parent project"),
        ],
        default="project",
    )
    project_type = SelectField(
        "Type",
        choices=[
            ("parallel", "Parallel"),
            ("sequential", "Sequential"),
            ("single_actions", "Single actions"),
        ],
        default="parallel",
    )
    status = SelectField(
        "Status",
        choices=[
            ("active", "Active"),
            ("on_hold", "On hold"),
            ("completed", "Completed"),
            ("dropped", "Dropped"),
        ],
        default="active",
    )
    parent = SelectField("Inside", choices=[], validators=[Optional()])
    review_interval_days = IntegerField(
        "Review interval (days)", default=7, validators=[Optional()]
    )
    auto_complete_when_done = BooleanField(
        "Complete project when its last task is done"
    )
    members = SelectMultipleField("Members (collaboration)", choices=[])
    shared_teams = SelectMultipleField("Share with teams", choices=[])
    # NB: no ``jira_url`` field — the Jira base URL is set only in the CSV
    # import dialog (projects.import_csv), never via the project inspector.
    submit = SubmitField("Save project")


class TagForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=64)])
    color = StringField("Color (hex)", default="#6c757d")
    is_context = BooleanField("Display as @context", default=True)
    on_hold = BooleanField("On hold")
    parent = SelectField("Group (parent tag)", choices=[], validators=[Optional()])
    exclusive_children = BooleanField(
        "Children are mutually exclusive — only one child tag per task",
    )
    maps_to_status = SelectField(
        "Auto-status",
        choices=[
            ("", "— no automatic status —"),
            ("open", "Open"),
            ("next", "Next"),
            ("waiting", "Waiting"),
            ("waiting_reply", "Waiting on reply"),
            ("scheduled", "Scheduled"),
            ("someday", "Someday / Maybe"),
            ("done", "Done"),
            ("dropped", "Dropped"),
        ],
        default="",
        validators=[Optional()],
    )
    submit = SubmitField("Save tag")
