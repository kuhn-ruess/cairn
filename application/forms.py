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
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional


class LoginForm(FlaskForm):
    username = StringField("Username or Email", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign in")


class ForgotPasswordForm(FlaskForm):
    identifier = StringField("Username or email", validators=[DataRequired()])
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
    username = StringField("Username", validators=[DataRequired(), Length(min=2, max=64)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    display_name = StringField("Display name", validators=[Optional(), Length(max=128)])
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
    tags = SelectMultipleField("Tags / Contexts", choices=[])
    status = SelectField(
        "Status",
        choices=[
            ("open", "Open"),
            ("next", "Next"),
            ("waiting", "Waiting"),
            ("scheduled", "Scheduled"),
            ("someday", "Someday/Maybe"),
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
    assigned_to = SelectField("Assign to", choices=[], validators=[Optional()])
    is_internal = BooleanField("Internal — hide from public share dashboard")
    add_another = BooleanField("Add another after saving")
    submit = SubmitField("Save task")


class CommentForm(FlaskForm):
    body = TextAreaField("Comment", validators=[DataRequired()])
    submit = SubmitField("Post comment")


class ProjectForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=200)])
    note = TextAreaField("Note", validators=[Optional()])
    color = StringField("Color (hex)", validators=[Optional(), Length(max=16)])
    tags = SelectMultipleField("Labels", choices=[])
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
    members = SelectMultipleField("Members (collaboration)", choices=[])
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
    submit = SubmitField("Save tag")
