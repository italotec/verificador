"""
Facebook Business Verification automation bot.

Connects to an already-open AdsPower browser via CDP, then drives the full
verification wizard:
  1. Login  (email/password or cookie injection)
  2. Ensure PT-BR language
  3. Create Business Portfolio
  4. Fill company details (address, phone, website)
  5. Add domain + inject meta-tag + verify domain
  6. Run Business Verification wizard
     - Upload CNPJ PDF cartão
     - Prefer domain verification; fall back to SMS OTP
     - Retry with new phone number + refreshed PDF when SMS does not arrive
"""
import base64
import json
import os
import random
import re
import time
import traceback
import pyotp
import requests
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, BrowserContext

import config
from services.gerador_client import GeradorClient
from services.sms24h import SMS24HService


# ── helpers ───────────────────────────────────────────────────────────────────

def _wait(seconds: float = 1.0):
    time.sleep(seconds)


def _clear_fill(locator, value: str):
    """
    Wait for browser/AdsPower autofill to settle, then clear the field and type.

    Sequence:
      1. Wait 0.6 s — lets any pending autofill finish injecting its text
      2. Disable autocomplete on the element — prevents re-fill after we type
      3. Triple-click + Delete — selects and removes all existing content
      4. Short pause
      5. fill() — write our value
    """
    _wait(0.6)
    try:
        locator.evaluate("el => { el.setAttribute('autocomplete', 'off'); "
                         "el.setAttribute('autocomplete', 'new-password'); }")
    except Exception:
        pass
    try:
        locator.triple_click()
        locator.press("Delete")
        _wait(0.3)
    except Exception:
        pass
    locator.fill(value)


def _try_click(page: Page, selector: str, timeout: int = 2000) -> bool:
    """Click *selector* if visible. Returns True on success."""
    try:
        loc = page.locator(selector).first
        if loc.is_visible(timeout=timeout):
            loc.click()
            return True
    except Exception:
        pass
    return False


def _dismiss_overlays(page: Page):
    """Dismiss the most common Facebook overlay dialogs."""
    for label in ("Fechar", "Descartar", "Não agora", "Pular", "Fechar ​",
                  "Permitir", "Salvar"):
        for _ in range(2):
            try:
                btn = page.get_by_label(label).first
                if btn.is_visible(timeout=800):
                    btn.click()
                    _wait(0.4)
            except Exception:
                pass
    for text in ("Fechar", "Not now", "Aceito", "Concluir",
                 "Permitir", "Salvar", "Salvar endereço",
                 "Allow", "Save"):
        try:
            btn = page.get_by_role("button", name=text, exact=True)
            if btn.is_visible(timeout=800):
                btn.click()
                _wait(0.4)
        except Exception:
            pass


def _click_with_retry(page: Page, locator_fn, timeout: int = 5000, retries: int = 1):
    """
    Try to click an element. If it fails, dismiss overlays and retry.
    locator_fn: callable returning a Playwright Locator.
    """
    for attempt in range(retries + 1):
        try:
            el = locator_fn()
            el.click(timeout=timeout)
            return
        except Exception:
            if attempt < retries:
                _dismiss_overlays(page)
                _wait(0.5)
            else:
                raise


# ── main class ────────────────────────────────────────────────────────────────

