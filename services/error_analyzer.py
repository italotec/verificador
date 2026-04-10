"""
Claude-powered error analysis service.

Analyzes automation errors using screenshots and context to classify
them as one-time vs recurring, and generates fix suggestions in
Claude Code prompt format.
"""

import base64
import logging
from pathlib import Path

from web_app import db
from web_app.models import ErrorReport

logger = logging.getLogger(__name__)


def _load_screenshot_b64(screenshot_path: str | None) -> str | None:
    """Load a screenshot file and return as base64 string."""
    if not screenshot_path:
        return None
    path = Path(screenshot_path)
    if not path.is_absolute():
        # Resolve relative to Flask static/screenshots/
        try:
            from flask import current_app
            path = Path(current_app.static_folder) / "screenshots" / screenshot_path
        except RuntimeError:
            pass  # Outside app context — keep relative
    if not path.exists():
        return None
    try:
        return base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    except Exception:
        return None


def analyze_error(
    *,
    waba_record_id: int | None = None,
    job_id: int | None = None,
    error_type: str,
    error_message: str,
    screenshot_path: str | None = None,
    page_url: str | None = None,
    step_name: str | None = None,
    traceback_str: str | None = None,
    page_html: str | None = None,
) -> ErrorReport:
    """
    Create an ErrorReport and optionally analyze with Claude.

    Returns the created ErrorReport instance.
    """
    import config as app_config

    report = ErrorReport(
        waba_record_id=waba_record_id,
        job_id=job_id,
        error_type=error_type,
        error_message=error_message,
        screenshot_path=screenshot_path,
        page_url=page_url,
        step_name=step_name,
    )
    db.session.add(report)
    db.session.commit()

    # Try LLM analysis if Anthropic key is available
    if not app_config.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set, skipping LLM error analysis")
        return report

    try:
        _run_llm_analysis(report, screenshot_path, traceback_str, page_html)
    except Exception as e:
        logger.error(f"LLM error analysis failed: {e}")

    return report


def _run_llm_analysis(
    report: ErrorReport,
    screenshot_path: str | None,
    traceback_str: str | None,
    page_html: str | None,
):
    """Run Claude analysis on an error report."""
    import anthropic
    import config as app_config

    client = anthropic.Anthropic(api_key=app_config.ANTHROPIC_API_KEY)

    # Build the message content
    content = []

    # Add screenshot if available
    screenshot_b64 = _load_screenshot_b64(screenshot_path)
    if screenshot_b64:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": screenshot_b64,
            }
        })

    # Truncate page HTML to 10KB for the prompt
    html_snippet = ""
    if page_html:
        html_snippet = page_html[:10000]
        if len(page_html) > 10000:
            html_snippet += "\n... [truncated]"

    analysis_prompt = f"""Analyze this Facebook/WhatsApp Business automation error.

**Step that failed:** {report.step_name or 'Unknown'}
**Error Type:** {report.error_type}
**Error Message:** {report.error_message}
**Page URL at failure:** {report.page_url or 'Unknown'}
{f'**Traceback:**\n```\n{traceback_str}\n```' if traceback_str else ''}
{f'**Page HTML at failure (truncated):**\n```html\n{html_snippet}\n```' if html_snippet else ''}
{"**Screenshot:** Attached above — examine it carefully for Facebook dialogs, error messages, CAPTCHAs, or unexpected UI states." if screenshot_b64 else "**Screenshot:** Not available."}

This bot automates Facebook Business Verification via Playwright + AdsPower. The steps are:
1. Login (cookies or email/password + 2FA)
2. Create Business Portfolio
3. Fill company details (address, phone, website)
4. Add domain + verify via meta-tag
5. Create WABA (WhatsApp Business Account)
6. Business Verification wizard (upload CNPJ PDF, verify via domain or SMS OTP)

Analyze this error thoroughly:

1. **Root Cause**: Based on the error message, page URL, page HTML, and screenshot (if available), what SPECIFICALLY went wrong? Be precise — e.g. "The login page showed a checkpoint/CAPTCHA at URL /checkpoint/..." or "The company details form was not found because the page redirected to ...".

2. **Classification**: Is this a ONE-TIME error (network glitch, timing issue, Facebook temporary checkpoint, API rate limit, stale session) or a RECURRING error (selector permanently broken, flow restructured by Facebook, logic bug in the code)?

3. **Fix Suggestion**: ONLY if this is a RECURRING error that requires a code change, write a detailed prompt that can be pasted into Claude Code in VS Code to fix the issue. Include:
   - Which file(s) and function(s) to modify
   - What the current code does wrong
   - What the fix should be
   - Any relevant selectors or page structure from the HTML

If this is a ONE-TIME error (no code fix needed), set "fix_suggestion" to null.

Respond in this exact JSON format:
{{"is_recurring": true/false, "analysis": "detailed root cause analysis...", "fix_suggestion": "Claude Code prompt to fix the issue... or null"}}"""

    content.append({"type": "text", "text": analysis_prompt})

    response = client.messages.create(
        model=app_config.CLAUDE_SMART_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )

    response_text = response.content[0].text

    # Parse the JSON response
    import json
    try:
        json_start = response_text.find("{")
        json_end = response_text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            data = json.loads(response_text[json_start:json_end])
            report.is_recurring = data.get("is_recurring", False)
            report.llm_analysis = data.get("analysis", response_text)
            fix = data.get("fix_suggestion")
            report.fix_suggestion = fix if fix and fix != "null" else ""
        else:
            report.llm_analysis = response_text
    except json.JSONDecodeError:
        report.llm_analysis = response_text

    db.session.commit()
