"""
Browser Discovery Service — Claude-powered flow recording and replay.

When a new automation flow is encountered (no existing recording):
1. Opens the browser at a given URL
2. Uses Claude to navigate step by step (screenshot → action)
3. Records every action with screenshots
4. Auto-generates a clean Playwright Python script
5. Polishes the script with a second Claude pass
6. Saves to BrowserRecording model

On subsequent runs:
1. Try polished_script (if is_tested=True)
2. Try generated_script
3. Fall back to Claude-driven discovery

Debug: Every action has before/after screenshots for full audit trail.
"""

import base64
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class DiscoveryError(Exception):
    pass


class BrowserDiscovery:
    """
    Drives a Playwright browser session with Claude computer-use style
    navigation, recording every action for later deterministic replay.
    """

    def __init__(self, page, task_name: str, debug_dir: str | None = None):
        self.page = page
        self.task_name = task_name
        self.steps: list[dict] = []
        import config as app_config
        self.debug_dir = Path(debug_dir or app_config.DEBUG_DIR) / "discovery" / task_name
        self.debug_dir.mkdir(parents=True, exist_ok=True)

        # Get Claude client
        client = None
        try:
            import anthropic
            api_key = app_config.ANTHROPIC_API_KEY or os.getenv("ANTHROPIC_API_KEY", "")
            if api_key:
                client = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            pass
        self.client = client
        self.model = app_config.CLAUDE_SMART_MODEL

    def _screenshot_b64(self, label: str) -> tuple[str, str]:
        """Take a screenshot, save it, and return (filepath, base64)."""
        ts = int(time.time() * 1000)
        path = self.debug_dir / f"{len(self.steps):03d}_{label}_{ts}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
        except Exception:
            self.page.screenshot(path=str(path))
        b64 = base64.b64encode(path.read_bytes()).decode()
        return str(path), b64

    def _ask_claude(self, prompt: str, img_b64: str) -> str:
        """Send screenshot + prompt to Claude, return text response."""
        if not self.client:
            raise DiscoveryError("ANTHROPIC_API_KEY not configured")
        response = self.client.messages.create(
            model=self.model,
            max_tokens=500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        return response.content[0].text.strip()

    def record_step(self, goal: str, max_attempts: int = 3) -> dict:
        """
        Ask Claude what action to take to achieve *goal*, execute it,
        record before/after screenshots.

        Returns a step dict with all recorded data.
        """
        before_path, before_b64 = self._screenshot_b64(f"before_{goal[:20].replace(' ', '_')}")

        # Ask Claude for the next action
        prompt = f"""You are automating a browser. Current goal: {goal}

Look at this screenshot and return a JSON action to execute. Use one of these formats:
{{"action": "click", "selector": "text=Button Label"}}
{{"action": "click", "selector": "[aria-label='...']"}}
{{"action": "type", "selector": "text=Label", "value": "text to type"}}
{{"action": "press", "key": "Enter"}}
{{"action": "wait", "ms": 1000}}
{{"action": "navigate", "url": "https://..."}}
{{"action": "done"}}

Reply with ONLY the JSON, nothing else. If the goal is already achieved, return {{"action": "done"}}.
If you're unsure, take the most conservative action (smallest click, no navigation)."""

        response_text = self._ask_claude(prompt, before_b64)
        logger.info(f"[Discovery] {self.task_name} step {len(self.steps)}: Claude → {response_text}")

        try:
            action_data = json.loads(response_text)
        except json.JSONDecodeError:
            # Try extracting JSON
            import re
            m = re.search(r'\{.*\}', response_text, re.DOTALL)
            if m:
                action_data = json.loads(m.group())
            else:
                raise DiscoveryError(f"Could not parse Claude response: {response_text}")

        # Execute the action
        action = action_data.get("action")
        error = None

        try:
            if action == "click":
                sel = action_data["selector"]
                self.page.locator(sel).first.click(timeout=5000)
                time.sleep(0.8)
            elif action == "type":
                sel = action_data["selector"]
                value = action_data.get("value", "")
                self.page.locator(sel).first.fill(value)
                time.sleep(0.3)
            elif action == "press":
                self.page.keyboard.press(action_data.get("key", "Enter"))
                time.sleep(0.3)
            elif action == "wait":
                time.sleep(action_data.get("ms", 1000) / 1000)
            elif action == "navigate":
                self.page.goto(action_data["url"], wait_until="domcontentloaded", timeout=30000)
                time.sleep(1)
            elif action == "done":
                pass  # goal achieved
        except Exception as e:
            error = str(e)
            logger.warning(f"[Discovery] Action failed: {e}")

        after_path, _ = self._screenshot_b64(f"after_{goal[:20].replace(' ', '_')}")

        step = {
            "index": len(self.steps),
            "goal": goal,
            "action": action_data,
            "before_screenshot": before_path,
            "after_screenshot": after_path,
            "error": error,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self.steps.append(step)
        return step

    def run_flow(self, goals: list[str]) -> list[dict]:
        """
        Run a sequence of goals, recording each step.
        Returns all recorded steps.
        """
        for goal in goals:
            step = self.record_step(goal)
            if step["action"].get("action") == "done":
                logger.info(f"[Discovery] Goal achieved: {goal}")
            elif step.get("error"):
                logger.warning(f"[Discovery] Step had error: {step['error']}")

        return self.steps

    def save_recording(self) -> "BrowserRecording":
        """
        Save the recorded steps to BrowserRecording model,
        generate a Playwright script, then Polish it with Claude.
        """
        steps_json = json.dumps(self.steps, indent=2)
        generated = self._generate_script(steps_json)
        polished = self._polish_script(generated)

        try:
            from web_app import db
            from web_app.models import BrowserRecording

            recording = BrowserRecording.query.filter_by(task_name=self.task_name).first()
            if recording:
                recording.steps_json = steps_json
                recording.generated_script = generated
                recording.polished_script = polished
                recording.is_tested = False
                recording.updated_at = datetime.utcnow()
            else:
                recording = BrowserRecording(
                    task_name=self.task_name,
                    steps_json=steps_json,
                    generated_script=generated,
                    polished_script=polished,
                )
                db.session.add(recording)
            db.session.commit()
            logger.info(f"[Discovery] Saved recording '{self.task_name}'")
            return recording
        except Exception as e:
            logger.error(f"[Discovery] Failed to save recording: {e}")
            # Save to file as fallback
            fallback_path = self.debug_dir / "recording.json"
            fallback_path.write_text(json.dumps({
                "task_name": self.task_name,
                "steps": self.steps,
                "generated_script": generated,
                "polished_script": polished,
            }, indent=2))
            logger.info(f"[Discovery] Saved to file: {fallback_path}")
            return None

    def _generate_script(self, steps_json: str) -> str:
        """Generate a Playwright Python script from recorded steps."""
        if not self.client:
            return self._generate_script_basic()

        prompt = f"""You are a Playwright Python expert. Convert these recorded browser automation steps into a clean, production-ready Playwright script.

Steps JSON:
{steps_json}

Requirements:
1. Use Playwright sync API
2. Add proper waits after each action (time.sleep or wait_for)
3. Add descriptive comments for each step
4. Wrap in a function called `run_{self.task_name.replace('-', '_')}(page)`
5. Add error handling with try/except for each major step
6. Use the most robust selectors (prefer role/text over CSS)
7. Return True on success, False on failure

Reply with ONLY the Python code, no markdown fences."""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.error(f"[Discovery] Script generation failed: {e}")
            return self._generate_script_basic()

    def _generate_script_basic(self) -> str:
        """Basic script generation without LLM."""
        lines = [
            "import time",
            "from playwright.sync_api import Page",
            "",
            f"def run_{self.task_name.replace('-', '_')}(page: Page) -> bool:",
            f'    """Auto-generated from recorded steps for: {self.task_name}"""',
            "    try:",
        ]
        for step in self.steps:
            action = step.get("action", {})
            a = action.get("action")
            if a == "click":
                lines.append(f"        # {step['goal']}")
                lines.append(f"        page.locator({action['selector']!r}).first.click(timeout=5000)")
                lines.append("        time.sleep(0.8)")
            elif a == "type":
                lines.append(f"        # {step['goal']}")
                lines.append(f"        page.locator({action['selector']!r}).first.fill({action.get('value', '')!r})")
            elif a == "press":
                lines.append(f"        page.keyboard.press({action.get('key', 'Enter')!r})")
            elif a == "navigate":
                lines.append(f"        page.goto({action['url']!r}, wait_until='domcontentloaded')")
                lines.append("        time.sleep(1)")
        lines += [
            "        return True",
            "    except Exception as e:",
            "        print(f'Script error: {e}')",
            "        return False",
        ]
        return "\n".join(lines)

    def _polish_script(self, script: str) -> str:
        """Polish the generated script with Claude for robustness."""
        if not self.client or not script:
            return script

        prompt = f"""Review and improve this Playwright automation script for robustness:

```python
{script}
```

Improvements to make:
1. Add fallback selectors where possible (try multiple locators)
2. Ensure waits are correct (wait_for_load_state, wait_for_selector where needed)
3. Add more descriptive error messages in except blocks
4. Make sure the function handles overlays/modals that might appear
5. Keep the same function signature and return value

Reply with ONLY the improved Python code, no markdown fences."""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2500,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.warning(f"[Discovery] Script polishing failed: {e}")
            return script


def get_or_record(page, task_name: str, goals: list[str]) -> str | None:
    """
    Get the best available script for a task, or run discovery to create one.

    Priority:
    1. polished_script (if is_tested=True)
    2. generated_script
    3. Run discovery with Claude, then return polished_script

    Returns the script code string, or None if all options fail.
    """
    try:
        from web_app.models import BrowserRecording
        recording = BrowserRecording.query.filter_by(task_name=task_name).first()

        if recording:
            if recording.is_tested and recording.polished_script:
                return recording.polished_script
            if recording.generated_script:
                return recording.generated_script
    except Exception:
        pass  # No DB context available

    # Run discovery
    logger.info(f"[Discovery] No recording for '{task_name}', starting discovery")
    discovery = BrowserDiscovery(page, task_name)
    discovery.run_flow(goals)
    recording = discovery.save_recording()

    if recording:
        return recording.polished_script or recording.generated_script
    return None
