"""
Admin error panel — view, filter, and manage ErrorReports.
"""

from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from web_app import db
from web_app.models import ErrorReport

bp = Blueprint("errors", __name__, url_prefix="/errors")


def _admin_required(f):
    """Decorator: require admin role."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            return jsonify({"error": "Admin required"}), 403
        return f(*args, **kwargs)
    return wrapper


@bp.route("/")
@login_required
@_admin_required
def error_list():
    """Show all error reports."""
    filter_type = request.args.get("filter", "all")
    page = request.args.get("page", 1, type=int)
    per_page = 25

    query = ErrorReport.query

    if filter_type == "recurring":
        query = query.filter_by(is_recurring=True)
    elif filter_type == "unresolved":
        query = query.filter_by(resolved=False)

    pagination = query.order_by(ErrorReport.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # Count unresolved for badge
    unresolved_count = ErrorReport.query.filter_by(resolved=False).count()

    return render_template(
        "admin_errors.html",
        errors=pagination.items,
        pagination=pagination,
        filter_type=filter_type,
        unresolved_count=unresolved_count,
    )


@bp.route("/<int:error_id>/resolve", methods=["POST"])
@login_required
@_admin_required
def resolve_error(error_id):
    """Mark an error as resolved."""
    error = db.session.get(ErrorReport, error_id)
    if not error:
        return jsonify({"error": "Not found"}), 404

    error.resolved = True
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/<int:error_id>")
@login_required
@_admin_required
def error_detail(error_id):
    """Get error detail as JSON (for modals)."""
    error = db.session.get(ErrorReport, error_id)
    if not error:
        return jsonify({"error": "Not found"}), 404

    return jsonify({
        "id": error.id,
        "error_type": error.error_type,
        "error_message": error.error_message,
        "step_name": error.step_name,
        "page_url": error.page_url,
        "screenshot_path": error.screenshot_path,
        "llm_analysis": error.llm_analysis,
        "fix_suggestion": error.fix_suggestion,
        "is_recurring": error.is_recurring,
        "resolved": error.resolved,
        "created_at": error.created_at.isoformat() if error.created_at else None,
    })


@bp.route("/unresolved-count")
@login_required
def unresolved_count():
    """Return unresolved error count (for nav badge)."""
    count = ErrorReport.query.filter_by(resolved=False).count()
    return jsonify({"count": count})
