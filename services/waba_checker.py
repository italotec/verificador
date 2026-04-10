"""
Browser-based WABA status checker.

Opens an AdsPower profile via Playwright and checks:
1. Central de Segurança — "Verificada" status
2. WhatsApp Manager — sending limit tier
3. Business Support — restricted/disabled detection

Reuses detection patterns from Multi Agents Diagnóstico.
"""

import logging
import os
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class WabaChecker:
    """Checks WABA status via browser automation."""

    def __init__(self):
        import config as app_config
        from services.adspower import AdsPowerClient

        self.ads = AdsPowerClient(app_config.ADSPOWER_BASE)
        self.screenshots_dir = Path(app_config.BASE_DIR) / "web_app" / "static" / "screenshots" / "checks"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def check(self, waba) -> dict:
        """
        Run all checks on a WabaRecord.
        Returns dict with check results.
        """
        from web_app import db
        from services.status_manager import StatusManager

        profile_id = waba.profile_id
        if not profile_id:
            raise ValueError("WabaRecord has no profile_id")

        business_id = waba.business_id
        if not business_id:
            raise ValueError("WabaRecord has no business_id for checking")

        # Open browser
        browser_data = self.ads.open_browser(profile_id)
        ws_endpoint = browser_data.get("ws", {}).get("puppeteer", "")
        if not ws_endpoint:
            raise RuntimeError(f"Failed to get WebSocket endpoint for profile {profile_id}")

        result = {
            "profile_id": profile_id,
            "verified": False,
            "restricted": False,
            "disabled": False,
            "messaging_limit": None,
            "screenshots": {},
        }

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(ws_endpoint)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()

                # Check 1: Central de Segurança
                try:
                    verified = self._check_security_center(page, business_id, profile_id)
                    result["verified"] = verified
                except Exception as e:
                    logger.error(f"Security center check failed for {profile_id}: {e}")

                # Check 2: Restriction/Disabled detection
                try:
                    restriction_data = self._check_restrictions(page, business_id, profile_id)
                    result["restricted"] = restriction_data.get("restricted", False)
                    result["disabled"] = restriction_data.get("disabled", False)
                except Exception as e:
                    logger.error(f"Restriction check failed for {profile_id}: {e}")

                # Check 3: Sending limit
                try:
                    limit = self._check_sending_limit(page, business_id, profile_id)
                    result["messaging_limit"] = limit
                except Exception as e:
                    logger.error(f"Limit check failed for {profile_id}: {e}")

                page.close()

        finally:
            try:
                self.ads.close_browser(profile_id)
            except Exception:
                pass

        # Apply results to WabaRecord
        self._apply_results(waba, result)

        return result

    def _take_screenshot(self, page, name: str, profile_id: str) -> str:
        """Take a screenshot and return the path."""
        filename = f"{profile_id}_{name}_{int(time.time())}.png"
        filepath = self.screenshots_dir / filename
        try:
            page.screenshot(path=str(filepath), full_page=True)
        except Exception:
            page.screenshot(path=str(filepath))
        return str(filepath)

    def _check_security_center(self, page, business_id: str, profile_id: str) -> bool:
        """
        Navigate to central de segurança and check for "Verificada".
        """
        url = f"https://business.facebook.com/settings/security-center?business_id={business_id}"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        self._take_screenshot(page, "security_center", profile_id)

        # Look for "Verificada" text on the page
        body_text = page.inner_text("body")
        verified = "Verificada" in body_text or "Verified" in body_text

        return verified

    def _check_restrictions(self, page, business_id: str, profile_id: str) -> dict:
        """
        Check for restricted/disabled status.
        Reuses patterns from Multi Agents Diagnóstico detector.
        """
        result = {"restricted": False, "disabled": False, "review_requested": False}

        # Check business support home
        url = f"https://business.facebook.com/business-support-home/?business_id={business_id}"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        self._take_screenshot(page, "business_support", profile_id)

        body_text = page.inner_text("body")

        # Detection patterns (from Multi Agents Diagnóstico)
        if "Conta restrita" in body_text or "Account restricted" in body_text:
            result["restricted"] = True
        if "Business remains disabled" in body_text or "permanentemente desativada" in body_text:
            result["disabled"] = True
        if "Análise solicitada" in body_text or "Review requested" in body_text:
            result["review_requested"] = True

        # Check for "Pedir análise" button (indicates disabled but recoverable)
        try:
            pedir_btn = page.locator("text=Pedir análise")
            if pedir_btn.count() > 0 and pedir_btn.first.is_enabled():
                result["disabled"] = True
        except Exception:
            pass

        return result

    def _check_sending_limit(self, page, business_id: str, profile_id: str) -> str | None:
        """
        Check the WhatsApp sending limit tier.
        Navigates to WhatsApp Manager and scrapes the limit information.
        """
        url = f"https://business.facebook.com/latest/whatsapp_manager/phone_numbers?business_id={business_id}"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        self._take_screenshot(page, "whatsapp_limits", profile_id)

        body_text = page.inner_text("body")

        # Try to detect limit tier from the page text
        limit_mapping = {
            "1K": "TIER_1K",
            "1.000": "TIER_1K",
            "10K": "TIER_10K",
            "10.000": "TIER_10K",
            "100K": "TIER_100K",
            "100.000": "TIER_100K",
            "Ilimitado": "TIER_UNLIMITED",
            "Unlimited": "TIER_UNLIMITED",
        }

        # Check for higher tiers first (more specific)
        for text, tier in limit_mapping.items():
            if text in body_text:
                return tier

        # Default: check for 250 indicators
        if "250" in body_text:
            return "TIER_250"

        return None

    def _apply_results(self, waba, result: dict):
        """Apply check results to WabaRecord via StatusManager."""
        from web_app import db
        from services.status_manager import StatusManager

        waba.last_limit_check = datetime.utcnow()

        # Handle restriction/disabled
        if result["disabled"]:
            StatusManager.detect_restriction(waba, disabled=True)
            return
        if result["restricted"]:
            StatusManager.detect_restriction(waba, restricted=True)
            return

        # Handle verification status
        if result["verified"] and waba.status == "em_revisao":
            StatusManager.transition(waba, "monitorando_limite", reason="Verificação confirmada na Central de Segurança")
            return
        if result["verified"] and waba.status == "nao_verificou":
            StatusManager.transition(waba, "monitorando_limite", reason="Verificação confirmada (atrasada)")
            return

        # Handle limit evaluation
        if result["messaging_limit"] and waba.status == "monitorando_limite":
            StatusManager.evaluate_limit(waba, result["messaging_limit"])
            return

        db.session.commit()