class FacebookBot:
    def __init__(
        self,
        ws_endpoint: str,
        run_data: dict,
        gerador: GeradorClient,
        sms: SMS24HService,
        email_mode: str = "own",          # "own" | "temp"
        sms_timeout: int = config.SMS_WAIT_TIMEOUT,
        sms_max_attempts: int = config.SMS_MAX_ATTEMPTS,
        # Step-tracking (optional — only needed when resuming)
        adspower_client=None,             # AdsPowerClient instance
        profile_user_id: str = "",        # AdsPower user_id of the running profile
        profile_remark: str = "",         # Current full remark string
        gerador_data: dict | None = None, # Parsed GERADOR JSON from remark (step flags)
    ):
        self.ws = ws_endpoint
        self.run = run_data
        self.gerador = gerador
        self.sms = sms
        self.email_mode = email_mode
        self.sms_timeout = sms_timeout
        self.sms_max_attempts = sms_max_attempts
        self.business_id: str = ""
        self.domain: str = ""
        # Virtual SMS number bought during the contact-info wizard sub-step
        self._sms_activation_id: str | None = None
        self._sms_phone_fb: str | None = None    # 10-digit local format for wizard

        # Step tracking
        self._adspower = adspower_client
        self._profile_user_id = profile_user_id
        self._profile_remark = profile_remark
        self._gerador_data: dict = dict(gerador_data) if gerador_data else {}

        # Debug settings (read from config, can be overridden by env)
        self._debug = config.DEBUG
        self._debug_screenshots = config.DEBUG_SCREENSHOTS
        self._debug_trace = config.DEBUG_TRACE
        self._debug_save_html = config.DEBUG_SAVE_HTML
        self._debug_dir = Path(config.DEBUG_DIR)
        self._step = 0  # monotonic screenshot counter within a session

    # ── debug helpers ─────────────────────────────────────────────────────────

    def _purge_old_debug_files(self, keep_mb: int = 200):
        """
        Delete oldest debug files (PNG, HTML, ZIP) when the debug folder exceeds
        *keep_mb* MB, keeping disk space available.  Runs at most once per session.
        """
        if getattr(self, "_debug_purged", False):
            return
        self._debug_purged = True
        try:
            files = sorted(
                self._debug_dir.glob("*"),
                key=lambda p: p.stat().st_mtime,
            )
            total = sum(f.stat().st_size for f in files if f.is_file())
            limit = keep_mb * 1024 * 1024
            deleted = 0
            for f in files:
                if total <= limit:
                    break
                if f.is_file():
                    sz = f.stat().st_size
                    f.unlink(missing_ok=True)
                    total -= sz
                    deleted += 1
            if deleted:
                print(f"[DEBUG] Purged {deleted} old debug file(s) to stay under {keep_mb} MB")
        except Exception as e:
            print(f"[DEBUG] Purge failed: {e}")

    def _shot(self, page: Page, label: str):
        """
        Save a full-page PNG screenshot when DEBUG_SCREENSHOTS is enabled.
        Files are named: debug/<run_id>_<HHMMSS>_<step>_<label>.png
        """
        if not self._debug_screenshots:
            return
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        self._purge_old_debug_files()
        self._step += 1
        run_id = self.run.get("run_id", "x")
        ts = datetime.now().strftime("%H%M%S")
        name = f"{run_id}_{ts}_{self._step:02d}_{label}.png"
        try:
            page.screenshot(path=str(self._debug_dir / name), full_page=True)
            print(f"[DEBUG] Screenshot → debug/{name}")
        except Exception as e:
            print(f"[DEBUG] Screenshot failed ({label}): {e}")

    def _save_html(self, page: Page, label: str):
        """
        Save the current page HTML when DEBUG_SAVE_HTML is enabled.
        Useful for inspecting selector mismatches offline.
        """
        if not self._debug_save_html:
            return
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        run_id = self.run.get("run_id", "x")
        ts = datetime.now().strftime("%H%M%S")
        name = f"{run_id}_{ts}_{label}.html"
        try:
            with open(self._debug_dir / name, "w", encoding="utf-8") as f:
                f.write(page.content())
            print(f"[DEBUG] HTML saved → debug/{name}")
        except Exception as e:
            print(f"[DEBUG] HTML save failed ({label}): {e}")

    def _mark_step_done(self, step: str, value=True):
        """
        Persist a step-completion flag in the AdsPower profile remark.

        Updates self._gerador_data in-memory and rewrites the ---GERADOR--- block
        of the remark via the AdsPower API so that the next run can skip this step.
        """
        self._gerador_data[step] = value
        if not self._adspower or not self._profile_user_id:
            return
        try:
            marker = config.GERADOR_REMARK_MARKER
            if marker in self._profile_remark:
                pre, _, _ = self._profile_remark.partition(marker)
            else:
                pre = self._profile_remark.rstrip() + "\n\n"
            new_remark = f"{pre}{marker}\n{json.dumps(self._gerador_data)}"
            self._adspower.update_profile(self._profile_user_id, remark=new_remark)
            self._profile_remark = new_remark
            print(f"[STEP] ✓ '{step}' saved to profile remark")
        except Exception as e:
            print(f"[STEP] Could not update remark: {e}")

    # ── automation engine integration ─────────────────────────────────────────

    def make_replay_engine(self, task_name: str, llm_fn, **kwargs):
        """
        Create a ReplayEngine for *task_name* pre-configured with project paths.

        The engine will look for / save scripts under:
            Disparos/memory/tasks/<task_name>.json  (recorded steps)
            Disparos/memory/tasks/<task_name>.py    (generated script)

        Example — wrap a new automation flow:

            engine = self.make_replay_engine(
                "fb_create_portfolio",
                llm_fn=lambda p: self._my_new_portfolio_flow(p),
            )
            success = engine.run(page)   # pass raw Playwright page

        On the first run: llm_fn is called with a TrackedPage.  If you confirm
        success, the steps are saved and a deterministic script is generated.
        On subsequent runs: the script is replayed directly (faster & reliable).
        If replay fails, llm_fn is called again automatically.
        """
        import sys
        _repo = str(config.REPO_DIR)
        if _repo not in sys.path:
            sys.path.insert(0, _repo)
        from automation_engine import ReplayEngine
        return ReplayEngine(
            task_name=task_name,
            tasks_dir=config.TASKS_DIR,
            llm_fn=llm_fn,
            auto_confirm=config.REPLAY_AUTO_CONFIRM,
            max_retries=config.REPLAY_MAX_RETRIES,
            **kwargs,
        )

    # ── entry point ───────────────────────────────────────────────────────────

    def run_verification(
        self,
        username: str,
        password: str,
        fakey: str = "",
        cookies: str = "",
        business_id: str = "",
    ) -> bool:
        """
        Full verification flow. Returns True when the wizard reaches
        "Em análise" or "Agradecemos o envio".

        Pass business_id to resume from company-details onward, skipping
        BM creation (useful when the portfolio was already created in a
        previous run).
        """
        # Store credentials so any stage can re-authenticate if needed
        self._username = username
        self._password = password
        self._fakey = fakey

        # Pre-set business_id if resuming a previous run
        if business_id:
            self.business_id = business_id
            print(f"[BOT] Resuming with existing business_id: {business_id}")

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.ws)
            ctx: BrowserContext = browser.contexts[0] if browser.contexts else browser.new_context()
            page: Page = ctx.new_page()

            # Start Playwright trace if requested (captures network, console, DOM)
            if self._debug_trace:
                self._debug_dir.mkdir(parents=True, exist_ok=True)
                ctx.tracing.start(screenshots=True, snapshots=True, sources=True)

            try:
                self._shot(page, "start")

                if not self._login(page, ctx, username, password, fakey, cookies):
                    self._shot(page, "login_fail")
                    self._save_html(page, "login_fail")
                    print("[BOT] Login failed — aborting")
                    return False
                self._shot(page, "login_ok")

                self._ensure_portuguese(page)

                if not self.business_id:
                    self.business_id = self._create_business_portfolio(page, ctx)
                    if not self.business_id:
                        self._shot(page, "portfolio_fail")
                        self._save_html(page, "portfolio_fail")
                        print("[BOT] Could not create Business Portfolio")
                        return False
                    self._shot(page, "portfolio_ok")
                    self._mark_step_done("business_id", self.business_id)

                if not self._gerador_data.get("business_info_done"):
                    self._set_company_details(page)
                    self._shot(page, "company_details")
                    self._mark_step_done("business_info_done")
                else:
                    print("[BOT] Skipping company details (already done)")

                if not self._gerador_data.get("domain_done"):
                    meta_tag = self._add_domain(page)
                    if meta_tag:
                        print(f"[BOT] Injecting meta tag: {meta_tag[:60]}…")
                        self.gerador.inject_meta_tag(self.run["run_id"], meta_tag)
                        _wait(30)  # give DNS / CloudPanel time to propagate
                        verified = self._verify_domain(page)
                        self._shot(page, "domain_verified")
                        if verified:
                            self._mark_step_done("domain_done")
                        else:
                            print("[BOT] Domain verification failed — not marking as done")
                    else:
                        self._shot(page, "domain_no_metatag")
                        # No meta tag returned — domain may already exist and be verified
                        # from a previous partial run where the remark wasn't saved.
                        # Check the domains settings page for a "Verificado" badge.
                        print("[BOT] No meta tag — checking if domain already verified")
                        for verified_text in ("Verificado", "Verified"):
                            try:
                                if page.get_by_text(verified_text, exact=True).is_visible(timeout=3_000):
                                    print(f"[BOT] Domain already verified ('{verified_text}') — marking done")
                                    self._mark_step_done("domain_done")
                                    break
                            except Exception:
                                pass
                else:
                    print("[BOT] Skipping domain (already done)")

                if not self._gerador_data.get("waba_done"):
                    if self._create_waba(page):
                        self._shot(page, "waba_done")
                        self._mark_step_done("waba_done")
                    else:
                        self._shot(page, "waba_fail")
                        print("[BOT] WABA creation failed — continuing anyway")
                else:
                    print("[BOT] Skipping WABA (already done)")

                result = False
                for verify_attempt in range(3):
                    try:
                        result = self._run_business_verification(page)
                    except Exception as ve:
                        print(f"[BOT] Verification attempt {verify_attempt+1} crashed: {ve}")
                        if self._debug:
                            traceback.print_exc()
                        result = False
                    if result:
                        break
                    if verify_attempt < 2:
                        print(f"[BOT] Verification failed — reloading and retrying ({verify_attempt+2}/3)")
                        self._sms_activation_id = None
                        self._sms_phone_fb = None
                        _wait(5)
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=20_000)
                            _wait(3)
                        except Exception:
                            pass

                self._shot(page, "verification_done" if result else "verification_fail")
                return result

            except Exception as e:
                print(f"[BOT] Unexpected error: {e}")
                self._shot(page, "crash")
                self._save_html(page, "crash")
                if self._debug:
                    traceback.print_exc()
                return False
            finally:
                if self._debug_trace:
                    try:
                        run_id = self.run.get("run_id", "x")
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        trace_path = str(self._debug_dir / f"{run_id}_{ts}_trace.zip")
                        ctx.tracing.stop(path=trace_path)
                        print(f"[DEBUG] Trace saved → {trace_path}")
                    except Exception as te:
                        print(f"[DEBUG] Trace save failed: {te}")
                try:
                    browser.close()
                except Exception:
                    pass

    # ── stage 1: login ────────────────────────────────────────────────────────

    def _is_logged_in(self, page: Page) -> bool:
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
            _wait(3)
            if "/login" in page.url:
                return False
            # "Amigos" nav button — try multiple selectors across FB layouts
            for sel in (
                '[aria-label="Amigos"]',
                'a[href*="/friends"]',
                '[aria-label="Friends"]',
            ):
                try:
                    if page.locator(sel).first.is_visible(timeout=2_000):
                        print(f"[LOGIN] Detected logged-in session ({sel})")
                        return True
                except Exception:
                    pass
            # Fallback: login form absent → consider logged in
            try:
                if page.locator('input[name="email"]').is_visible(timeout=2_000):
                    return False  # login form visible → not logged in
            except Exception:
                pass
            print("[LOGIN] Detected logged-in session (no login form)")
            return True
        except Exception:
            return False

    def _inject_cookies(self, ctx: BrowserContext, cookies_str: str):
        """Parse 'key=val;key2=val2' cookies and add them to the context."""
        cookie_list = []
        for part in cookies_str.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookie_list.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".facebook.com",
                "path": "/",
            })
        if cookie_list:
            ctx.add_cookies(cookie_list)

    def _handle_2fa(self, page: Page, fakey: str):
        otp = pyotp.TOTP(fakey).now()
        for selector in [
            'input[name="approvals_code"]',
            'input[autocomplete="one-time-code"]',
            'input[type="text"][maxlength="6"]',
        ]:
            try:
                field = page.locator(selector).first
                if field.is_visible(timeout=2_000):
                    _clear_fill(field, otp)
                    page.get_by_role("button", name="Continuar").click()
                    _wait(2)
                    return
            except Exception:
                pass

    def _do_password_login(self, page: Page, username: str, password: str, fakey: str) -> bool:
        """
        Drive the Facebook login form to the email+password stage and submit.

        Pre-login screen variants handled:
          A) One-tap "Continuar" — click it to open the full email+password form.
          B) Account picker "Usar outro perfil" — click it first.
          C) Full form already visible — proceed directly.

        After reaching the form:
          - If AdsPower pre-filled the credentials, click "Entrar" immediately.
          - Otherwise fill manually and click "Entrar".
        """
        print(f"[LOGIN] Attempting password login for {username}")
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
            _wait(2)  # give AdsPower autofill time to inject stored credentials

            # ── Step 1: get past any pre-login screens ────────────────────────

            # A) One-tap "Continuar" — just opens the full form, don't submit here
            try:
                if page.locator('button:has-text("Continuar")').first.is_visible(timeout=2_000):
                    print("[LOGIN] One-tap screen — clicking Continuar to open form")
                    page.locator('button:has-text("Continuar")').first.click()
                    _wait(2)  # let form render and AdsPower autofill credentials
            except Exception:
                pass

            # B) Account picker — click "Usar outro perfil" to reach the form
            try:
                if page.locator('button:has-text("Usar outro perfil")').first.is_visible(timeout=1_000):
                    page.locator('button:has-text("Usar outro perfil")').first.click()
                    _wait(2)
            except Exception:
                pass

            # ── Step 2: wait for email+password form ──────────────────────────
            email_field = page.locator('input[name="email"]')
            email_field.wait_for(state="visible", timeout=15_000)
            _wait(1)  # final pause for AdsPower autofill to settle

            # ── Step 3: fill only if empty ────────────────────────────────────
            if email_field.input_value():
                print("[LOGIN] Form pre-filled by AdsPower — clicking Entrar")
            else:
                print("[LOGIN] Form empty — filling credentials manually")
                _clear_fill(email_field, username)
                _clear_fill(page.locator('input[name="pass"]'), password)

            # ── Step 4: click "Entrar" ────────────────────────────────────────
            page.get_by_role("button", name="Entrar").click()
            page.wait_for_load_state("domcontentloaded", timeout=60_000)

        except Exception as e:
            print(f"[LOGIN] Form error: {e}")
            self._shot(page, "login_form_fail")
            self._save_html(page, "login_form_fail")
            return False

        _wait(2)
        if fakey:
            self._handle_2fa(page, fakey)
        _dismiss_overlays(page)
        logged = self._is_logged_in(page)
        print(f"[LOGIN] {'Logged in' if logged else 'Failed'} via email/password")
        return logged

    def _login(
        self,
        page: Page,
        ctx: BrowserContext,
        username: str,
        password: str,
        fakey: str,
        cookies: str,
    ) -> bool:
        # ── always check existing session first (AdsPower may already be logged in)
        if self._is_logged_in(page):
            print("[LOGIN] Already logged in via existing browser session")
            return True

        # ── try injecting cookies ─────────────────────────────────────────────
        if cookies:
            print("[LOGIN] Injecting cookies…")
            self._inject_cookies(ctx, cookies)
            if self._is_logged_in(page):
                print("[LOGIN] Logged in via cookies")
                return True
            print("[LOGIN] Cookies did not authenticate — falling back to email/password")

        return self._do_password_login(page, username, password, fakey)

    def _relogin_with_password(self, page: Page) -> bool:
        """
        Force password login using stored credentials.
        Called when a later stage detects the session is no longer authenticated.
        """
        print(f"[LOGIN] Re-authenticating for {self._username}")
        return self._do_password_login(page, self._username, self._password, self._fakey)

    # ── stage 2: language ─────────────────────────────────────────────────────

    def _ensure_portuguese(self, page: Page):
        """Switch to PT-BR if the UI isn't already in Portuguese."""
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
            if "Entrar" in page.content() or "Criar nova conta" in page.content():
                return  # already PT-BR

            # Navigate through Spanish settings (most common alternative)
            page.get_by_role("button", name="Tu perfil", exact=True).click(timeout=5_000)
            page.get_by_role("button", name="Configuración y privacidad").click(timeout=5_000)
            page.get_by_text("Idioma").click(timeout=5_000)
            page.get_by_role("button").filter(has_text="Idioma de Facebook").click(timeout=5_000)
            page.get_by_text("Portugués (Brasil)").click(timeout=5_000)
            page.wait_for_load_state("domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"[LANG] Could not change language (may already be PT-BR): {e}")

    # ── stage 3: create business portfolio ───────────────────────────────────

    def _scrape_user_name(self, page: Page) -> tuple[str, str]:
        try:
            page.goto("https://www.facebook.com/me", wait_until="domcontentloaded", timeout=60_000)
            heading = page.get_by_role("heading").first.inner_text(timeout=5_000).strip()
            parts = heading.split()
            first = parts[0]
            last = " ".join(parts[1:]) if len(parts) > 1 else parts[0]
            return first, last
        except Exception as e:
            print(f"[NAME] Could not scrape name: {e}")
            return "Admin", "User"

    def _get_temp_email(self, page: Page) -> str:
        try:
            tp = page.context.new_page()
            tp.goto(config.TUAMAE_URL, wait_until="domcontentloaded", timeout=60_000)
            tp.get_by_role("link", name="Copiar").click(timeout=5_000)
            _wait(0.5)
            # Try to read email from the page text
            text = tp.inner_text("body")
            match = re.search(r"[\w.+-]+@tuamaeaquelaursa\.com", text)
            email = match.group(0) if match else ""
            tp.close()
            return email
        except Exception as e:
            print(f"[EMAIL] Temp email failed: {e}")
            return ""

    def _create_business_portfolio(self, page: Page, ctx: BrowserContext) -> str:
        # Collect everything that requires navigation BEFORE opening the modal
        first, last = self._scrape_user_name(page)
        if self.email_mode == "temp":
            email = self._get_temp_email(page)
            if not email:
                print("[BM] Temp email failed — falling back to login email")
                email = self._username
        else:
            email = self._username  # use the FB login email as the commercial email

        # Navigate to adsmanager
        page.goto(
            "https://adsmanager.facebook.com/adsmanager/manage/accounts",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        _wait(3)
        # Dismiss overlays aggressively — policy modals can appear after page load
        for _ in range(4):
            _dismiss_overlays(page)
            _wait(0.5)

        # Click "Criar um portfólio empresarial" — loop: dismiss overlays → try selectors → LLM.
        # Each iteration re-dismisses any late-appearing modals (e.g. "Política de Não Discriminação")
        # before retrying the portfólio button search.  The LLM may return a modal-dismiss button
        # (e.g. "Concluir") rather than the portfólio button itself — in that case the loop
        # continues so the portfólio button is sought again on the next iteration.
        portf_clicked = False
        for outer_attempt in range(6):
            # Re-dismiss any overlay/modal that may have appeared or persisted
            _dismiss_overlays(page)
            _wait(0.5)

            # Try well-known button texts
            for btn_text in ("Criar um portfólio empresarial", "Crie um portfólio de negócios", "Criar portfólio", "Novo portfólio"):
                try:
                    btn = page.get_by_role("button", name=btn_text).first
                    if btn.is_visible(timeout=2_000):
                        btn.click()
                        portf_clicked = True
                        break
                except Exception:
                    pass
            if portf_clicked:
                break

            # Broader has_text search
            for has_text in ("portfólio empresarial", "portfólio de negócios", "portfólio", "novo portfólio"):
                try:
                    btn = page.locator("[role='button'], button").filter(has_text=has_text).first
                    if btn.is_visible(timeout=2_000):
                        btn.click()
                        portf_clicked = True
                        break
                except Exception:
                    pass
            if portf_clicked:
                break

            if outer_attempt == 0:
                # First miss: open the account-switcher dropdown (may reveal "Criar portfólio")
                print("[BM] 'Criar portfólio' not visible — opening account switcher")
                for sel in (
                    'div[aria-haspopup="listbox"]',
                    'button[aria-haspopup="listbox"]',
                    '[role="button"][aria-haspopup="listbox"]',
                ):
                    try:
                        loc = page.locator(sel).first
                        if loc.is_visible(timeout=2_000):
                            loc.click()
                            _wait(1)
                            break
                    except Exception:
                        pass
                continue  # re-check after opening switcher

            # LLM vision fallback — may return a modal-dismiss button or the portfólio button
            print(f"[BM] Portfolio button not found (attempt {outer_attempt}) — asking LLM")
            llm_text = self._llm_find_action(
                page,
                "click the button that creates a new business portfolio (portfólio empresarial). "
                "If a modal or overlay with a blocking button (e.g. 'Concluir', 'Fechar', 'Aceito') "
                "is visible, return that button's text first so we can dismiss it.",
            )
            if not llm_text:
                print("[BM] LLM could not identify a button — giving up")
                break
            print(f"[BM] LLM suggests clicking: '{llm_text}'")
            try:
                page.get_by_role("button", name=llm_text).first.click(timeout=5_000)
                _wait(1)
            except Exception:
                try:
                    page.get_by_text(llm_text, exact=False).first.click(timeout=3_000)
                    _wait(1)
                except Exception as e:
                    print(f"[BM] Could not click LLM-suggested '{llm_text}': {e}")
                    break

        if not portf_clicked:
            print("[BM] Could not find portfolio creation button — aborting")
            return ""
        _wait(1)

        # Business name — razao_social comes in ALL CAPS; FB rejects it, so title-case it
        biz_name = self.run["razao_social"].title()
        _clear_fill(page.get_by_placeholder("Jasper's Market"), biz_name)

        # First / last name (already scraped before navigating here)
        try:
            _clear_fill(page.get_by_label("Nome", exact=True), first)
            _clear_fill(page.get_by_label("Sobrenome"), last)
        except Exception:
            pass

        if email:
            try:
                _clear_fill(page.get_by_label("Email comercial"), email)
                _wait(1)  # let FB validate the field and enable the "Criar" button
            except Exception:
                pass

        # Try to click "Criar" — scope to dialog to avoid matching the "+ Criar"
        # button in the main FB toolbar (strict mode violation).
        criar_clicked = False
        for attempt in range(3):
            try:
                dialog = page.get_by_role("dialog")
                dialog.get_by_role("button", name="Criar", exact=True).click(timeout=6_000)
                criar_clicked = True
                break
            except Exception as e:
                if attempt < 2:
                    print(f"[BM] 'Criar' still disabled (attempt {attempt+1}) — {e}")
                    fix = self._llm_fix_form(page)
                    if fix:
                        print(f"[BM] LLM applied fix: {fix}")
                    _wait(1)

        if not criar_clicked:
            print("[BM] Could not click 'Criar' — form validation not resolved")
            return ""

        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        _wait(3)

        # Skip onboarding screens — keep clicking "Pular" until it disappears,
        # then click "Confirmar" if present
        for _ in range(6):
            try:
                btn = page.get_by_role("button", name="Pular", exact=True)
                if btn.is_visible(timeout=2_000):
                    btn.click()
                    _wait(1.5)
                    continue
            except Exception:
                pass
            try:
                btn = page.get_by_role("button", name="Confirmar")
                if btn.is_visible(timeout=1_000):
                    btn.click()
                    _wait(1.5)
            except Exception:
                pass
            break

        # Navigate to business home — FB redirects to a URL containing business_id
        # e.g. https://business.facebook.com/latest/business_home?business_id=XXXX
        _wait(5)
        page.goto("https://business.facebook.com/", wait_until="domcontentloaded", timeout=30_000)
        _wait(10)
        url = page.url
        m = re.search(r"business_id[=\/](\d+)", url)
        biz_id = m.group(1) if m else ""
        print(f"[BM] Portfolio created — business_id: {biz_id} (url: {url})")
        return biz_id

    # ── stage 4: company details ──────────────────────────────────────────────

    def _set_company_details(self, page: Page):
        if not self.business_id:
            return
        page.goto(
            f"https://business.facebook.com/latest/settings/business_info?business_id={self.business_id}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        _wait(2)
        _dismiss_overlays(page)

        # Click "Informações da empresa" in the left sidebar to ensure the right panel is open
        try:
            page.get_by_label("Informações da empresa").click(timeout=5_000)
            _wait(1)
        except Exception:
            pass

        # Open "Detalhes da empresa" edit panel
        _dismiss_overlays(page)
        try:
            _click_with_retry(
                page,
                lambda: page.locator("div").filter(
                    has_text=re.compile(r"^Detalhes da empresaEditar$")
                ).get_by_role("button"),
                timeout=5_000,
            )
        except Exception:
            _click_with_retry(
                page,
                lambda: page.locator("button").filter(has_text="Editar").first,
                timeout=5_000,
            )
        _wait(1)

        # Business name
        _clear_fill(page.get_by_placeholder("Insira o nome comercial"), self.run["razao_social"])

        # Country → Brasil
        # The combobox opens a separate overlay with a search input.
        # After clicking to open, type via keyboard into the focused search box.
        try:
            page.get_by_role("combobox", name="País").click()
            _wait(0.4)
            page.keyboard.type("Brasil")
            _wait(0.5)
            page.get_by_role("option", name="Brasil").click()
            _wait(0.5)
        except Exception:
            pass

        _clear_fill(page.get_by_label("Endereço", exact=True), self.run.get("logradouro", ""))
        _clear_fill(page.get_by_label("Cidade"), self.run.get("municipio", ""))

        try:
            _clear_fill(page.get_by_label("Estado/província/região"), self.run.get("estado_nome", ""))
            _wait(0.5)
        except Exception:
            pass

        try:
            cep = self.run.get("cep_digits", "")
            _clear_fill(page.get_by_label("CEP/código postal"), cep)
        except Exception:
            pass

        # Phone — Facebook wants digits with country code: e.g. 5571988608723
        tel = self.run.get("telefone_digits", "")
        if tel and not tel.startswith("55"):
            tel = "55" + tel
        try:
            _clear_fill(page.get_by_label("Telefone comercial"), tel)
        except Exception:
            pass

        # Website
        deploy = self.run.get("deploy_url", "")
        if deploy:
            try:
                _clear_fill(page.get_by_label("Site da empresa"), deploy)
            except Exception:
                pass

        _click_with_retry(
            page,
            lambda: page.get_by_role("button", name="Salvar"),
            timeout=5_000,
        )
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        _wait(2)
        _dismiss_overlays(page)  # handle "Salvar endereço" popup after save

    # ── stage 5: domain ───────────────────────────────────────────────────────

    def _add_domain(self, page: Page) -> str:
        """
        Navigate to owned-domains settings, add the company domain,
        and return the raw <meta> tag string for verification.

        Flow (matches FB Business Manager UI):
          1. Open /latest/settings/owned-domains/
          2. Click "Adicionar" → "Criar um domínio"
          3. Fill domain (no protocol/www)
          4. Click submit "Adicionar"
          5. Extract <meta name="facebook-domain-verification" …> tag
        """
        if not self.business_id:
            return ""

        # Domain without protocol or www
        deploy = self.run.get("deploy_url", "")
        self.domain = re.sub(r"^https?://(www\.)?", "", deploy).rstrip("/")
        if not self.domain:
            print("[DOMAIN] No deploy_url in run data — cannot add domain")
            return ""

        page.goto(
            f"https://business.facebook.com/latest/settings/domains/?business_id={self.business_id}",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        _wait(2)
        _dismiss_overlays(page)
        self._shot(page, "domain_01_page")

        # Click "Adicionar" to open the domain creation flow
        try:
            page.get_by_role("button", name="Adicionar").first.click(timeout=8_000)
        except Exception as e:
            print(f"[DOMAIN] Could not click Adicionar: {e}")
            return ""
        _wait(1)
        self._shot(page, "domain_02_after_adicionar")

        # Click "Criar um domínio" option inside the "O que você quer fazer?" modal
        try:
            page.get_by_text("Criar um domínio").click(timeout=5_000)
            _wait(1)
        except Exception:
            # The modal may have gone straight to the input — continue anyway
            pass
        self._shot(page, "domain_03_criar_dominio")

        # Fill in domain name
        filled = False
        try:
            domain_field = page.get_by_placeholder("exemplo.com ou exemplo.com.br")
            domain_field.wait_for(state="visible", timeout=6_000)
            _clear_fill(domain_field, self.domain)
            filled = True
        except Exception:
            pass

        if not filled:
            try:
                domain_field = page.locator('input[type="text"]').last
                domain_field.wait_for(state="visible", timeout=4_000)
                _clear_fill(domain_field, self.domain)
                filled = True
            except Exception:
                pass

        if not filled:
            # LLM fallback: let the model look at the screen and find/reveal the field
            filled = self._llm_fill_field(page, self.domain, "domain name (e.g. exemplo.com)")
            if not filled:
                print("[DOMAIN] Could not find domain input field — aborting")
                return ""

        _wait(0.5)
        self._shot(page, "domain_04_filled")

        # Click submit "Adicionar" — scoped to dialog to avoid wrong button
        try:
            dialog = page.locator('[role="dialog"]').last
            dialog.get_by_role("button", name="Adicionar").click(timeout=5_000)
        except Exception:
            try:
                page.get_by_role("button", name="Adicionar").last.click(timeout=5_000)
            except Exception as e:
                print(f"[DOMAIN] Could not click submit Adicionar: {e}")
                return ""
        _wait(3)
        self._shot(page, "domain_05_submitted")

        # Extract the <meta> verification tag shown after domain is added.
        # IMPORTANT: always extract ONLY the <meta> tag, never a larger text blob.
        _wait(1)  # let page settle after submit

        def _extract_meta(raw: str) -> str:
            """Pull just the <meta name="facebook-domain-verification" …> from raw text."""
            m = re.search(
                r'<meta\s+name="facebook-domain-verification"[^>]*/?>',
                raw,
            )
            if m:
                return m.group(0).strip()
            # Fallback: construct from content= value if visible in the text
            m2 = re.search(r'content="([a-z0-9]+)"', raw)
            if m2:
                return f'<meta name="facebook-domain-verification" content="{m2.group(1)}" />'
            return ""

        meta_text = ""

        # 1. Try textarea.input_value (rare FB pattern)
        try:
            ta = page.locator("textarea").filter(has_text="facebook-domain-verification").first
            if ta.count() > 0:
                raw = ta.input_value(timeout=3_000)
                meta_text = _extract_meta(raw)
        except Exception:
            pass

        # 2. Try code/pre/span elements — inner_text may return a big block, so regex it
        if not meta_text:
            for selector in (
                'code:has-text("facebook-domain-verification")',
                'pre:has-text("facebook-domain-verification")',
                '[class*="code"]:has-text("facebook-domain-verification")',
                'span:has-text("facebook-domain-verification")',
            ):
                try:
                    el = page.locator(selector).first
                    if el.is_visible(timeout=3_000):
                        raw = el.inner_text(timeout=3_000)
                        meta_text = _extract_meta(raw)
                        if meta_text:
                            break
                except Exception:
                    pass

        # 3. Body inner_text regex (handles plain-text display of the tag)
        if not meta_text:
            try:
                body_text = page.locator("body").inner_text(timeout=5_000)
                meta_text = _extract_meta(body_text)
            except Exception:
                pass

        # 4. Raw HTML source fallback
        if not meta_text:
            try:
                meta_text = _extract_meta(page.content())
            except Exception:
                pass

        if not meta_text:
            print("[DOMAIN] Meta tag not found on page")
        else:
            print(f"[DOMAIN] Meta tag: {meta_text}")

        return meta_text

    def _verify_domain(self, page: Page) -> bool:
        """
        Click 'Verificar domínio' and check that the domain badge becomes 'Verified'.
        Returns True only if verification succeeds; False otherwise.
        """
        try:
            page.get_by_role("button", name="Verificar domínio").click(timeout=8_000)
        except Exception as e:
            print(f"[DOMAIN] 'Verificar domínio' button not found: {e}")
            return False

        _wait(5)
        self._shot(page, "domain_06_after_verify")

        # Look for positive confirmation ("Verificado" in PT-BR, "Verified" in EN)
        # Use get_by_text with exact=True so "Verificado" does NOT match "Não verificado"
        for verified_text in ("Verificado", "Verified"):
            try:
                loc = page.get_by_text(verified_text, exact=True).first
                if loc.is_visible(timeout=5_000):
                    print(f"[DOMAIN] Domain verified! (badge: '{verified_text}')")
                    return True
            except Exception:
                pass

        # If "Não verificado" / "Not Verified" still visible, it failed
        for fail_text in ("Não verificado", "Not Verified"):
            try:
                loc = page.get_by_text(fail_text, exact=True).first
                if loc.is_visible(timeout=2_000):
                    print(f"[DOMAIN] Verification failed — badge still: '{fail_text}'")
                    return False
            except Exception:
                pass

        print("[DOMAIN] Could not determine verification status")
        return False

    # ── stage 6: WhatsApp Business Account ───────────────────────────────────

    def _create_waba(self, page: Page) -> bool:
        """
        Create a WhatsApp Business Account for the portfolio.
        Returns True on success, False if any required step fails.

        Steps:
          1. Navigate to WhatsApp account settings
          2. Click "Adicionar" (first button) → "Crie uma nova conta do WhatsApp Business"
          3. Select a category randomly (Serviços profissionais / Viagem e transporte)
          4. Click "Continuar"
          5. Close the resulting modal
        """
        if not self.business_id:
            return False
        page.goto(
            f"https://business.facebook.com/latest/settings/whatsapp_account?business_id={self.business_id}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        _wait(2)
        _dismiss_overlays(page)

        # Click first "Adicionar" button (there may be multiple on the page)
        try:
            page.get_by_role("button", name="Adicionar").first.click(timeout=5_000)
            _wait(1)
        except Exception as e:
            print(f"[WABA] 'Adicionar' not found: {e}")
            return False

        # Click "Crie uma nova conta do WhatsApp Business"
        try:
            page.get_by_text("Crie uma nova conta do WhatsApp Business").click(timeout=5_000)
            _wait(1)
        except Exception as e:
            print(f"[WABA] 'Crie nova conta' option not found: {e}")
            return False

        self._shot(page, "waba_category")

        category_done = False

        # Open the Categoria dropdown.
        # The modal has 2 comboboxes: name input (first) + Categoria (last).
        # Using .last avoids accidentally clicking the name field.
        dropdown_opened = False
        for opener_sel in (
            ("combobox", "last"),
            ("combobox", "nth1"),
        ):
            try:
                role, pos = opener_sel
                loc = page.get_by_role(role)
                elem = loc.last if pos == "last" else loc.nth(1)
                elem.click(timeout=3_000)
                _wait(0.6)
                dropdown_opened = True
                break
            except Exception:
                pass
        # Fallback: aria-haspopup or aria-expanded
        if not dropdown_opened:
            for fb_sel in ('[aria-haspopup="listbox"]', '[aria-expanded="false"]'):
                try:
                    page.locator(fb_sel).last.click(timeout=2_000)
                    _wait(0.6)
                    dropdown_opened = True
                    break
                except Exception:
                    pass
        if not dropdown_opened:
            print("[WABA] Could not open category dropdown")

        self._shot(page, "waba_category_open")

        # Pick any first visible option — the list is alphabetical and lazily rendered,
        # so specific-text searches often miss items that aren't in view yet.
        # Any valid category works; we just need one selected to enable "Continuar".
        for opt_sel in ("[role='option']", "[role='radio']", "li[tabindex]"):
            try:
                items = page.locator(opt_sel)
                if items.count() > 0 and items.first.is_visible(timeout=1_500):
                    items.first.click()
                    _wait(0.5)
                    category_done = True
                    print(f"[WABA] Category selected (first item via '{opt_sel}')")
                    break
            except Exception:
                pass

        # Keyboard fallback: ArrowDown highlights first item, Enter confirms
        if not category_done:
            try:
                page.keyboard.press("ArrowDown")
                _wait(0.3)
                page.keyboard.press("Enter")
                _wait(0.3)
                category_done = True
                print("[WABA] Category selected via keyboard (ArrowDown+Enter)")
            except Exception as e:
                print(f"[WABA] Category selection failed: {e}")

        # Click "Continuar"
        try:
            page.get_by_role("button", name="Continuar").click(timeout=5_000)
            _wait(2)
            self._shot(page, "waba_continuar")
        except Exception as e:
            print(f"[WABA] 'Continuar' not found: {e}")
            return False

        # Close the modal (try aria-label selectors, then Escape)
        for close_label in ("Fechar", "Close"):
            try:
                btn = page.get_by_role("button", name=close_label).first
                if btn.is_visible(timeout=2_000):
                    btn.click()
                    _wait(1)
                    print(f"[WABA] Modal closed ('{close_label}' button)")
                    return True
            except Exception:
                pass
        try:
            page.keyboard.press("Escape")
            _wait(1)
            print("[WABA] Modal closed (Escape)")
        except Exception:
            pass
        return True

    # ── stage 8: business verification wizard ────────────────────────────────

    def _run_business_verification(self, page: Page) -> bool:
        """
        Run the Facebook Business Verification wizard.

        Handles both a fresh start ('Iniciar verificação') and a resume
        ('Continuar' / 'Verificar').  After opening the wizard, uses
        keyword-based step detection (with LLM vision fallback) to jump
        to the correct place in the flow.
        """
        if not self.business_id:
            return False

        page.goto(
            f"https://business.facebook.com/settings/security/?business_id={self.business_id}",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        _wait(2)
        _dismiss_overlays(page)
        # Reload so the page reflects the latest verification state
        # (after WABA creation the entry button may not appear without a reload)
        page.reload(wait_until="domcontentloaded", timeout=20_000)
        _wait(2)
        _dismiss_overlays(page)
        self._shot(page, "verify_security_page")

        # ── Detect entry button ────────────────────────────────────────────
        entry_button = None
        for btn_name in ("Iniciar verificação", "Continuar", "Verificar"):
            try:
                btn = page.get_by_role("button", name=btn_name).first
                if btn.is_visible(timeout=3_000):
                    entry_button = btn_name
                    break
            except Exception:
                _dismiss_overlays(page)
                continue

        if not entry_button:
            print("[VERIFY] No entry button found — checking if already verified")
            try:
                body = page.inner_text("body").lower()
                if "em análise" in body or "verificado" in body:
                    print("[VERIFY] Already verified or in review — skipping")
                    return True
            except Exception:
                pass
            return False

        resume_mode = entry_button in ("Continuar", "Verificar")
        print(f"[VERIFY] Entry button: '{entry_button}' (resume={resume_mode})")

        page.get_by_role("button", name=entry_button).first.click(timeout=5_000)
        _wait(2)
        self._shot(page, "verify_wizard_opened")

        # ── Detect current step when resuming ──────────────────────────────
        if resume_mode:
            step = self._detect_wizard_step(page)
            print(f"[VERIFY] Detected step after '{entry_button}': {step}")
        else:
            step = "start"

        return self._wizard_run_from(page, step)

    # ── Wizard step detection ──────────────────────────────────────────────────

    def _detect_wizard_step(self, page: Page) -> str:
        """
        Identify the current wizard step from visible page text.
        Falls back to LLM vision analysis when keywords are ambiguous.

        Returns one of:
            start | entity_type | registration | cnpj_input | cnpj_list |
            add_company_data | document_upload | method_selection |
            phone_entry | otp_entry | complete | unknown
        """
        # Prefer the dialog text so we get modal content (FB renders wizards in dialogs)
        try:
            dialog = page.locator('[role="dialog"]').first
            text = dialog.inner_text(timeout=3_000) if dialog.is_visible(timeout=800) else page.inner_text("body")
        except Exception:
            try:
                text = page.inner_text("body")
            except Exception:
                return "unknown"

        t = text.lower()

        if any(k in t for k in ("agradecemos", "verificação foi concluída", "em análise")):
            return "complete"
        # Domain confirmation intermediate screen — has "Avançar" but is NOT complete
        if "domínio abaixo foi verificado" in t or "verifique com o domínio da empresa" in t:
            return "domain_confirmed"
        if any(k in t for k in ("código de verificação", "código que você recebeu", "inserir o código")):
            return "otp_entry"
        if any(k in t for k in ("sms foi enviado", "enviar sms", "enviar um sms")) or \
                ("número de telefone" in t and "código" in t):
            return "phone_entry"
        if any(k in t for k in ("verificação de domínio", "mensagem de texto (sms)", "ligação telefônica")):
            return "method_selection"
        # Must be before document_upload: the start/intro screen lists "Carregar documentos"
        # as a bullet point under "Ações necessárias", which would otherwise false-match.
        if "começar" in t and ("ações necessárias" in t or "conexão legítima" in t):
            return "start"
        if any(k in t for k in ("carregar documentos", "tipo de documento", "selecione um tipo")):
            return "document_upload"
        if "não está na lista" in t or "minha empresa não está" in t:
            return "cnpj_list"
        if "adicionar dados da empresa" in t:
            return "add_company_data"
        if "identificação fiscal" in t or "número de identificação fiscal" in t:
            return "cnpj_input"
        if "tem registro" in t or "registro fiscal" in t:
            return "registration"
        if "empresa individual" in t or "tipo de empresa" in t:
            return "entity_type"
        if "começar" in t:
            return "start"

        return self._detect_step_llm(page)

    def _detect_step_llm(self, page: Page) -> str:
        """
        LLM vision fallback: sends a screenshot to OpenAI GPT-4o-mini and
        asks which wizard step is visible.  Requires OPENAI_API_KEY in env.
        """
        openai_key = os.getenv("OPENAI_API_KEY") or config.OPENAI_API_KEY
        if not openai_key:
            print("[VERIFY] No OPENAI_API_KEY — LLM step detection unavailable")
            return "unknown"

        try:
            img_b64 = base64.b64encode(page.screenshot()).decode()
            payload = {
                "model": "gpt-4o-mini",
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You are analysing a Facebook Business Verification wizard "
                                "screenshot (in Portuguese). Identify the current step and "
                                "reply with ONLY one keyword from this list:\n"
                                "start, entity_type, registration, cnpj_input, cnpj_list, "
                                "add_company_data, document_upload, method_selection, "
                                "phone_entry, otp_entry, complete, unknown\n\n"
                                "Examples:\n"
                                "  document_upload  — file upload area visible\n"
                                "  method_selection — domain/SMS/call radio buttons visible\n"
                                "  entity_type      — 'Empresa individual' choice visible\n"
                                "  complete         — 'Processando seu envio', 'Agradecemos o envio', or 'Em análise' text visible\n"
                                "  start            — intro screen with 'Começar' button listing verification steps\n"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{img_b64}",
                                "detail": "low",
                            },
                        },
                    ],
                }],
                "max_tokens": 20,
            }
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip().lower().split()[0]
            valid = {
                "start", "entity_type", "registration", "cnpj_input", "cnpj_list",
                "add_company_data", "document_upload", "method_selection",
                "phone_entry", "otp_entry", "complete",
            }
            result = raw if raw in valid else "unknown"
            print(f"[VERIFY] LLM step detection → {result}")
            return result
        except Exception as e:
            print(f"[VERIFY] LLM step detection failed: {e}")
            return "unknown"

    def _llm_fix_form(self, page: Page) -> str:
        """
        Ask GPT-4o-mini to look at the current form screenshot and identify
        which required field is empty or invalid, then attempt to fill it.

        Returns a short description of the fix applied, or "" if nothing done.
        """
        openai_key = os.getenv("OPENAI_API_KEY") or config.OPENAI_API_KEY
        if not openai_key:
            return ""
        try:
            img_b64 = base64.b64encode(page.screenshot()).decode()
            payload = {
                "model": "gpt-4o-mini",
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "This is a Facebook Business Portfolio creation form in Portuguese. "
                                "The 'Criar' (Create) button is disabled. "
                                "Look at the form and identify which required field is empty or has an error. "
                                "Reply with a JSON object like: "
                                '{"field": "<label name in Portuguese>", "value": "<what to fill>"} '
                                "or {\"field\": null} if you cannot determine the issue. "
                                "Only reply with the JSON, nothing else."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "low"},
                        },
                    ],
                }],
                "max_tokens": 80,
            }
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            data = json.loads(raw)
            field = data.get("field")
            value = data.get("value", "")
            if not field or not value:
                return ""
            # Try to fill the identified field
            try:
                loc = page.get_by_label(field)
                if loc.is_visible(timeout=2_000):
                    _clear_fill(loc, value)
                    _wait(0.8)
                    return f"filled '{field}' with '{value}'"
            except Exception:
                pass
            # Fallback: search by placeholder
            try:
                loc = page.get_by_placeholder(field)
                if loc.is_visible(timeout=1_000):
                    _clear_fill(loc, value)
                    _wait(0.8)
                    return f"filled placeholder '{field}' with '{value}'"
            except Exception:
                pass
        except Exception as e:
            print(f"[BM] LLM form diagnosis failed: {e}")
        return ""

    def _llm_find_action(self, page: Page, goal: str) -> str:
        """
        Ask GPT-4o-mini to look at the current screenshot and return the exact
        visible text of the button/link that achieves *goal*.

        Returns the button text string, or "" if LLM can't determine it.
        Use this when selectors fail and the bot doesn't know what to click next.
        """
        openai_key = os.getenv("OPENAI_API_KEY") or config.OPENAI_API_KEY
        if not openai_key:
            return ""
        try:
            img_b64 = base64.b64encode(page.screenshot()).decode()
            payload = {
                "model": "gpt-4o-mini",
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"You are helping automate a Facebook Business Manager page in Portuguese.\n"
                                f"Goal: {goal}\n"
                                f"Look at the screenshot and return ONLY the exact visible text of the "
                                f"button or link I should click to achieve this goal. "
                                f"If there is a modal/dialog blocking the page, return the text of the "
                                f"button to dismiss it first. "
                                f"Reply with just the button text, nothing else. "
                                f"If you cannot determine it, reply with: UNKNOWN"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "low"},
                        },
                    ],
                }],
                "max_tokens": 30,
            }
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            result = r.json()["choices"][0]["message"]["content"].strip()
            if result.upper() == "UNKNOWN" or not result:
                return ""
            print(f"[LLM] find_action → '{result}'")
            return result
        except Exception as e:
            print(f"[LLM] find_action failed: {e}")
            return ""

    def _llm_fill_field(self, page: Page, value: str, goal: str) -> bool:
        """
        LLM-guided field fill for when Playwright locators fail.

        1. Screenshot → ask LLM for the exact placeholder/label text of the input.
        2. If NOT_VISIBLE → ask LLM what to click to reveal the input, click it, retry once.
        3. Fill using placeholder or label.

        Returns True if the field was successfully filled, False otherwise.
        """
        openai_key = os.getenv("OPENAI_API_KEY") or config.OPENAI_API_KEY
        if not openai_key:
            return False

        def _ask_identifier() -> str:
            try:
                img_b64 = base64.b64encode(page.screenshot()).decode()
                payload = {
                    "model": "gpt-4o-mini",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Look at this Facebook Business Manager page (in Portuguese).\n"
                                    f"I need to type the value '{value}' into the {goal} input field.\n"
                                    f"What is the EXACT placeholder text, label text, or nearby visible "
                                    f"text of the input I should fill?\n"
                                    f"Reply with ONLY that text, nothing else.\n"
                                    f"If no such input is visible on screen (e.g. a dialog has not "
                                    f"opened yet), reply with exactly: NOT_VISIBLE"
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{img_b64}",
                                    "detail": "low",
                                },
                            },
                        ],
                    }],
                    "max_tokens": 30,
                }
                r = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {openai_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=30,
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                print(f"[LLM] fill_field identifier failed: {e}")
                return ""

        def _try_fill(identifier: str) -> bool:
            for getter in [
                lambda: page.get_by_placeholder(identifier),
                lambda: page.get_by_label(identifier, exact=False),
            ]:
                try:
                    loc = getter()
                    if loc.first.is_visible(timeout=2_000):
                        _clear_fill(loc.first, value)
                        print(f"[LLM] fill_field: filled '{goal}' using '{identifier}'")
                        return True
                except Exception:
                    pass
            return False

        # First attempt
        identifier = _ask_identifier()
        print(f"[LLM] fill_field identifier → '{identifier}'")

        if identifier and identifier.upper() != "NOT_VISIBLE":
            if _try_fill(identifier):
                return True

        # Input not visible — ask LLM what to click to reveal it
        reveal_btn = self._llm_find_action(page, f"open or reveal the {goal} input field")
        if reveal_btn and reveal_btn.upper() != "UNKNOWN":
            try:
                page.get_by_text(reveal_btn, exact=False).first.click(timeout=4_000)
                print(f"[LLM] fill_field: clicked '{reveal_btn}' to reveal field")
                _wait(1.5)
            except Exception as e:
                print(f"[LLM] fill_field: could not click reveal button '{reveal_btn}': {e}")

        # Retry after revealing
        identifier = _ask_identifier()
        print(f"[LLM] fill_field retry identifier → '{identifier}'")
        if identifier and identifier.upper() != "NOT_VISIBLE":
            return _try_fill(identifier)

        return False

    # ── Wizard state machine ───────────────────────────────────────────────────

    def _wizard_run_from(self, page: Page, initial_step: str) -> bool:
        """
        Drive the wizard from *initial_step* to completion.

        After each step handler runs, the current step is re-detected so the
        flow adapts automatically (handles skipped steps, FB A/B tests, etc.).
        Stops when it reaches 'document_upload' and delegates to the upload +
        verification method pipeline.
        """
        PRE_UPLOAD = ["start", "entity_type", "registration", "cnpj_input", "cnpj_list", "add_company_data"]
        current = initial_step

        for iteration in range(18):  # safety cap (3 add_company_data sub-steps + buffer)
            self._shot(page, f"wiz_{iteration:02d}_{current}")
            print(f"[VERIFY] Wizard step [{iteration}]: {current}")

            if current == "document_upload":
                return self._wizard_upload_and_verify(page)

            if current == "method_selection":
                # Two distinct flows:
                # A) method_selection comes BEFORE doc upload → CNPJ not found in FB registry
                #    ("Como não encontramos um registro correspondente / Antes de carregar um documento")
                #    Select SMS → Avançar → upload doc → SMS loop
                # B) method_selection comes AFTER doc upload → handled by _wizard_upload_and_verify
                #    Here we are in flow A (detected before doc_upload step).
                return self._wizard_method_first(page)

            if current in ("phone_entry", "otp_entry"):
                return self._wizard_upload_and_verify(page)

            if current == "complete":
                # Double-check with real text — LLM can misidentify the intro
                # screen ("Começar" button) as complete.
                return self._complete_verification(page)

            handler = {
                "start":            self._wiz_start,
                "entity_type":      self._wiz_entity_type,
                "registration":     self._wiz_registration,
                "cnpj_input":       self._wiz_cnpj_input,
                "cnpj_list":        self._wiz_cnpj_list,
                "add_company_data": self._wiz_add_company_data,
            }.get(current)

            if handler:
                handler(page)
                _wait(1.5)
            else:
                # unknown — nudge with Avançar / Começar
                print("[VERIFY] Unknown step — nudging with Avançar")
                for btn_name in ("Avançar", "Começar"):
                    try:
                        btn = page.get_by_role("button", name=btn_name)
                        if btn.is_visible(timeout=2_000):
                            btn.click()
                            _wait(1.5)
                            break
                    except Exception:
                        pass

            current = self._detect_wizard_step(page)

        print("[VERIFY] Step cap reached — attempting upload anyway")
        return self._wizard_upload_and_verify(page)

    # ── Individual step handlers ───────────────────────────────────────────────

    def _wiz_start(self, page: Page):
        """Wizard intro screen — click Começar or Avançar."""
        for name in ("Começar", "Avançar"):
            try:
                btn = page.get_by_role("button", name=name)
                if btn.is_visible(timeout=2_000):
                    btn.click()
                    _wait(1)
                    return
            except Exception:
                pass

    def _wiz_entity_type(self, page: Page):
        """Select 'Empresa individual' and advance."""
        # Strategy 1: FB option-card buttons (role="button")
        try:
            page.get_by_role("button", name="Empresa individual").click(timeout=3_000)
            _wait(0.5)
        except Exception:
            # Strategy 2: second radio in the list (Corporação=0, Empresa individual=1)
            try:
                page.get_by_role("radio").nth(1).click(timeout=3_000)
                _wait(0.5)
            except Exception:
                pass
        # Longer timeout so Avançar has time to enable after selection
        try:
            page.get_by_role("button", name="Avançar").click(timeout=8_000)
            _wait(1)
        except Exception:
            pass

    def _wiz_registration(self, page: Page):
        """Select 'Tem registro' (first option) and advance."""
        # Strategy 1: FB option-card button
        clicked = False
        try:
            page.get_by_role("button", name="Tem registro").click(timeout=3_000)
            _wait(0.5)
            clicked = True
        except Exception:
            pass
        # Strategy 2: first radio in the list = "Tem registro"
        if not clicked:
            try:
                page.get_by_role("radio").first.click(timeout=3_000)
                _wait(0.5)
                clicked = True
            except Exception:
                pass
        # Strategy 3: click the text heading directly
        if not clicked:
            try:
                page.get_by_text("Tem registro", exact=True).first.click(timeout=3_000)
                _wait(0.5)
            except Exception:
                pass
        # Give Avançar time to become enabled after selection (8 s)
        try:
            page.get_by_role("button", name="Avançar").click(timeout=8_000)
            _wait(1)
        except Exception:
            pass

    def _wiz_cnpj_input(self, page: Page):
        """Fill in the CNPJ and advance."""
        cnpj = self.run.get("cnpj_digits", "")
        try:
            _clear_fill(page.get_by_label("Identificação Fiscal"), cnpj)
            _wait(0.5)
        except Exception:
            pass
        try:
            page.get_by_role("button", name="Avançar").click(timeout=3_000)
            _wait(2)
        except Exception:
            pass

    def _wiz_cnpj_list(self, page: Page):
        """Select 'not in list' radio and advance."""
        try:
            page.get_by_role("radio", name="Minha empresa não está na").click(timeout=3_000)
            _wait(0.5)
        except Exception:
            pass
        try:
            page.get_by_role("button", name="Avançar").click(timeout=3_000)
            _wait(1)
        except Exception:
            pass

    def _wiz_add_company_data(self, page: Page):
        """
        Handle the multi-sub-step 'Adicionar dados da empresa' form.

        Sub-step 1: fill empty CNPJ field → Avançar.
        Sub-step 2: address pre-filled → Avançar.
        Sub-step 3 ('Informações de contato'): buy a virtual SMS number,
          replace the phone field with it, update the PDF, then Avançar.
          The virtual number is stored so the SMS loop can use it directly.
        """
        cnpj = self.run.get("cnpj_digits", "")
        run_id = self.run.get("run_id")

        # Detect sub-step 3 (contact info) — text OR visible Telefone+Site fields
        is_contact = False
        try:
            dialog = page.locator('[role="dialog"]').first
            dlg_text = dialog.inner_text(timeout=2_000) if dialog.is_visible(timeout=500) else ""
            if "informações de contato" in dlg_text.lower():
                is_contact = True
        except Exception:
            pass
        if not is_contact:
            try:
                has_phone = any(
                    page.get_by_label(lbl).is_visible(timeout=500)
                    for lbl in ("Telefone", "Número de telefone", "Número de celular")
                )
                has_site = any(
                    page.get_by_label(lbl).is_visible(timeout=500)
                    for lbl in ("Site", "Website", "URL do site")
                )
                if has_phone and has_site:
                    is_contact = True
            except Exception:
                pass

        if is_contact and run_id:
            # Buy virtual SMS number to use as the verification phone
            activation_id, full_phone = self.sms.buy_number()
            if activation_id:
                phone_fb  = SMS24HService.to_facebook_format(full_phone)
                phone_pdf = SMS24HService.to_pdf_format(full_phone)
                self._sms_activation_id = activation_id
                self._sms_phone_fb = phone_fb
                print(f"[VERIFY] Virtual SMS number acquired: {phone_fb}")
                # Replace the phone in the wizard form
                self._fill_contact_phone(page, phone_fb)
                # Update the PDF so it matches the verification phone
                try:
                    self.gerador.change_phone(run_id, phone_pdf)
                    print(f"[VERIFY] PDF updated with virtual phone")
                except Exception as e:
                    print(f"[VERIFY] PDF phone update failed: {e}")
            else:
                print("[VERIFY] Could not buy SMS number at contact step — using existing phone")
        else:
            # Sub-step 1: fill CNPJ if the 'Identificação Fiscal' field is empty
            try:
                field = page.get_by_label("Identificação Fiscal")
                if field.is_visible(timeout=1_500):
                    current_val = field.input_value(timeout=1_000)
                    if not current_val and cnpj:
                        _clear_fill(field, cnpj)
                        page.keyboard.press("Tab")   # trigger FB validation
                        _wait(0.8)
            except Exception:
                pass

        # Avançar — 8 s timeout so a freshly-enabled button has time to appear
        try:
            page.get_by_role("button", name="Avançar").click(timeout=8_000)
            _wait(1.5)
        except Exception:
            pass

    def _fill_contact_phone(self, page: Page, phone: str):
        """Fill the phone field on the 'Informações de contato' sub-step."""
        for label in ("Telefone", "Número de telefone", "Número de celular"):
            try:
                field = page.get_by_label(label)
                if field.is_visible(timeout=1_500):
                    _clear_fill(field, phone)
                    _wait(0.5)
                    return
            except Exception:
                pass

    def _wiz_back_to_contact_info(self, page: Page) -> bool:
        """
        Navigate back through the wizard (clicking the back arrow) until the
        'Informações de contato' sub-step is visible.
        Returns True when the step is reached, False if navigation gives up.
        """
        for _ in range(6):
            try:
                back_btns = page.locator('[aria-label="Voltar"]')
                count = back_btns.count()
                if count > 1:
                    back_btns.nth(1).click(timeout=3_000)
                elif count == 1:
                    back_btns.first.click(timeout=3_000)
                else:
                    page.get_by_role("button", name="Voltar").first.click(timeout=3_000)
                _wait(0.8)
            except Exception:
                break
            # Check if we landed on the contact info sub-step (text or fields)
            try:
                dialog = page.locator('[role="dialog"]').first
                dlg = dialog.inner_text(timeout=1_000) if dialog.is_visible(timeout=500) else ""
                if "informações de contato" in dlg.lower():
                    return True
            except Exception:
                pass
            try:
                has_phone = any(
                    page.get_by_label(lbl).is_visible(timeout=400)
                    for lbl in ("Telefone", "Número de telefone", "Número de celular")
                )
                has_site = any(
                    page.get_by_label(lbl).is_visible(timeout=400)
                    for lbl in ("Site", "Website", "URL do site")
                )
                if has_phone and has_site:
                    return True
            except Exception:
                pass
        return False

    # ── Document upload + verification method pipeline ─────────────────────────

    def _wizard_method_first(self, page: Page) -> bool:
        """
        Handle the reversed wizard flow where method selection comes BEFORE
        document upload. Triggered when FB says 'não encontramos um registro
        correspondente' and only shows phone options (no domain).

        Flow: select SMS → Avançar → upload PDF → SMS OTP loop
        """
        run_id = self.run["run_id"]

        # If no virtual number was bought at the contact-info sub-step (detection
        # missed it), navigate back now to acquire one before selecting the method.
        if self._sms_activation_id is None:
            print("[VERIFY] No virtual number — navigating back to contact info to buy one")
            if self._wiz_back_to_contact_info(page):
                activation_id, full_phone = self.sms.buy_number()
                if activation_id:
                    phone_fb  = SMS24HService.to_facebook_format(full_phone)
                    phone_pdf = SMS24HService.to_pdf_format(full_phone)
                    self._sms_activation_id = activation_id
                    self._sms_phone_fb = phone_fb
                    self._fill_contact_phone(page, phone_fb)
                    try:
                        self.gerador.change_phone(run_id, phone_pdf)
                        print(f"[VERIFY] PDF updated with virtual phone {phone_fb}")
                    except Exception as e:
                        print(f"[VERIFY] PDF phone update failed: {e}")
                    # Re-advance from contact-info back to method-selection
                    try:
                        page.get_by_role("button", name="Avançar").click(timeout=8_000)
                        _wait(2)
                    except Exception as e:
                        print(f"[VERIFY] Re-advance from contact-info failed: {e}")
                else:
                    print("[VERIFY] Could not buy virtual number — proceeding anyway")
            else:
                print("[VERIFY] Could not reach contact info — proceeding anyway")

        # Select method — domain not available in this flow, will select SMS
        method = self._wiz_select_method(page)
        print(f"[VERIFY] Method-first flow — chosen method: {method}")
        self._shot(page, "mf_after_method_select")

        if method == "domain":
            # FB shows document upload after domain selection — route through the
            # same state machine used in the normal post-domain flow.
            pdf_path = ""
            try:
                pdf_path = self.gerador.download_pdf(run_id)
                print(f"[VERIFY] PDF downloaded for domain flow: {pdf_path}")
            except Exception as e:
                print(f"[VERIFY] PDF download failed for domain flow: {e}")
            return self._continue_after_domain(page, pdf_path)

        # Detect what FB shows AFTER selecting SMS + Avançar.
        # When called post-domain (_continue_after_domain), the doc was already uploaded so
        # FB skips directly to phone_entry ("Enviar SMS" visible) — no doc upload needed.
        # When called on a fresh CNPJ-not-found flow, FB shows document_upload.
        _wait(2)
        next_step = self._detect_wizard_step(page)
        self._shot(page, f"mf_post_sms_{next_step}")
        print(f"[VERIFY] Post-SMS step: {next_step}")

        if next_step == "complete":
            return True

        # Download PDF — needed for doc upload or SMS retry on timeout.
        # When already at phone_entry the doc was already uploaded, so the PDF is NOT
        # needed for the first OTP attempt (only for re-upload on timeout retry).
        # Don't abort on download failure in that case — proceed with empty path.
        pdf_path = ""
        try:
            pdf_path = self.gerador.download_pdf(run_id)
            print(f"[VERIFY] PDF downloaded: {pdf_path}")
        except Exception as e:
            print(f"[VERIFY] PDF download failed: {e}")
            if next_step not in ("phone_entry", "otp_entry"):
                return False  # PDF required for doc upload — cannot continue
            print("[VERIFY] Proceeding to phone entry without PDF (not needed for first OTP attempt)")

        if next_step in ("phone_entry", "otp_entry"):
            # FB went directly to phone entry — doc was already uploaded earlier
            print("[VERIFY] Post-SMS: already at phone entry — skipping doc upload")
            return self._wiz_sms_loop(page, pdf_path)

        # Default: attempt doc upload (CNPJ-not-found first-time flow)
        self._shot(page, "mf_before_doc_upload")
        if not self._wiz_upload_document(page, pdf_path):
            # Upload reported failure — check if wizard actually advanced past it
            actual = self._detect_wizard_step(page)
            if actual in ("phone_entry", "otp_entry"):
                print("[VERIFY] Upload nominally failed but already at phone entry — continuing")
                return self._wiz_sms_loop(page, pdf_path)
            print("[VERIFY] Doc upload failed in method-first flow")
            return False

        # Now wait for OTP
        return self._wiz_sms_loop(page, pdf_path)

    def _wizard_upload_and_verify(self, page: Page) -> bool:
        """
        Handle the document upload step, then choose a verification method.
        Only buys an SMS number if the SMS method is actually selected.
        Implements a retry loop for SMS with fresh phone numbers.
        """
        run_id = self.run["run_id"]

        # Download the current PDF (original phone)
        try:
            pdf_path = self.gerador.download_pdf(run_id)
            print(f"[VERIFY] Initial PDF downloaded: {pdf_path}")
        except Exception as e:
            print(f"[VERIFY] Could not download PDF: {e}")
            return False

        # ── Document upload step ───────────────────────────────────────────
        if not self._wiz_upload_document(page, pdf_path):
            return False

        # ── Choose verification method ─────────────────────────────────────
        self._shot(page, "verify_method_select")
        method = self._wiz_select_method(page)
        print(f"[VERIFY] Chosen method: {method}")

        if method == "domain":
            # Domain is step 1 — FB often requires SMS as a mandatory second step.
            # Continue through whatever wizard steps follow domain verification.
            return self._continue_after_domain(page, pdf_path)

        # ── SMS retry loop ─────────────────────────────────────────────────
        return self._wiz_sms_loop(page, pdf_path)

    def _continue_after_domain(self, page: Page, pdf_path: str) -> bool:
        """
        After domain verification is selected, FB typically requires a second
        verification via SMS.  This mini state-machine keeps advancing until
        it hits the SMS OTP step or a completion screen.

        Observed sequence:
          domain selected → [domain confirmed] →
          "Escolha como gostaria de confirmar" (phone-only) →
          doc upload → SMS OTP → complete
        """
        for _ in range(10):
            _wait(2)
            step = self._detect_wizard_step(page)
            self._shot(page, f"post_domain_{step}")
            print(f"[VERIFY] Post-domain step: {step}")

            if step == "complete":
                return self._complete_verification(page)

            if step == "domain_confirmed":
                # Domain was verified — click Avançar to proceed to next step
                try:
                    page.get_by_role("button", name="Avançar").click(timeout=5_000)
                    _wait(2)
                except Exception as e:
                    print(f"[VERIFY] domain_confirmed Avançar failed: {e}")
                continue

            if step == "method_selection":
                # Phone-only method selection — "Antes de carregar um documento"
                # This is _wizard_method_first flow: select SMS → upload doc → OTP
                return self._wizard_method_first(page)

            if step in ("otp_entry", "phone_entry"):
                return self._wiz_sms_loop(page, pdf_path)

            if step == "document_upload":
                # Re-upload doc for the phone verification step
                if not self._wiz_upload_document(page, pdf_path):
                    return False
                continue

            # Unknown / loading — try to advance
            try:
                page.get_by_role("button", name="Avançar").click(timeout=3_000)
                _wait(1.5)
            except Exception:
                pass

            # Direct completion check
            try:
                body = page.inner_text("body").lower()
                if "agradecemos" in body or "em análise" in body:
                    print("[VERIFY] Completion text found after domain")
                    return True
            except Exception:
                pass

        return self._complete_verification(page)

    def _wiz_upload_document(self, page: Page, pdf_path: str) -> bool:
        """
        Document upload step:
          1. Click 'Carregar documentos' if it's a selectable option
          2. Select CNPJ from the document-type combobox
          3. Upload the PDF via file input
          4. Tick 'Sim' (document contains a phone number)
          5. Advance
        """
        # Select "Carregar documentos" mode if presented as a choice
        try:
            carregar = page.get_by_text("Carregar documentos", exact=True)
            if carregar.is_visible(timeout=2_000):
                carregar.click()
                _wait(0.5)
        except Exception:
            pass

        self._shot(page, "verify_before_doctype")
        self._wiz_select_doc_type(page)
        self._shot(page, "verify_after_doctype")

        self._shot(page, "verify_before_upload")
        if not self._wiz_set_pdf(page, pdf_path):
            # Before giving up, check if the wizard already advanced past doc upload
            _wait(1)
            actual = self._detect_wizard_step(page)
            if actual in ("method_selection", "phone_entry", "otp_entry", "complete"):
                print(f"[VERIFY] No file input found but wizard is at '{actual}' — treating upload as done")
                return True
            print("[VERIFY] PDF upload failed")
            self._save_html(page, "verify_upload_fail")
            return False

        _wait(1.5)
        self._shot(page, "verify_after_upload")

        # Tick "Sim" — document contains the company phone
        for label in ("Sim", "Yes"):
            try:
                radio = page.get_by_role("radio", name=label)
                if radio.is_visible(timeout=1_500):
                    radio.check()
                    break
            except Exception:
                pass

        try:
            page.get_by_role("button", name="Avançar").click(timeout=5_000)
            _wait(1.5)
        except Exception as e:
            print(f"[VERIFY] Avançar after upload failed: {e}")
            return False

        return True

    def _wiz_select_doc_type(self, page: Page):
        """
        Open the document-type dropdown and select the CNPJ option.
        Tries native <select>, ARIA combobox, and text-click fallbacks.
        """
        # Strategy 1: native <select> — select_option by label
        try:
            combo = page.get_by_role("combobox").first
            if combo.is_visible(timeout=2_000):
                combo.select_option(label="Cadastro Nacional de Pessoa Jurídica (CNPJ)")
                _wait(0.3)
                return
        except Exception:
            pass

        # Strategy 2: ARIA combobox click → listbox option
        try:
            page.get_by_role("combobox").first.click(timeout=2_000)
            _wait(0.5)
            page.get_by_role("option", name="Cadastro Nacional de Pessoa Jurídica").first.click(
                timeout=3_000
            )
            _wait(0.3)
            return
        except Exception:
            pass

        # Strategy 3: click placeholder text → click CNPJ text
        try:
            page.get_by_text("Selecione", exact=False).first.click(timeout=2_000)
            _wait(0.5)
            page.get_by_text("CNPJ").first.click(timeout=3_000)
            _wait(0.3)
            return
        except Exception:
            pass

        # Strategy 4: click combobox → type "cnpj" to filter → pick first option
        try:
            page.get_by_role("combobox").first.click(timeout=2_000)
            _wait(0.4)
            page.keyboard.type("cnpj", delay=80)
            _wait(0.5)
            page.locator('[role="option"]').first.click(timeout=2_000)
            _wait(0.3)
            return
        except Exception:
            pass

        # Strategy 5: LLM vision fallback
        print("[VERIFY] Doc type selection failed — asking LLM")
        try:
            # Open the combobox so the LLM can see the options
            page.get_by_role("combobox").first.click(timeout=1_500)
            _wait(0.6)
        except Exception:
            pass
        llm_text = self._llm_find_action(
            page,
            "click the 'Cadastro Nacional de Pessoa Jurídica (CNPJ)' option in the document "
            "type dropdown. The dropdown may already be open. Return the exact visible text "
            "of the CNPJ option.",
        )
        if llm_text:
            for try_click in (
                lambda t: page.get_by_role("option", name=t, exact=False).first.click(timeout=2_000),
                lambda t: page.get_by_text(t, exact=False).first.click(timeout=2_000),
            ):
                try:
                    try_click(llm_text)
                    _wait(0.3)
                    return
                except Exception:
                    pass

        print("[VERIFY] Could not select doc type — continuing with default")

    def _wiz_set_pdf(self, page: Page, pdf_path: str) -> bool:
        """
        Locate the file input and attach the PDF.
        Tries label-based, raw input[type=file], and file-chooser event.
        """
        # Strategy 1: labelled input
        for label in ("Carregar documentos para", "Selecionar arquivo", "Carregar"):
            try:
                loc = page.get_by_label(label)
                if loc.is_visible(timeout=1_500):
                    loc.set_input_files(pdf_path)
                    return True
            except Exception:
                pass

        # Strategy 2: any visible file input
        try:
            inputs = page.locator('input[type="file"]')
            for i in range(inputs.count()):
                try:
                    inputs.nth(i).set_input_files(pdf_path)
                    return True
                except Exception:
                    continue
        except Exception:
            pass

        # Strategy 3: force-set on every input[type=file] (even hidden ones)
        try:
            for inp in page.locator('input[type="file"]').all():
                try:
                    inp.set_input_files(pdf_path, timeout=3_000)
                    return True
                except Exception:
                    continue
        except Exception:
            pass

        return False

    def _llm_detect_methods(self, page: Page) -> dict:
        """
        Screenshot → LLM: ask which verification methods are currently visible.
        Returns e.g. {"domain": True, "sms": True, "call": False}.
        Falls back to all-False dict on any error or missing API key.
        """
        openai_key = os.getenv("OPENAI_API_KEY") or config.OPENAI_API_KEY
        if not openai_key:
            return {"domain": False, "sms": False, "call": False}
        try:
            img_b64 = base64.b64encode(page.screenshot()).decode()
            payload = {
                "model": "gpt-4o-mini",
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You are analysing a Facebook Business Verification wizard "
                                "screenshot (in Portuguese).\n"
                                "Look at the verification method options visible on screen "
                                "(radio buttons or list items).\n"
                                "Reply with ONLY a JSON object with boolean keys, nothing else:\n"
                                "{\"domain\": <true if a domain/DNS verification option is visible>, "
                                "\"sms\": <true if an SMS text message option is visible>, "
                                "\"call\": <true if a phone call option is visible>}"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{img_b64}",
                                "detail": "low",
                            },
                        },
                    ],
                }],
                "max_tokens": 40,
            }
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            result = json.loads(raw)
            print(f"[LLM] detect_methods → {result}")
            return {
                "domain": bool(result.get("domain")),
                "sms":    bool(result.get("sms")),
                "call":   bool(result.get("call")),
            }
        except Exception as e:
            print(f"[LLM] detect_methods failed: {e}")
            return {"domain": False, "sms": False, "call": False}

    def _wiz_select_method(self, page: Page) -> str:
        """
        Wait for the method selection screen to fully load, then select the best
        available verification method (domain > SMS > call) and advance.

        Primary path: LLM vision detects available methods, then guides each click.
        Fallback (no API key / LLM error): selector-based logic.

        Priority rule: domain FIRST, always.  SMS only when domain is not present.
        Returns 'domain', 'sms', or 'call'.
        """
        # Wait for the screen to finish rendering (radios must be visible)
        try:
            page.locator('[role="radio"]').first.wait_for(state="visible", timeout=10_000)
        except Exception:
            _wait(3)
        _wait(0.5)  # extra settle

        self._shot(page, "method_select_before")

        # ── LLM primary path ───────────────────────────────────────────────
        methods = self._llm_detect_methods(page)

        # Strict priority: domain first, SMS only if domain absent
        if methods["domain"]:
            target = "domain"
            select_goal = "select the domain verification option (radio button or list item)"
        elif methods["sms"]:
            target = "sms"
            select_goal = "select the SMS text message verification option (radio button or list item)"
        elif methods["call"]:
            target = "call"
            select_goal = "select the phone call verification option (radio button or list item)"
        else:
            target = None
            select_goal = ""

        if target:
            # Step 1: find and click the method option
            option_text = self._llm_find_action(page, select_goal)
            clicked_option = False
            if option_text:
                try:
                    page.get_by_text(option_text, exact=False).first.click(timeout=4_000)
                    clicked_option = True
                    print(f"[LLM] Clicked method option: '{option_text}'")
                except Exception as e:
                    print(f"[LLM] Could not click '{option_text}': {e}")

            if not clicked_option:
                # LLM found no text or click failed — try label-based click as bridge
                try:
                    page.get_by_label(option_text or target, exact=False).first.click(timeout=3_000)
                    clicked_option = True
                except Exception:
                    pass

            _wait(0.5)
            self._shot(page, f"method_select_after_{target}")

            # Step 2: click Avançar — ask LLM for the button text first
            advance_text = self._llm_find_action(page, "click the button to advance to the next step (Avançar or similar)")
            try:
                if advance_text and advance_text.upper() != "UNKNOWN":
                    page.get_by_role("button", name=advance_text).click(timeout=5_000)
                else:
                    page.get_by_role("button", name="Avançar").click(timeout=5_000)
            except Exception as e:
                print(f"[LLM] Avançar click failed ({e}), retrying with fallback selector")
                try:
                    page.get_by_role("button", name="Avançar").click(timeout=5_000)
                except Exception:
                    pass
            _wait(1.5)

            # Step 3: some flows show a second confirmation Avançar
            try:
                page.get_by_role("button", name="Avançar").click(timeout=2_000)
                _wait(1.5)
            except Exception:
                pass

            self._shot(page, f"method_select_done_{target}")
            return target

        # ── Selector fallback (no API key or LLM returned all-False) ───────
        print("[VERIFY] LLM method detection unavailable — using selector fallback")

        # Domain verification (best option)
        for sel in [
            lambda: page.locator('[role="radio"]').filter(has_text="domínio").first,
            lambda: page.get_by_text("Verificação de domínio", exact=False).first,
        ]:
            try:
                el = sel()
                if el.is_visible(timeout=2_000):
                    el.scroll_into_view_if_needed()
                    el.click()
                    _wait(0.5)
                    page.get_by_role("button", name="Avançar").click(timeout=5_000)
                    _wait(1.5)
                    try:
                        page.get_by_role("button", name="Avançar").click(timeout=2_000)
                        _wait(1.5)
                    except Exception:
                        pass
                    return "domain"
            except Exception:
                pass

        # SMS (second best)
        for sel in [
            lambda: page.locator('[role="radio"]').filter(has_text="SMS").first,
            lambda: page.get_by_text("Mensagem de texto (SMS)", exact=False).first,
            lambda: page.get_by_role("radio", name="Mensagem de texto (SMS)"),
        ]:
            try:
                el = sel()
                if el.is_visible(timeout=2_000):
                    el.scroll_into_view_if_needed()
                    el.click()
                    _wait(0.5)
                    page.get_by_role("button", name="Avançar").click(timeout=5_000)
                    _wait(1.5)
                    return "sms"
            except Exception:
                pass

        # Last resort: first radio
        try:
            page.locator('[role="radio"]').first.click(timeout=2_000)
            _wait(0.5)
            page.get_by_role("button", name="Avançar").click(timeout=5_000)
            _wait(1.5)
        except Exception:
            pass
        return "sms"

    # ── SMS retry loop ─────────────────────────────────────────────────────────

    def _wiz_sms_loop(self, page: Page, initial_pdf_path: str) -> bool:
        """
        SMS verification loop.

        The phone number for verification is set in the 'Informações de contato'
        wizard sub-step (handled by _wiz_add_company_data).  This loop:

        Attempt 0 (happy path):
          - Uses the virtual number already bought at the contact-info step.
          - Enters the phone on the phone-entry screen (if one exists), sends
            SMS, and waits up to sms_timeout seconds for the OTP.

        On OTP timeout / no pre-bought number:
          - Navigates back through the wizard to the 'Informações de contato'
            sub-step.
          - Buys a fresh virtual number, fills the phone field.
          - Updates the PDF with the new phone.
          - Advances forward: contact-info → document-upload (re-upload PDF)
            → method-selection (select SMS) → phone-entry.
          - Waits for the OTP again.

        A new phone number is only purchased when the previous one times out.
        """
        run_id = self.run["run_id"]
        pdf_path = initial_pdf_path

        # Retrieve the number bought during the contact-info wizard sub-step
        activation_id = self._sms_activation_id
        phone_fb      = self._sms_phone_fb

        for attempt in range(self.sms_max_attempts):
            print(f"[VERIFY] SMS attempt {attempt + 1}/{self.sms_max_attempts}")

            # ── Need to (re-)acquire a virtual number ──────────────────────
            # Either first attempt with no pre-bought number, or OTP timed out.
            if activation_id is None:
                self._shot(page, f"sms_{attempt+1}_before_back")

                # Navigate back to the contact-info sub-step
                if not self._wiz_back_to_contact_info(page):
                    print("[VERIFY] Could not navigate back to contact info — aborting")
                    break

                # Buy new number
                activation_id, full_phone = self.sms.buy_number()
                if not activation_id:
                    print("[VERIFY] Could not buy SMS number — retrying")
                    _wait(10)
                    continue

                phone_fb  = SMS24HService.to_facebook_format(full_phone)
                phone_pdf = SMS24HService.to_pdf_format(full_phone)
                self._sms_activation_id = activation_id
                self._sms_phone_fb = phone_fb
                print(f"[VERIFY] Virtual number acquired: {phone_fb}")

                # Change phone in the contact-info form
                self._fill_contact_phone(page, phone_fb)

                # Update PDF with the new phone
                try:
                    _, pdf_path = self.gerador.change_phone(run_id, phone_pdf)
                    print(f"[VERIFY] PDF updated — phone {phone_fb}")
                except Exception as e:
                    print(f"[VERIFY] PDF update failed: {e}")
                    self.sms.cancel(activation_id)
                    activation_id = None
                    self._sms_activation_id = None
                    continue

                # Advance from contact-info to document-upload
                try:
                    page.get_by_role("button", name="Avançar").click(timeout=8_000)
                    _wait(1.5)
                except Exception as e:
                    print(f"[VERIFY] Avançar from contact-info failed: {e}")
                    self.sms.cancel(activation_id)
                    activation_id = None
                    self._sms_activation_id = None
                    continue

                # Document-upload: wait for the page to finish rendering
                self._shot(page, f"sms_{attempt+1}_doc_reupload")
                try:
                    page.locator('input[type="file"]').first.wait_for(
                        state="attached", timeout=10_000
                    )
                except Exception:
                    _wait(3)
                try:
                    remove_btn = page.get_by_label("Remover arquivo").first
                    if remove_btn.is_visible(timeout=2_000):
                        remove_btn.click()
                        _wait(0.5)
                except Exception:
                    pass

                if not self._wiz_set_pdf(page, pdf_path):
                    print("[VERIFY] Re-upload failed")
                    self.sms.cancel(activation_id)
                    activation_id = None
                    self._sms_activation_id = None
                    continue

                _wait(1.5)
                for label in ("Sim", "Yes"):
                    try:
                        radio = page.get_by_role("radio", name=label)
                        if radio.is_visible(timeout=1_000):
                            radio.check()
                            break
                    except Exception:
                        pass

                try:
                    page.get_by_role("button", name="Avançar").click(timeout=5_000)
                    _wait(1.5)
                except Exception as e:
                    print(f"[VERIFY] Avançar after doc-upload failed: {e}")
                    self.sms.cancel(activation_id)
                    activation_id = None
                    self._sms_activation_id = None
                    continue

                # Method-selection: wait for radios, select SMS
                self._shot(page, f"sms_{attempt+1}_method_reselect")
                try:
                    page.locator('[role="radio"]').first.wait_for(state="visible", timeout=8_000)
                except Exception:
                    _wait(2)

                sms_selected = False
                for sel in [
                    lambda: page.locator('[role="radio"]').filter(has_text="SMS").first,
                    lambda: page.get_by_text("Mensagem de texto (SMS)", exact=False).first,
                ]:
                    try:
                        el = sel()
                        if el.is_visible(timeout=2_000):
                            el.scroll_into_view_if_needed()
                            el.click()
                            _wait(0.5)
                            page.get_by_role("button", name="Avançar").click(timeout=5_000)
                            _wait(1.5)
                            sms_selected = True
                            break
                    except Exception:
                        pass

                if not sms_selected:
                    print("[VERIFY] Could not select SMS method after re-upload")
                    self.sms.cancel(activation_id)
                    activation_id = None
                    self._sms_activation_id = None
                    continue

            # ── Phone-entry screen (may be pre-filled or skipped by FB) ───
            self._shot(page, f"sms_{attempt+1}_phone_entry")
            for label in ("Telefone", "Número de telefone", "Número de celular", "Phone number"):
                try:
                    field = page.get_by_label(label)
                    if field.is_visible(timeout=2_000):
                        _clear_fill(field, phone_fb)
                        _wait(0.5)
                        break
                except Exception:
                    pass

            try:
                page.get_by_role("button", name="Avançar").click(timeout=5_000)
                _wait(1.5)
                if page.locator("text=Ocorreu um problema ao salvar").is_visible(timeout=1_500):
                    self._shot(page, f"sms_{attempt+1}_phone_error")
                    print("[VERIFY] Phone format error — retrying Avançar")
                    page.get_by_role("button", name="Avançar").click()
                    _wait(1)
            except Exception as e:
                self._shot(page, f"sms_{attempt+1}_phone_fail")
                print(f"[VERIFY] Phone/Avançar failed: {e}")

            # ── Send SMS ───────────────────────────────────────────────────
            try:
                page.get_by_role("button", name="Enviar SMS").click(timeout=5_000)
            except Exception:
                pass

            self._shot(page, f"sms_{attempt+1}_waiting_otp")

            # ── Wait for OTP (default 180 s = 3 min) ──────────────────────
            otp = self.sms.wait_for_otp(activation_id, timeout=self.sms_timeout)
            if otp:
                print(f"[VERIFY] OTP received: {otp}")
                try:
                    # Try known placeholders then fall back to any dialog input
                    otp_field = None
                    for ph in ("Código de confirmação", "12345", "código"):
                        try:
                            f = page.get_by_placeholder(ph, exact=False)
                            if f.is_visible(timeout=1_000):
                                otp_field = f
                                break
                        except Exception:
                            pass
                    if otp_field is None:
                        otp_field = page.locator('[role="dialog"] input').first
                    _clear_fill(otp_field, otp)
                    self._shot(page, f"sms_{attempt+1}_otp_filled")
                    page.get_by_role("button", name="Avançar").click(timeout=5_000)
                    _wait(2)
                    return self._complete_verification(page)
                except Exception as e:
                    self._shot(page, f"sms_{attempt+1}_otp_fail")
                    self._save_html(page, f"sms_{attempt+1}_otp_fail")
                    print(f"[VERIFY] OTP entry failed: {e}")
            else:
                self._shot(page, f"sms_{attempt+1}_otp_timeout")
                print("[VERIFY] No OTP — will retry with a new number")
                self.sms.cancel(activation_id)
                activation_id = None
                self._sms_activation_id = None

        print("[VERIFY] All SMS attempts exhausted")
        self._shot(page, "verification_fail")
        return False

    def _complete_verification(self, page: Page) -> bool:
        """
        Wait for any completion indicator and return True.

        Accepted success texts (in order of likelihood):
          - "Processando seu envio"   — FB is processing (counts as success)
          - "Agradecemos o envio"     — thank-you screen
          - "Em análise"              — review-status badge
        """
        SUCCESS = (
            "Processando seu envio",
            "Agradecemos o envio",
            "Em análise",
        )

        # Wait up to 35 s for any success indicator
        deadline = time.time() + 35
        while time.time() < deadline:
            for txt in SUCCESS:
                try:
                    if page.get_by_text(txt, exact=False).first.is_visible(timeout=500):
                        print(f"[VERIFY] Wizard completed — '{txt}'")
                        # Wait 60 s before returning — closing the browser too soon
                        # cancels the submission while FB is still processing it.
                        print("[VERIFY] Waiting 60 s for FB to finalize submission…")
                        _wait(60)
                        return True
                except Exception:
                    pass
            _wait(2)

        # Final content fallback
        try:
            body = page.content()
            return any(t in body for t in SUCCESS)
        except Exception:
            return False
