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

# Claude client — lazily initialised on first LLM call
_claude_client = None

def _get_claude_client():
    global _claude_client
    if _claude_client is None:
        import anthropic
        import config as _cfg
        api_key = _cfg.ANTHROPIC_API_KEY or os.getenv("ANTHROPIC_API_KEY", "")
        if api_key:
            _claude_client = anthropic.Anthropic(api_key=api_key)
    return _claude_client
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, BrowserContext

import config
from services.gerador_facade import GeradorService as GeradorClient
from services.sms24h import SMS24HService


# ── helpers ───────────────────────────────────────────────────────────────────

def _wait(seconds: float = 1.0):
    time.sleep(seconds)


def _log_phone_match(pdf_path: str, phone_fb: str, tel_fmt: str):
    """Append a line to phone_log.txt next to the PDF for post-run verification."""
    from pathlib import Path
    log_file = Path(pdf_path).parent / "phone_log.txt"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] wizard={phone_fb} | documento={tel_fmt}\n")


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


# Ordered mapping: (title_contains, subtitle_contains_or_None, step_name)
# More specific entries (with subtitle) come before general ones (subtitle=None).
# Matching is case-insensitive substring check on the extracted wizard title/subtitle.
_WIZARD_TITLE_MAP: list[tuple[str, "str | None", str]] = [
    # Screen 9 — unique title
    ("escolha como gostaria de confirmar", None, "method_selection"),
    # Screen 8 — unique title
    ("carregar documentos",               None, "document_upload"),
    # Screen 2 — unique title
    ("selecionar um país",                None, "country_selection"),
    # Screens 3 & 4 — same title, disambiguate by subtitle
    ("selecione o tipo da sua empresa",   "registro",  "registration"),
    ("selecione o tipo da sua empresa",   None,        "entity_type"),
    # Screens 5, 6, 7 — all map to add_company_data; handler differentiates internally
    ("adicionar dados da empresa",        None,        "add_company_data"),
    # reCAPTCHA modal
    ("ajude-nos a confirmar",             None,        "identity_check"),
    # Screen 1 — "Verificar [BUSINESS_NAME]" (prefix match)
    ("verificar ",                        None,        "start"),
]


def _click_comecar(page: Page) -> bool:
    """
    Find and click the 'Começar' intro modal button.

    Facebook renders it as <div role="button" aria-label="Começar"> — not a
    native <button> — so we skip the is_visible() check (which can give false
    negatives during rendering) and attempt each selector directly, catching
    timeout exceptions as "not found".

    Returns True if the button was clicked successfully.
    """
    css_selectors = [
        '[role="button"][aria-label="Começar"]',
        '[role="button"]:has-text("Começar")',
        'button:has-text("Começar")',
    ]
    for sel in css_selectors:
        try:
            page.locator(sel).first.click(timeout=2_000)
            print(f"[VERIFY] 'Começar' intro modal clicked via selector: {sel}")
            _wait(2)
            return True
        except Exception:
            continue
    # Last resort: accessible-name match
    try:
        page.get_by_role("button", name="Começar").first.click(timeout=2_000)
        print("[VERIFY] 'Começar' intro modal clicked via get_by_role")
        _wait(2)
        return True
    except Exception:
        pass
    return False


# ── custom exception ─────────────────────────────────────────────────────────

class VerificationStepError(RuntimeError):
    """Raised when a specific verification step fails, carrying step name, page URL, and HTML."""
    def __init__(self, step: str, reason: str, page_url: str = "", page_html: str = "",
                 screenshot_path: str = ""):
        super().__init__(f"[{step}] {reason}")
        self.step = step
        self.reason = reason
        self.page_url = page_url
        self.page_html = page_html
        self.screenshot_path = screenshot_path


class BmRestrictedException(RuntimeError):
    """Raised when the Business Manager is detected as restricted/disabled for advertising."""
    pass


class DomainVerificationError(RuntimeError):
    """Raised when the domain was not verified after clicking 'Verificar domínio'. Non-retryable."""
    pass


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
            return ""
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        self._purge_old_debug_files()
        self._step += 1
        run_id = self.run.get("run_id", "x")
        ts = datetime.now().strftime("%H%M%S")
        name = f"{run_id}_{ts}_{self._step:02d}_{label}.png"
        path_str = str(self._debug_dir / name)
        try:
            page.screenshot(path=path_str, full_page=True)
            print(f"[DEBUG] Screenshot → debug/{name}")
            return path_str
        except Exception as e:
            print(f"[DEBUG] Screenshot failed ({label}): {e}")
            return ""

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

    def _flush_remark(self):
        """
        Write the current _gerador_data (including run_id) to the AdsPower
        profile remark without adding any new step flags.  Call this as early
        as possible so run_id is persisted even if the session crashes before
        the first step completes.
        """
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
            print(f"[STEP] run_id={self._gerador_data.get('run_id')} persisted to profile remark")
        except Exception as e:
            print(f"[STEP] Could not flush remark: {e}")

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

        # Persist run_id to the profile remark immediately — before any automation step —
        # so re-runs reuse the same company data even if the session crashes early.
        self._flush_remark()

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
                    raise VerificationStepError("login", "Login falhou — credenciais inválidas ou checkpoint do Facebook", page.url, page.content())
                self._shot(page, "login_ok")

                self._ensure_portuguese(page)

                if not self.business_id:
                    self.business_id = self._create_business_portfolio(page, ctx)
                    if not self.business_id:
                        self._shot(page, "portfolio_fail")
                        self._save_html(page, "portfolio_fail")
                        print("[BOT] Could not create Business Portfolio")
                        raise VerificationStepError("business_portfolio", "Não foi possível criar o Business Portfolio no Meta", page.url, page.content())
                    self._shot(page, "portfolio_ok")
                    self._mark_step_done("business_id", self.business_id)

                phase_order = self.run.get("middle_phase_order", ["business_info", "domain", "waba"])
                phase_map = {
                    "business_info": self._phase_business_info,
                    "domain":        self._phase_domain,
                    "waba":          self._phase_waba,
                }
                for _phase_name in phase_order:
                    _phase_fn = phase_map.get(_phase_name)
                    if not _phase_fn:
                        print(f"[BOT] Unknown phase {_phase_name!r} in MIDDLE_PHASE_ORDER — skipping")
                        continue
                    _phase_result = _phase_fn(page)
                    if isinstance(_phase_result, dict) and not _phase_result.get("success", True):
                        return _phase_result

                result = False
                _last_verification_error: "VerificationStepError | None" = None
                for verify_attempt in range(3):
                    try:
                        result = self._run_business_verification(page)
                    except BmRestrictedException:
                        raise  # Never retry BM-restricted WABAs — propagate immediately
                    except VerificationStepError as vse:
                        print(f"[BOT] Verification attempt {verify_attempt+1} failed at step '{vse.step}': {vse.reason}")
                        _last_verification_error = vse
                        result = False
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
                if not result:
                    if _last_verification_error:
                        raise _last_verification_error  # preserve specific step/reason
                    raise VerificationStepError(
                        "business_verification",
                        "Wizard de verificação do negócio não concluído após 3 tentativas (nenhum erro específico capturado)",
                        page.url,
                        page.content(),
                    )
                return result

            except (VerificationStepError, BmRestrictedException, DomainVerificationError):
                raise  # propagate with type info intact
            except Exception as e:
                print(f"[BOT] Unexpected error: {e}")
                self._shot(page, "crash")
                self._save_html(page, "crash")
                if self._debug:
                    traceback.print_exc()
                try:
                    html = page.content()
                except Exception:
                    html = ""
                raise VerificationStepError("unexpected", str(e), page.url, html)
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

    # ── middle phase methods (orderable via MIDDLE_PHASE_ORDER) ─────────────

    def _phase_business_info(self, page: "Page"):
        if not self._gerador_data.get("business_info_done"):
            self._set_company_details(page)
            self._shot(page, "company_details")
            self._mark_step_done("business_info_done")
        else:
            print("[BOT] Skipping company details (already done)")

    def _phase_domain(self, page: "Page"):
        # Accept both current flag name ("domain_done") and legacy name ("domain_zone")
        domain_already_done = (
            self._gerador_data.get("domain_done") or self._gerador_data.get("domain_zone")
        )
        if not domain_already_done:
            # Navigate to domains page first so we can check for an existing "Verificado" badge
            # before trying to add the domain again.
            try:
                page.goto(
                    f"https://business.facebook.com/settings/owned-domains/?business_id={self.business_id}",
                    wait_until="domcontentloaded", timeout=15_000,
                )
                _wait(2)
                for verified_text in ("Verificado", "Verified"):
                    try:
                        if page.get_by_text(verified_text, exact=True).is_visible(timeout=3_000):
                            print(f"[BOT] Domain already verified on page ('{verified_text}') — marking done and skipping")
                            self._mark_step_done("domain_done")
                            domain_already_done = True
                            break
                    except Exception:
                        pass
            except Exception:
                pass

        if not domain_already_done:
            verify_method = self.run.get("domain_verification_method", "meta_tag")
            print(f"[BOT] domain_verification_method={verify_method!r} (from self.run)")
            meta_tag = self._add_domain(page)

            if verify_method == "dns_txt":
                token = self._select_dns_txt_method_and_extract_token(page)
                if not token:
                    self._shot(page, "domain_no_txt_token")
                    return {"success": False, "error": "Domain verification failed: TXT token not found"}

                # Split FQDN into (host, parent_domain) using the dominios pool
                host, parent = "", ""
                for parent_candidate in self.run.get("dominios", []):
                    if self.domain == parent_candidate or self.domain.endswith("." + parent_candidate):
                        parent = parent_candidate
                        host = self.domain[:-(len(parent) + 1)] if self.domain != parent else "@"
                        break
                if not parent:
                    self._shot(page, "domain_no_parent")
                    raise DomainVerificationError(
                        f"[DOMAIN] {self.domain} not in dominios pool — cannot add TXT record"
                    )

                from services.cloudpanel_deploy import adicionar_txt_record
                api_key = self.run.get("spaceship_api_key", "")
                api_secret = self.run.get("spaceship_api_secret", "")
                if not api_key or not api_secret:
                    raise DomainVerificationError("[DOMAIN] Spaceship credentials missing in run data")

                ok = adicionar_txt_record(parent, host, f"facebook-domain-verification={token}", api_key, api_secret)
                if not ok:
                    self._shot(page, "domain_txt_failed")
                    raise DomainVerificationError("[DOMAIN] Failed to add TXT record on Spaceship")

                _wait(45)  # DNS propagation window
                last_err = None
                verified = False
                for attempt in range(3):
                    try:
                        verified = self._verify_domain(page)
                        if verified:
                            break
                    except DomainVerificationError as e:
                        last_err = e
                        if attempt < 2:
                            _wait(30)
                            try:
                                page.reload(wait_until="domcontentloaded", timeout=15_000)
                                _wait(2)
                            except Exception:
                                pass
                if verified:
                    self._shot(page, "domain_verified")
                    self._mark_step_done("domain_done")
                else:
                    self._shot(page, "domain_failed")
                    raise last_err or DomainVerificationError("[DOMAIN] DNS verification failed after retries")

            else:
                # ── meta-tag flow (default) ──
                if meta_tag:
                    print(f"[BOT] Injecting meta tag: {meta_tag[:60]}…")
                    self.gerador.inject_meta_tag(self.run["run_id"], meta_tag)
                    _wait(30)  # give DNS / CloudPanel time to propagate
                    try:
                        verified = self._verify_domain(page)
                        self._shot(page, "domain_verified")
                        if verified:
                            self._mark_step_done("domain_done")
                    except DomainVerificationError:
                        self._shot(page, "domain_failed")
                        raise  # non-retryable — propagate directly
                    except RuntimeError as e:
                        self._shot(page, "domain_failed")
                        return {"success": False, "error": str(e)}
                else:
                    self._shot(page, "domain_no_metatag")
                    # No meta tag returned — domain may already exist and be verified
                    # from a previous partial run where the remark wasn't saved.
                    # Check the domains settings page for a "Verificado" badge.
                    print("[BOT] No meta tag — checking if domain already verified")
                    domain_verified_found = False
                    for verified_text in ("Verificado", "Verified"):
                        try:
                            if page.get_by_text(verified_text, exact=True).is_visible(timeout=3_000):
                                print(f"[BOT] Domain already verified ('{verified_text}') — marking done")
                                self._mark_step_done("domain_done")
                                domain_verified_found = True
                                break
                        except Exception:
                            pass
                    if not domain_verified_found:
                        print("[BOT] Domain not verified and no meta tag available — aborting")
                        return {"success": False, "error": "Domain verification failed: no meta tag returned and Verified badge not found"}
        else:
            print("[BOT] Skipping domain (already done — domain_done or domain_zone flag set)")

    def _phase_waba(self, page: "Page"):
        # Accept both "waba_done" and legacy "waba_created" flag names
        if not (self._gerador_data.get("waba_done") or self._gerador_data.get("waba_created")):
            if self._create_waba(page):
                self._shot(page, "waba_done")
                self._mark_step_done("waba_done")
            else:
                self._shot(page, "waba_fail")
                return {"success": False, "error": "WABA creation failed — account not found on settings page after retries"}
        else:
            print("[BOT] Skipping WABA (already done — waba_done or waba_created flag set)")

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

    @staticmethod
    def _craft_random_email() -> str:
        import random, string
        local = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"{local}@hotmail.com"

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

    def _find_jasper_or_click_entrar(self, page: Page) -> bool:
        """Try to find Jasper's Market placeholder. If not found, click 'Entrar com o Facebook' and retry."""
        # First attempt — just look for the placeholder
        try:
            field = page.get_by_placeholder("Jasper's Market")
            if field.is_visible(timeout=3_000):
                return True
        except Exception:
            pass

        # Not found — try clicking "Entrar com o Facebook" button
        print("[BM] Jasper's Market not found — looking for 'Entrar com o Facebook' button")
        try:
            entrar_btn = page.get_by_role("button").filter(has_text="Entrar com o Facebook")
            if entrar_btn.is_visible(timeout=3_000):
                entrar_btn.click()
                print("[BM] Clicked 'Entrar com o Facebook'")
                _wait(3)
                _dismiss_overlays(page)
                # Retry finding Jasper's Market
                try:
                    field = page.get_by_placeholder("Jasper's Market")
                    if field.is_visible(timeout=5_000):
                        return True
                except Exception:
                    pass
        except Exception:
            pass

        print("[BM] Jasper's Market still not found after 'Entrar com o Facebook' attempt")
        return False

    def _create_business_portfolio(self, page: Page, ctx: BrowserContext) -> str:
        # Collect everything that requires navigation BEFORE opening the modal
        first, last = self._scrape_user_name(page)
        if self.email_mode == "temp":
            email = self._get_temp_email(page)
            if not email:
                print("[BM] Temp email failed — falling back to random email")
                email = self._craft_random_email()
        else:
            email = self._craft_random_email()

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
            print("[BM] Could not find portfolio button on adsmanager — trying /create fallback")
            return self._create_business_portfolio_biz_create(page, email)
        _wait(1)

        # Business name — razao_social comes in ALL CAPS; FB rejects it, so title-case it
        biz_name = self.run["razao_social"].title()
        if not self._find_jasper_or_click_entrar(page):
            print("[BM] Jasper's Market not found on adsmanager — falling through to /create")
            return self._create_business_portfolio_biz_create(page, email)
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
            print("[BM] Could not click 'Criar' — falling through to /create fallback")
            return self._create_business_portfolio_biz_create(page, email)

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

        biz_id = self._resolve_business_id_from_select(page)
        print(f"[BM] Portfolio created — business_id: {biz_id} (url: {page.url})")
        return biz_id

    def _resolve_business_id_from_select(self, page: Page) -> str:
        """Navigate to /select and extract business_id for the newly-created BM.

        - Single BM: FB redirects immediately → parse business_id from URL.
        - Multiple BMs: picker shown → click the entry matching self.run["razao_social"].

        Retries up to 4 times with increasing waits because FB may take several
        seconds to register the new BM on the backend after form submission.
        """
        # Check if we're already on a page with business_id (e.g. post-onboarding)
        m = re.search(r"business_id[=\/](\d+)", page.url)
        if m:
            return m.group(1)

        razao = self.run.get("razao_social", "")
        razao_norm = razao.lower().strip()
        razao_title = razao.title().lower()

        for attempt in range(4):
            wait_secs = 5 + attempt * 5  # 5, 10, 15, 20
            print(f"[BM] _resolve_business_id attempt {attempt + 1}/4 (wait {wait_secs}s)")
            _wait(wait_secs)

            page.goto("https://business.facebook.com/select", wait_until="domcontentloaded", timeout=30_000)
            _wait(3)

            url = page.url
            m = re.search(r"business_id[=\/](\d+)", url)
            if m:
                return m.group(1)

            # Picker page — find the <a> card matching razao_social
            anchors = page.locator("a[href*='business_id=']").all()
            if not anchors:
                # No cards yet — FB hasn't registered the BM, retry
                continue

            clicked = False
            for anchor in anchors:
                try:
                    text = (anchor.text_content() or "").lower().strip()
                    if razao_norm and razao_norm in text:
                        anchor.click()
                        clicked = True
                        break
                except Exception:
                    pass

            if not clicked:
                for anchor in anchors:
                    try:
                        text = (anchor.text_content() or "").lower().strip()
                        if razao_title and razao_title in text:
                            anchor.click()
                            clicked = True
                            break
                    except Exception:
                        pass

            if not clicked:
                anchors[-1].click()
                clicked = True

            if clicked:
                try:
                    page.wait_for_url(re.compile(r"business_id"), timeout=15_000)
                except Exception:
                    pass
                _wait(2)
                m = re.search(r"business_id[=\/](\d+)", page.url)
                if m:
                    return m.group(1)

        return ""

    def _create_business_portfolio_biz_fallback(self, page: Page, email: str) -> str:
        """Fallback BM creation via business.facebook.com when adsmanager path fails."""
        print("[BM-fallback] Navigating to business.facebook.com")
        page.goto("https://business.facebook.com", wait_until="domcontentloaded", timeout=60_000)
        _wait(3)
        _dismiss_overlays(page)

        # Find and click the portfolio creation button
        portf_clicked = False
        for attempt in range(4):
            _dismiss_overlays(page)
            _wait(0.5)

            for btn_text in (
                "Criar um portfólio empresarial",
                "Crie um portfólio de negócios",
                "Criar portfólio",
                "Novo portfólio",
                "Criar conta",
            ):
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

            llm_text = self._llm_find_action(
                page,
                "click the button that creates a new business portfolio or business account. "
                "If a modal or overlay is blocking, return its dismiss button text first.",
            )
            if llm_text:
                print(f"[BM-fallback] LLM suggests: '{llm_text}'")
                try:
                    page.get_by_role("button", name=llm_text).first.click(timeout=5_000)
                    _wait(1)
                except Exception:
                    try:
                        page.get_by_text(llm_text, exact=False).first.click(timeout=3_000)
                        _wait(1)
                    except Exception as e:
                        print(f"[BM-fallback] Could not click LLM suggestion: {e}")

        if not portf_clicked:
            print("[BM-fallback] Could not find portfolio creation button — trying /create fallback")
            return self._create_business_portfolio_biz_create(page, email)
        _wait(1)

        # Business name (same placeholder as adsmanager form)
        biz_name = self.run["razao_social"].title()
        if not self._find_jasper_or_click_entrar(page):
            print("[BM-fallback] Jasper's Market not found — falling through to /create")
            return self._create_business_portfolio_biz_create(page, email)
        try:
            _clear_fill(page.get_by_placeholder("Jasper's Market"), biz_name)
        except Exception as e:
            print(f"[BM-fallback] Could not fill business name: {e}")
            return self._create_business_portfolio_biz_create(page, email)

        # Email — no placeholder on this form; try common labels then positional fallback
        if email:
            filled = False
            for label_text in ("Email comercial", "Email", "Endereço de email"):
                try:
                    _clear_fill(page.get_by_label(label_text), email)
                    filled = True
                    break
                except Exception:
                    pass
            if not filled:
                # Second visible text input that isn't the business name field
                try:
                    for inp in page.locator("input[type='text']").all():
                        if inp.is_visible(timeout=500):
                            placeholder = inp.get_attribute("placeholder") or ""
                            if "Jasper" not in placeholder:
                                _clear_fill(inp, email)
                                filled = True
                                break
                except Exception as e:
                    print(f"[BM-fallback] Could not fill email: {e}")
            _wait(1)

        # Click "Enviar"
        enviar_clicked = False
        for attempt in range(3):
            try:
                page.get_by_role("button", name="Enviar", exact=True).click(timeout=6_000)
                enviar_clicked = True
                break
            except Exception as e:
                if attempt < 2:
                    print(f"[BM-fallback] 'Enviar' not clickable (attempt {attempt+1}) — {e}")
                    fix = self._llm_fix_form(page)
                    if fix:
                        print(f"[BM-fallback] LLM applied fix: {fix}")
                    _wait(1)

        if not enviar_clicked:
            print("[BM-fallback] Could not click 'Enviar' — aborting")
            return ""

        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        _wait(3)

        # Skip onboarding screens
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

        biz_id = self._resolve_business_id_from_select(page)
        print(f"[BM-fallback] Portfolio created — business_id: {biz_id} (url: {page.url})")
        return biz_id

    def _create_business_portfolio_biz_create(self, page: Page, email: str) -> str:
        """Last-resort BM creation via business.facebook.com/create (direct form URL)."""
        print("[BM-create] Navigating to business.facebook.com/create")
        page.goto("https://business.facebook.com/create", wait_until="domcontentloaded", timeout=60_000)
        _wait(3)
        _dismiss_overlays(page)

        # Business name — try helper (clicks "Entrar com o Facebook" if needed),
        # then retry by reloading /create if still not found
        biz_name = self.run["razao_social"].title()
        jasper_found = self._find_jasper_or_click_entrar(page)
        if not jasper_found:
            print("[BM-create] Retrying — reloading business.facebook.com/create")
            page.goto("https://business.facebook.com/create", wait_until="domcontentloaded", timeout=60_000)
            _wait(3)
            _dismiss_overlays(page)
            jasper_found = self._find_jasper_or_click_entrar(page)
        if not jasper_found:
            print("[BM-create] Could not find Jasper's Market after retries — aborting")
            return ""
        try:
            _clear_fill(page.get_by_placeholder("Jasper's Market"), biz_name)
        except Exception as e:
            print(f"[BM-create] Could not fill business name: {e}")
            return ""

        # Email field — no placeholder; fill the second visible text input
        if email:
            filled = False
            for label_text in ("Email comercial", "Email", "Endereço de email"):
                try:
                    _clear_fill(page.get_by_label(label_text), email)
                    filled = True
                    break
                except Exception:
                    pass
            if not filled:
                try:
                    for inp in page.locator("input[type='text'], input[type='email']").all():
                        if inp.is_visible(timeout=500):
                            placeholder = inp.get_attribute("placeholder") or ""
                            if "Jasper" not in placeholder:
                                _clear_fill(inp, email)
                                filled = True
                                break
                except Exception as e:
                    print(f"[BM-create] Could not fill email: {e}")
            _wait(1)

        # Click "Enviar"
        enviar_clicked = False
        for attempt in range(3):
            try:
                page.get_by_role("button", name="Enviar", exact=True).click(timeout=6_000)
                enviar_clicked = True
                break
            except Exception as e:
                if attempt < 2:
                    print(f"[BM-create] 'Enviar' not clickable (attempt {attempt+1}) — {e}")
                    fix = self._llm_fix_form(page)
                    if fix:
                        print(f"[BM-create] LLM applied fix: {fix}")
                    _wait(1)

        if not enviar_clicked:
            print("[BM-create] Could not click 'Enviar' — aborting")
            return ""

        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        _wait(3)

        # Skip onboarding screens
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

        biz_id = self._resolve_business_id_from_select(page)
        print(f"[BM-create] Portfolio created — business_id: {biz_id} (url: {page.url})")
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

    def _update_business_phone(self, page: Page, phone_digits: str):
        """Update Telefone comercial in business_info, then return to the security
        center and re-enter the verification wizard so the caller can continue."""
        if not self.business_id or not phone_digits:
            return
        try:
            page.goto(
                f"https://business.facebook.com/latest/settings/business_info?business_id={self.business_id}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            _wait(2)
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
            tel = phone_digits if phone_digits.startswith("55") else "55" + phone_digits
            try:
                _clear_fill(page.get_by_label("Telefone comercial"), tel)
            except Exception:
                pass
            _click_with_retry(
                page,
                lambda: page.get_by_role("button", name="Salvar"),
                timeout=5_000,
            )
            page.wait_for_load_state("domcontentloaded", timeout=60_000)
            _wait(2)
            _dismiss_overlays(page)
            print(f"[VERIFY] Business phone updated to {tel}")
        except Exception as e:
            print(f"[VERIFY] _update_business_phone failed: {e}")

        # Navigate back to the security center and re-open the verification wizard
        # so the caller can continue the wizard flow from where it left off.
        try:
            page.goto(
                f"https://business.facebook.com/settings/security/?business_id={self.business_id}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            _wait(2)
            _dismiss_overlays(page)
            # Verification is in progress → FB shows "Continuar"; fresh start → other labels
            for btn_name in ("Continuar", "Iniciar verificação", "Verificar", "Começar"):
                try:
                    btn = page.get_by_role("button", name=btn_name).first
                    if btn.is_visible(timeout=3_000):
                        btn.click(timeout=5_000)
                        _wait(2)
                        print(f"[VERIFY] Re-entered wizard via '{btn_name}'")
                        break
                except Exception:
                    pass
            # Handle the "Começar" intro modal if it appears
            _click_comecar(page)
            _wait(1)
        except Exception as e:
            print(f"[VERIFY] Failed to re-enter wizard after business_info update: {e}")

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

        # Detect BM restriction before doing anything else
        try:
            body_t = page.inner_text("body").lower()
            if "não pode usar este portfólio empresarial para anunciar" in body_t \
                    or "you can't use this business portfolio to advertise" in body_t:
                self._shot(page, "bm_restricted")
                raise BmRestrictedException(
                    "Business Manager está restrito — portfólio bloqueado para anúncios"
                )
        except BmRestrictedException:
            raise
        except Exception:
            pass

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
            # Fallback: look for just the content token in visible text.
            # Must be >= 20 chars to avoid matching unrelated content= attributes
            # like content="noarchive", content="width", etc.
            m2 = re.search(r'content="([a-z0-9]{20,})"', raw)
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

    def _select_dns_txt_method_and_extract_token(self, page: Page) -> str:
        """
        Switch the verification method combobox from 'Adicionar uma metatag' to
        'Atualize o registro TXT do DNS', then extract the bare token.
        Called while still on the domain detail page right after _add_domain().
        Returns the token string or '' on failure.
        """
        self._shot(page, "dns_01_before_select")

        # ── Step 1: open the combobox ─────────────────────────────────────────
        # Facebook's React combobox listens for mousedown at the document root
        # (React synthetic events). Playwright's .click() can miss because its
        # hit-test sees the wrapping label, not the combobox itself.
        # Fix: dispatch a native MouseEvent with bubbles:true from JS so React's
        # document-level listener picks it up. Try multiple locator strategies.
        opened = False
        _combo_locators = [
            page.get_by_label("Selecione uma opção"),
            page.locator('[aria-haspopup="listbox"]').first,
            page.locator('[role="combobox"]').first,
        ]
        for combo in _combo_locators:
            try:
                combo.wait_for(state="visible", timeout=4_000)
            except Exception:
                continue
            # Primary: bubble a real mousedown so React's synthetic event router fires
            for _fn in [
                lambda c=combo: c.evaluate(
                    "el => el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true,cancelable:true,view:window}))"
                ),
                lambda c=combo: c.evaluate("el => el.click()"),
                lambda c=combo: c.click(force=True),
            ]:
                try:
                    _fn()
                    # Confirm the listbox appeared
                    page.locator('[role="listbox"], [role="option"]').first.wait_for(
                        state="visible", timeout=2_000
                    )
                    opened = True
                    break
                except Exception:
                    pass
            if opened:
                break

        if not opened:
            print("[DOMAIN/DNS] Could not open the verification method combobox")
            return ""

        _wait(1.5)
        self._shot(page, "dns_02_dropdown_open")

        # ── Step 2: select the DNS TXT option ────────────────────────────────
        # Listbox is a React portal rendered at body level — wait for it first.
        selected = False
        try:
            page.locator('[role="listbox"]').wait_for(state="visible", timeout=6_000)
            for _loc in [
                page.locator('[role="listbox"]').get_by_text("Atualize o registro TXT do DNS"),
                page.locator('[role="listbox"]').get_by_text(re.compile(r"TXT do DNS", re.I)),
            ]:
                try:
                    _loc.first.click(force=True, timeout=4_000)
                    selected = True
                    break
                except Exception:
                    pass
        except Exception:
            pass

        if not selected:
            # listbox may lack role="listbox" — search full page
            for _loc in [
                page.get_by_text("Atualize o registro TXT do DNS"),
                page.get_by_text(re.compile(r"Atualize o registro TXT", re.I)),
                page.get_by_text(re.compile(r"TXT do DNS", re.I)),
            ]:
                try:
                    _loc.first.wait_for(state="visible", timeout=4_000)
                    _loc.first.click(force=True, timeout=4_000)
                    selected = True
                    break
                except Exception:
                    pass

        if not selected:
            print("[DOMAIN/DNS] Could not select the DNS TXT option")
            return ""

        _wait(2)
        self._shot(page, "dns_03_dns_selected")

        # ── Step 3: extract token ─────────────────────────────────────────────
        # Live HTML shows token inside <strong>, not <code>/<pre>.
        def _extract_token(raw: str) -> str:
            m = re.search(r'facebook-domain-verification=([a-z0-9]{20,})', raw)
            return m.group(1) if m else ""

        token = ""
        for _sel in (
            'strong:has-text("facebook-domain-verification")',
            'code:has-text("facebook-domain-verification")',
            'pre:has-text("facebook-domain-verification")',
            '[class*="code"]:has-text("facebook-domain-verification")',
            'span:has-text("facebook-domain-verification")',
        ):
            try:
                el = page.locator(_sel).first
                if el.is_visible(timeout=3_000):
                    token = _extract_token(el.inner_text(timeout=3_000))
                    if token:
                        break
            except Exception:
                pass

        if not token:
            try:
                token = _extract_token(page.locator("body").inner_text(timeout=5_000))
            except Exception:
                pass
        if not token:
            try:
                token = _extract_token(page.content())
            except Exception:
                pass

        if token:
            print(f"[DOMAIN/DNS] TXT token: {token}")
        else:
            print("[DOMAIN/DNS] TXT token not found")
        return token

    def _verify_domain(self, page: Page) -> bool:
        """
        Click 'Verificar domínio', wait 10 s, reload, then check for the specific
        'Verified' badge element. Raises RuntimeError if the badge is not found.
        """
        try:
            page.get_by_role("button", name="Verificar domínio").click(timeout=8_000)
        except Exception as e:
            raise RuntimeError(f"[DOMAIN] 'Verificar domínio' button not found: {e}")

        _wait(10)

        try:
            page.reload(wait_until="domcontentloaded", timeout=15_000)
            _wait(2)
        except Exception as e:
            print(f"[DOMAIN] Page reload failed: {e}")

        self._shot(page, "domain_06_after_verify")

        # Target the specific inner div Facebook renders inside the verified badge span.
        # CSS classes taken from the live element observed in the UI.
        verified_selector = (
            "div.x1vvvo52.xw23nyj.x63nzvj.x1heor9g.xuxw1ft"
            ".x6ikm8r.x10wlt62.xlyipyv.x1h4wwuj.x1pd3egz.xeuugli"
        )
        try:
            # Use exact regex to avoid matching "Not Verified" which contains "Verified" as substring
            loc = page.locator(verified_selector).filter(has_text=re.compile(r'^\s*Verified\s*$')).first
            if loc.is_visible(timeout=5_000):
                print("[DOMAIN] Domain verified! (Verified badge found)")
                return True
        except Exception:
            pass

        raise DomainVerificationError(
            "[DOMAIN] Domain was not verified — 'Verified' badge not found after reload. "
            "Execution stopped."
        )

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

        # Click "Continuar" — wait indefinitely if reCAPTCHA blocks it.
        # Mirrors _wiz_identity_check: the button's aria-disabled is the
        # authoritative signal that Facebook has accepted the captcha. The
        # recaptcha iframe usually stays in the DOM after a successful solve,
        # so we don't gate on its presence.
        self._shot(page, "waba_pre_continuar")
        continuar_clicked = False
        while not continuar_clicked:
            try:
                btn = page.get_by_role("button", name="Continuar")
                if not btn.is_visible(timeout=2_000):
                    _wait(3)
                    continue
                aria = (btn.get_attribute("aria-disabled") or "").lower()
                if aria == "true" or btn.get_attribute("disabled"):
                    print("[WABA] Continuar disabled (reCAPTCHA or validation) — waiting for manual solve (no timeout)...")
                    _wait(3)
                    continue
                btn.click()
                continuar_clicked = True
                _wait(2)
                self._shot(page, "waba_continuar")
            except Exception:
                _wait(3)

        # Close the modal (try aria-label selectors, then Escape)
        for close_label in ("Fechar", "Close"):
            try:
                btn = page.get_by_role("button", name=close_label).first
                if btn.is_visible(timeout=2_000):
                    btn.click()
                    _wait(1)
                    print(f"[WABA] Modal closed ('{close_label}' button)")
                    break
            except Exception:
                pass
        else:
            try:
                page.keyboard.press("Escape")
                _wait(1)
                print("[WABA] Modal closed (Escape)")
            except Exception:
                pass

        # ── Verify WABA actually exists (with retries for slow proxies) ─────
        # Navigate back to the WABA settings page and check for a WABA entry.
        # Retries up to 3 times with increasing waits to handle slow proxies
        # or delayed Facebook propagation.
        retry_waits = [3, 5, 8]
        for attempt, wait_secs in enumerate(retry_waits, 1):
            _wait(wait_secs)
            try:
                page.goto(
                    f"https://business.facebook.com/latest/settings/whatsapp_account?business_id={self.business_id}",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                _wait(3)
                self._shot(page, f"waba_verify_exists_attempt{attempt}")

                # A WABA row in the table is a gridcell containing a heading element
                # (the WABA name). This is the most reliable signal regardless of
                # the account name or language.
                try:
                    rows = page.locator('[role="gridcell"] [role="heading"]')
                    count = rows.count()
                    if count > 0:
                        waba_name = rows.first.inner_text(timeout=3_000).strip()
                        print(f"[WABA] WABA creation confirmed on attempt {attempt}/{len(retry_waits)} — found {count} account row(s), first: '{waba_name}'")
                        return True
                except Exception:
                    pass

                print(f"[WABA] Retry {attempt}/{len(retry_waits)}: WABA not found on settings page yet")
            except Exception as e:
                print(f"[WABA] Retry {attempt}/{len(retry_waits)}: navigation failed: {e}")

        self._shot(page, "waba_not_found")
        print(f"[WABA] WABA not found after {len(retry_waits)} attempts — creation failed")
        return False

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
        for btn_name in ("Iniciar verificação", "Continuar", "Verificar", "Começar"):
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
                if any(phrase in body for phrase in (
                    "em análise", "verificado", "analisando suas informações",
                    "verificando suas informações", "processando seu envio",
                )):
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

        # ── Always dismiss the "Começar" intro modal first ─────────────────
        # After clicking any entry button (including "Continuar" on resume),
        # Facebook shows an intro modal with a "Começar" button that MUST be
        # clicked before the actual wizard step becomes active.
        # We retry up to 3 times with 2s gaps because the button may render late.
        _wait(2)  # give the modal time to render
        for _comecar_attempt in range(3):
            if _click_comecar(page):
                break
            _wait(2)

        # ── Detect current step when resuming ──────────────────────────────
        if resume_mode:
            step = self._detect_wizard_step(page)
            print(f"[VERIFY] Detected step after '{entry_button}': {step}")
        else:
            step = "start"

        return self._wizard_run_from(page, step)

    # ── Wizard step detection ──────────────────────────────────────────────────

    def _extract_wizard_title(self, page: Page) -> tuple[str, str]:
        """
        Extract the title and subtitle text from the currently visible wizard dialog.

        Tries the topmost visible [role="dialog"] and looks for:
          - Title: [role="heading"] or first short span[dir="auto"] (< 120 chars)
          - Subtitle: second distinct span[dir="auto"] that differs from the title

        Returns (title_lower, subtitle_lower). Both are empty string on failure.
        All locators use a short timeout so this fails fast when the DOM isn't ready.
        """
        try:
            dialogs = page.locator('[role="dialog"]').all()
            dialog = None
            for dlg in reversed(dialogs):  # last = topmost in stacking order
                try:
                    if dlg.is_visible(timeout=400):
                        dialog = dlg
                        break
                except Exception:
                    continue
            if dialog is None:
                return "", ""

            # --- Title extraction ---
            title = ""
            # Prefer a heading role inside the dialog
            try:
                heading = dialog.locator('[role="heading"]').first
                if heading.is_visible(timeout=800):
                    title = heading.inner_text(timeout=800).strip()
            except Exception:
                pass

            # Fallback: first short span[dir="auto"] (titles are short)
            if not title:
                try:
                    spans = dialog.locator('span[dir="auto"]').all()
                    for span in spans:
                        try:
                            t = span.inner_text(timeout=500).strip()
                            if t and len(t) < 120:
                                title = t
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            if not title:
                return "", ""

            # --- Subtitle extraction ---
            subtitle = ""
            try:
                spans = dialog.locator('span[dir="auto"]').all()
                for span in spans:
                    try:
                        t = span.inner_text(timeout=500).strip()
                        if t and t != title and len(t) < 120:
                            subtitle = t
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            return title.lower(), subtitle.lower()
        except Exception:
            return "", ""

    def _detect_wizard_step(self, page: Page) -> str:
        """
        Identify the current wizard step from visible page text.
        Falls back to LLM vision analysis when keywords are ambiguous.

        Returns one of:
            start | entity_type | registration | cnpj_input | cnpj_list |
            add_company_data | document_upload | method_selection |
            phone_entry | otp_entry | complete | unknown
        """
        # Prefer the dialog text so we get modal content (FB renders wizards in dialogs).
        # IMPORTANT: FB can stack two dialogs — the background wizard + the Começar intro
        # modal on top. We scan ALL visible dialogs first and return "start" immediately
        # if the Começar modal is detected, so we never misread the background content.
        text = ""
        try:
            dialogs = page.locator('[role="dialog"]').all()
            for dlg in dialogs:
                try:
                    if not dlg.is_visible(timeout=400):
                        continue
                    dlg_text = dlg.inner_text(timeout=1_500).lower()
                    # Começar intro modal is uniquely identified by "ações necessárias" + button
                    if "começar" in dlg_text and ("ações necessárias" in dlg_text or "conexão legítima" in dlg_text):
                        print("[VERIFY] Step detection: Começar intro modal detected → returning 'start'")
                        return "start"
                    if dlg_text and not text:
                        text = dlg_text  # use first non-empty visible dialog as fallback
                except Exception:
                    continue
        except Exception:
            pass

        # ── Tier 1: Title-based detection ──────────────────────────────────────
        # Read the wizard modal's title/subtitle spans before falling back to
        # full-text keyword matching.  Fails fast (short timeouts) so it never
        # slows down the detection when the title element isn't present.
        wiz_title, wiz_subtitle = self._extract_wizard_title(page)
        if wiz_title:
            for t_pattern, s_pattern, step_name in _WIZARD_TITLE_MAP:
                if t_pattern not in wiz_title:
                    continue
                if s_pattern is not None and s_pattern not in wiz_subtitle:
                    continue
                print(f"[VERIFY] Tier 1 title match: title={wiz_title!r}, subtitle={wiz_subtitle!r} → {step_name}")
                return step_name
            print(f"[VERIFY] Tier 1: title found ({wiz_title!r}) but no mapping matched — falling to Tier 2")

        if not text:
            try:
                text = page.inner_text("body")
            except Exception:
                return "unknown"

        t = text.lower()
        print(f"[VERIFY] Tier 2 keyword detection text (first 500): {t[:500]!r}")

        if any(k in t for k in ("agradecemos", "verificação foi concluída", "em análise",
                                "analisando suas informações", "verificando suas informações",
                                "processando seu envio")):
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
        if "selecionar um país" in t or "localização da organização" in t:
            return "country_selection"
        if "ajude-nos a confirmar que é você" in t:
            return "identity_check"

        print(f"[VERIFY] Keyword matching failed (text len={len(t)}) — falling back to LLM")
        llm_result = self._detect_step_llm(page)
        if llm_result != "unknown":
            return llm_result

        # Both keyword and LLM failed — try MCP as last resort
        mcp_result = self._mcp_recover(page, "detect wizard step — return exactly one keyword: "
                                              "start | entity_type | registration | cnpj_input | cnpj_list | "
                                              "add_company_data | document_upload | method_selection | "
                                              "phone_entry | otp_entry | complete | country_selection | identity_check | unknown")
        valid = {
            "start", "entity_type", "registration", "cnpj_input", "cnpj_list",
            "add_company_data", "document_upload", "method_selection",
            "country_selection", "identity_check",
            "phone_entry", "otp_entry", "complete",
        }
        if mcp_result and mcp_result.lower().strip() in valid:
            return mcp_result.lower().strip()
        return "unknown"

    def _detect_step_llm(self, page: Page) -> str:
        """
        LLM vision fallback: sends a screenshot to Claude and asks which
        wizard step is visible. Requires ANTHROPIC_API_KEY in env.
        """
        client = _get_claude_client()
        if not client:
            print("[VERIFY] No ANTHROPIC_API_KEY — LLM step detection unavailable")
            return "unknown"

        try:
            img_b64 = base64.b64encode(page.screenshot()).decode()
            prompt = (
                "You are analysing a Facebook Business Verification wizard "
                "screenshot (in Portuguese). Identify the current step and "
                "reply with ONLY one keyword from this list:\n"
                "start, entity_type, registration, cnpj_input, cnpj_list, "
                "add_company_data, document_upload, method_selection, "
                "phone_entry, otp_entry, complete, country_selection, identity_check, unknown\n\n"
                "Examples:\n"
                "  document_upload   — file upload area visible\n"
                "  method_selection  — domain/SMS/call radio buttons visible\n"
                "  entity_type       — 'Empresa individual' choice visible\n"
                "  complete          — 'Processando seu envio', 'Agradecemos o envio', 'Em análise', 'Analisando suas informações', or 'Verificando suas informações' text visible\n"
                "  start             — intro screen with 'Começar' button listing verification steps\n"
                "  country_selection — 'Selecionar um país' dropdown with Brasil selected\n"
                "  identity_check    — 'Ajude-nos a confirmar que é você' with Avançar button\n"
            )
            response = client.messages.create(
                model=config.CLAUDE_FAST_MODEL,
                max_tokens=20,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": prompt},
                ]}],
            )
            raw_text = response.content[0].text.strip().lstrip("`").rstrip("`")
            print(f"[VERIFY] Claude step raw response: {raw_text!r}")
            if not raw_text:
                print("[VERIFY] Claude step response was empty — returning unknown")
                return "unknown"
            raw = raw_text.lower().split()[0]
            valid = {
                "start", "entity_type", "registration", "cnpj_input", "cnpj_list",
                "add_company_data", "document_upload", "method_selection",
                "phone_entry", "otp_entry", "complete", "country_selection", "identity_check",
            }
            result = raw if raw in valid else "unknown"
            if result == "unknown":
                print(f"[VERIFY] Claude returned unrecognised step '{raw}' — treating as unknown")
            print(f"[VERIFY] Claude step detection → {result}")
            return result
        except Exception as e:
            print(f"[VERIFY] Claude step detection failed: {e}")
            return "unknown"

    def _mcp_recover(self, page: Page, context: str) -> str | None:
        """
        Browser Use MCP fallback: when both keyword detection and Claude LLM fail,
        call the MCP server to inspect the page and figure out what to do.

        Args:
            context: What we're trying to accomplish (e.g. "detect wizard step",
                     "find and click verification method option").

        Returns the step name or action taken as a string, or None if MCP is
        disabled or also fails.

        Enabled when BROWSER_USE_MCP_URL is set in .env / config.
        """
        mcp_url = getattr(config, "BROWSER_USE_MCP_URL", "")
        if not mcp_url:
            return None

        try:
            import urllib.request
            import json as _json

            # Build a minimal page snapshot: screenshot + text + URL
            img_b64 = base64.b64encode(page.screenshot()).decode()
            try:
                page_text = page.inner_text("body")[:2000]
            except Exception:
                page_text = ""

            payload = _json.dumps({
                "context": context,
                "page_url": page.url,
                "page_text": page_text,
                "screenshot_b64": img_b64,
                "valid_steps": [
                    "start", "entity_type", "registration", "cnpj_input", "cnpj_list",
                    "add_company_data", "document_upload", "method_selection",
                    "phone_entry", "otp_entry", "complete", "unknown",
                ],
            }).encode()

            req = urllib.request.Request(
                mcp_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = _json.loads(resp.read().decode())

            result = body.get("result") or body.get("step") or body.get("action")
            print(f"[MCP] Recovery result for '{context}': {result!r}")
            return str(result) if result else None
        except Exception as e:
            print(f"[MCP] Recovery failed ({context}): {e}")
            return None

    def _llm_fix_form(self, page: Page) -> str:
        """
        Ask Claude to look at the current form screenshot and identify
        which required field is empty or invalid, then attempt to fill it.

        Returns a short description of the fix applied, or "" if nothing done.
        """
        client = _get_claude_client()
        if not client:
            return ""
        try:
            img_b64 = base64.b64encode(page.screenshot()).decode()
            prompt = (
                "This is a Facebook Business Portfolio creation form in Portuguese. "
                "The 'Criar' (Create) button is disabled. "
                "Look at the form and identify which required field is empty or has an error. "
                "Reply with a JSON object like: "
                '{"field": "<label name in Portuguese>", "value": "<what to fill>"} '
                'or {"field": null} if you cannot determine the issue. '
                "Only reply with the JSON, nothing else."
            )
            response = client.messages.create(
                model=config.CLAUDE_FAST_MODEL,
                max_tokens=80,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": prompt},
                ]}],
            )
            raw = response.content[0].text.strip()
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
        Ask Claude to look at the current screenshot and return the exact
        visible text of the button/link that achieves *goal*.

        Returns the button text string, or "" if LLM can't determine it.
        Use this when selectors fail and the bot doesn't know what to click next.
        """
        client = _get_claude_client()
        if not client:
            return ""
        try:
            img_b64 = base64.b64encode(page.screenshot()).decode()
            prompt = (
                f"You are helping automate a Facebook Business Manager page in Portuguese.\n"
                f"Goal: {goal}\n"
                f"Look at the screenshot and return ONLY the exact visible text of the "
                f"button or link I should click to achieve this goal. "
                f"If there is a modal/dialog blocking the page, return the text of the "
                f"button to dismiss it first. "
                f"Reply with just the button text, nothing else. "
                f"If you cannot determine it, reply with: UNKNOWN"
            )
            response = client.messages.create(
                model=config.CLAUDE_FAST_MODEL,
                max_tokens=30,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": prompt},
                ]}],
            )
            result = response.content[0].text.strip()
            if result.upper() == "UNKNOWN" or not result:
                return ""
            # Reject multi-sentence responses — the model should only return the button label
            if len(result) > 60 or result.count(" ") > 5:
                print(f"[Claude] find_action → response too long ({len(result)} chars), ignoring: {result[:80]!r}")
                return ""
            print(f"[Claude] find_action → '{result}'")
            return result
        except Exception as e:
            print(f"[Claude] find_action failed: {e}")
            return ""

    def _llm_fill_field(self, page: Page, value: str, goal: str) -> bool:
        """
        LLM-guided field fill for when Playwright locators fail.

        1. Screenshot → ask LLM for the exact placeholder/label text of the input.
        2. If NOT_VISIBLE → ask LLM what to click to reveal the input, click it, retry once.
        3. Fill using placeholder or label.

        Returns True if the field was successfully filled, False otherwise.
        """
        client = _get_claude_client()
        if not client:
            return False

        def _ask_identifier() -> str:
            try:
                img_b64 = base64.b64encode(page.screenshot()).decode()
                prompt = (
                    f"Look at this Facebook Business Manager page (in Portuguese).\n"
                    f"I need to type the value '{value}' into the {goal} input field.\n"
                    f"What is the EXACT placeholder text, label text, or nearby visible "
                    f"text of the input I should fill?\n"
                    f"Reply with ONLY that text, nothing else.\n"
                    f"If no such input is visible on screen (e.g. a dialog has not "
                    f"opened yet), reply with exactly: NOT_VISIBLE"
                )
                response = client.messages.create(
                    model=config.CLAUDE_FAST_MODEL,
                    max_tokens=30,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                        {"type": "text", "text": prompt},
                    ]}],
                )
                return response.content[0].text.strip()
            except Exception as e:
                print(f"[Claude] fill_field identifier failed: {e}")
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
        PRE_UPLOAD = ["start", "entity_type", "registration", "cnpj_input", "cnpj_list", "add_company_data",
                      "country_selection", "identity_check"]
        current = initial_step

        for iteration in range(18):  # safety cap (3 add_company_data sub-steps + buffer)
            # Dismiss the Começar intro modal before acting on any step.
            # It can appear/reappear at any point and blocks all interactions.
            if current in ("start", "unknown", "method_selection"):
                _click_comecar(page)

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
                "start":             self._wiz_start,
                "entity_type":       self._wiz_entity_type,
                "registration":      self._wiz_registration,
                "cnpj_input":        self._wiz_cnpj_input,
                "cnpj_list":         self._wiz_cnpj_list,
                "add_company_data":  self._wiz_add_company_data,
                "country_selection": self._wiz_advance,
                "identity_check":    self._wiz_identity_check,
            }.get(current)

            if handler:
                handler(page)
                _wait(1.5)
            else:
                # unknown — try MCP recovery first, then nudge with Avançar / Começar
                print("[VERIFY] Unknown step — trying MCP recovery")
                mcp_action = self._mcp_recover(
                    page,
                    "The wizard is on an unknown step. Identify what button or element to click "
                    "to advance to the next step, and return its exact visible text. "
                    "If you cannot determine what to do, return 'Avançar'."
                )
                nudge_buttons = [mcp_action] if mcp_action else []
                nudge_buttons += ["Avançar", "Começar"]
                for btn_name in nudge_buttons:
                    if not btn_name:
                        continue
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

    def _wiz_identity_check(self, page: Page):
        """Wait indefinitely for user to solve reCAPTCHA, then click Avançar.

        Facebook uses aria-disabled="true" (not the HTML disabled attribute)
        on the Avançar button while the captcha is unsolved.  We poll until
        aria-disabled disappears or becomes "false", then click.
        """
        print("[VERIFY] reCAPTCHA detected — waiting for manual solve (no timeout)...")
        while True:
            try:
                btn = page.get_by_role("button", name="Avançar")
                if not btn.is_visible(timeout=2_000):
                    _wait(3)
                    continue
                aria = btn.get_attribute("aria-disabled") or ""
                if aria.lower() == "true":
                    _wait(3)
                    continue
                # Button is clickable — captcha was solved
                btn.click()
                print("[VERIFY] reCAPTCHA solved — Avançar clicked. Waiting for page transition...")
                # Wait until the captcha title disappears so the wizard loop
                # doesn't re-detect identity_check on the stale page.
                for _ in range(30):  # up to ~30s
                    _wait(1)
                    new_title, _ = self._extract_wizard_title(page)
                    if "ajude-nos a confirmar" not in new_title:
                        print("[VERIFY] reCAPTCHA page transitioned — continuing wizard.")
                        return
                print("[VERIFY] reCAPTCHA page did not transition after 30s — continuing anyway.")
                return
            except Exception:
                _wait(3)

    def _wiz_advance(self, page: Page):
        """Generic advance step — click Avançar (button or link)."""
        for selector in (
            'button:has-text("Avançar")',
            '[role="button"]:has-text("Avançar")',
            'a:has-text("Avançar")',
        ):
            try:
                page.locator(selector).first.click(timeout=3_000)
                _wait(1)
                return
            except Exception:
                pass
        # Fallback: get_by_role
        try:
            page.get_by_role("button", name="Avançar").click(timeout=3_000)
            _wait(1)
        except Exception:
            pass

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
            if self._sms_activation_id is not None:
                # Number already bought (e.g. wizard re-entered after business_info update)
                # — just fill the field with the existing number and move on.
                print(f"[VERIFY] Reusing existing virtual number: {self._sms_phone_fb}")
                self._fill_contact_phone(page, self._sms_phone_fb)
            else:
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
                        tel_fmt, pdf_path = self.gerador.change_phone(run_id, phone_pdf)
                        # Keep self.run in sync so _validate_pdf_data checks the virtual phone
                        self.run["telefone_digits"] = re.sub(r"\D", "", tel_fmt)
                        _log_phone_match(pdf_path, phone_fb, tel_fmt)
                        print(f"[VERIFY] PDF updated with virtual phone — wizard={phone_fb} documento={tel_fmt}")
                    except Exception as e:
                        print(f"[VERIFY] PDF phone update failed: {e}")
                    # Update website HTML and Facebook business_info with the new phone
                    try:
                        self.gerador.change_website_phone(run_id, phone_pdf)
                    except Exception as e:
                        print(f"[VERIFY] Website phone update failed: {e}")
                    self._update_business_phone(page, phone_pdf)
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
                        tel_fmt, pdf_path = self.gerador.change_phone(run_id, phone_pdf)
                        # Keep self.run in sync so _validate_pdf_data checks the virtual phone
                        self.run["telefone_digits"] = re.sub(r"\D", "", tel_fmt)
                        _log_phone_match(pdf_path, phone_fb, tel_fmt)
                        print(f"[VERIFY] PDF updated with virtual phone — wizard={phone_fb} documento={tel_fmt}")
                    except Exception as e:
                        print(f"[VERIFY] PDF phone update failed: {e}")
                    # Update website HTML and Facebook business_info with the new phone
                    try:
                        self.gerador.change_website_phone(run_id, phone_pdf)
                    except Exception as e:
                        print(f"[VERIFY] Website phone update failed: {e}")
                    self._update_business_phone(page, phone_pdf)
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

        # Dismiss any "Começar" modal that may still be blocking.
        # If _click_comecar fails and step detection still returns "start",
        # retry up to 5 times with increasing waits.  If still stuck after
        # all retries, raise a specific error — never fall through to doc upload
        # with the Começar modal still present.
        next_step = "start"
        for _comecar_attempt in range(5):
            clicked = _click_comecar(page)
            _wait(1.5)
            next_step = self._detect_wizard_step(page)
            if next_step != "start":
                break
            wait_s = 2 + _comecar_attempt  # 2, 3, 4, 5, 6 seconds
            print(f"[VERIFY] Começar modal still present (attempt {_comecar_attempt + 1}/5, clicked={clicked}) — waiting {wait_s}s")
            _wait(wait_s)

        self._shot(page, f"mf_post_sms_{next_step}")
        print(f"[VERIFY] Post-SMS step: {next_step}")

        if next_step == "start":
            shot = self._shot(page, "comecar_modal_stuck")
            raise VerificationStepError(
                "comecar_modal_stuck",
                "Modal 'Começar' não foi dispensado após 5 tentativas — não é possível continuar o wizard",
                page.url, page.content(), screenshot_path=shot or "",
            )

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
        self._validate_pdf_data(pdf_path)
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
        domain_not_verified_count = 0
        for _ in range(10):
            _wait(2)
            step = self._detect_wizard_step(page)
            self._shot(page, f"post_domain_{step}")
            print(f"[VERIFY] Post-domain step: {step}")

            if step == "complete":
                return self._complete_verification(page)

            if step == "domain_confirmed":
                # Two sub-states share this step name:
                #  A) "ainda não foi verificado" → button is "Verificar" (triggers check)
                #  B) domain already verified    → button is "Avançar"
                try:
                    body_now = page.inner_text("body").lower()
                except Exception:
                    body_now = ""
                if "ainda não foi verificado" in body_now:
                    domain_not_verified_count += 1
                    if domain_not_verified_count >= 3:
                        # Domain genuinely failing — go back and switch to SMS
                        print("[VERIFY] Domain stuck unverified after 3 tries — falling back to SMS via Voltar")
                        try:
                            page.get_by_role("button", name="Voltar").click(timeout=5_000)
                            _wait(2)
                        except Exception:
                            pass
                        return self._wizard_method_first(page)
                    # Trigger the domain ownership check
                    try:
                        page.get_by_role("button", name="Verificar").click(timeout=5_000)
                        print("[VERIFY] domain_confirmed: clicked 'Verificar' to trigger check")
                        _wait(4)
                    except Exception as e:
                        print(f"[VERIFY] domain_confirmed 'Verificar' failed: {e}")
                else:
                    # Domain confirmed — advance to next step
                    try:
                        page.get_by_role("button", name="Avançar").click(timeout=5_000)
                        print("[VERIFY] domain_confirmed: clicked 'Avançar'")
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
                if any(phrase in body for phrase in (
                    "agradecemos", "em análise", "analisando suas informações",
                    "verificando suas informações", "processando seu envio",
                )):
                    print("[VERIFY] Completion text found after domain")
                    return True
            except Exception:
                pass

        return self._complete_verification(page)

    def _validate_pdf_data(self, pdf_path: str) -> None:
        """
        Extract text from the PDF and assert it contains the expected phone
        and CNPJ from self.run. Raises RuntimeError with details on mismatch.
        """
        import pdfplumber

        expected_phone = re.sub(r"\D", "", self.run.get("telefone_digits", ""))
        expected_cnpj  = re.sub(r"\D", "", self.run.get("cnpj_digits", ""))

        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception as e:
            raise RuntimeError(f"[PDF] Could not read PDF for validation: {e}")

        pdf_digits = re.sub(r"\D", "", text)

        if expected_phone and expected_phone not in pdf_digits:
            raise RuntimeError(
                f"[PDF] Phone mismatch — expected {expected_phone} not found in document. "
                "The PDF was not updated after the last phone change."
            )
        if expected_cnpj and expected_cnpj not in pdf_digits:
            raise RuntimeError(
                f"[PDF] CNPJ mismatch — expected {expected_cnpj} not found in document."
            )
        print(f"[PDF] Document validated — phone and CNPJ present.")

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
        _wait(1.5)  # allow file input to render after doc type selection

        self._shot(page, "verify_before_upload")
        if not self._wiz_set_pdf(page, pdf_path):
            # Before giving up, check if the wizard already advanced past doc upload
            _wait(1)
            actual = self._detect_wizard_step(page)
            if actual in ("method_selection", "phone_entry", "otp_entry", "complete"):
                print(f"[VERIFY] No file input found but wizard is at '{actual}' — treating upload as done")
                return True
            self._save_html(page, "verify_upload_fail")
            shot_path = self._shot(page, "document_upload_failed")
            raise VerificationStepError(
                "document_upload_failed",
                f"Não foi possível fazer upload do PDF CNPJ — input de arquivo não encontrado ou inacessível (pdf: {pdf_path})",
                page.url,
                page.content(),
                screenshot_path=shot_path or "",
            )

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
        Tries label-based, raw input[type=file], force-set on hidden inputs,
        and file-chooser interception (for custom upload buttons).
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

        # Strategy 4: file-chooser interception — Facebook uses a custom upload
        # button that hides the actual <input type=file>. Clicking the visible
        # button triggers a file-chooser event which we intercept here.
        upload_button_candidates = [
            page.get_by_role("button", name="Selecionar arquivo"),
            page.get_by_role("button", name="Carregar arquivo"),
            page.get_by_role("button", name="Escolher arquivo"),
            page.get_by_role("button", name="Upload file"),
            page.get_by_role("button", name="Choose file"),
            page.locator("label").filter(has_text="Selecionar"),
            page.locator("label").filter(has_text="Carregar"),
            page.locator('[aria-label*="arquivo"]'),
            page.locator('[aria-label*="file"]'),
        ]
        for btn in upload_button_candidates:
            try:
                if not btn.is_visible(timeout=1_000):
                    continue
                with page.expect_file_chooser(timeout=5_000) as fc_info:
                    btn.click()
                fc_info.value.set_files(pdf_path)
                print(f"[UPLOAD] File set via file-chooser interception")
                return True
            except Exception:
                continue

        return False

    def _llm_detect_methods(self, page: Page) -> dict:
        """
        Screenshot → Claude: ask which verification methods are currently visible.
        Returns e.g. {"domain": True, "sms": True, "call": False}.
        Falls back to all-False dict on any error or missing API key.
        """
        client = _get_claude_client()
        if not client:
            print("[VERIFY] No Anthropic client — LLM method detection skipped")
            return {"domain": False, "sms": False, "call": False, "_no_client": True}
        try:
            img_b64 = base64.b64encode(page.screenshot()).decode()
            prompt = (
                "You are analysing a Facebook Business Verification wizard "
                "screenshot (in Portuguese).\n"
                "Look at the verification method options visible on screen "
                "(radio buttons or list items).\n"
                "Reply with ONLY a JSON object with boolean keys, nothing else:\n"
                '{"domain": <true if a domain/DNS verification option is visible>, '
                '"sms": <true if an SMS text message option is visible>, '
                '"call": <true if a phone call option is visible>}'
            )
            response = client.messages.create(
                model=config.CLAUDE_FAST_MODEL,
                max_tokens=40,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": prompt},
                ]}],
            )
            raw = response.content[0].text.strip()
            print(f"[Claude] detect_methods raw response: {raw!r}")
            # Strip markdown code fences if Claude wrapped the JSON in ```json ... ```
            if raw.startswith("```"):
                raw = raw.split("```")[-2] if "```" in raw[3:] else raw.lstrip("`")
                raw = raw.lstrip("json").strip()
            if not raw:
                print("[Claude] detect_methods: empty response — treating as no methods")
                return {"domain": False, "sms": False, "call": False}
            result = json.loads(raw)
            print(f"[Claude] detect_methods → {result}")
            return {
                "domain": bool(result.get("domain")),
                "sms":    bool(result.get("sms")),
                "call":   bool(result.get("call")),
            }
        except Exception as e:
            print(f"[Claude] detect_methods failed: {e}")
            return {"domain": False, "sms": False, "call": False, "_error": str(e)}

    def _click_method_option(self, page: Page, target: str) -> bool:
        """
        Click a verification method radio/option by DOM selectors.
        Returns True if an element was found and clicked.

        Tries multiple selectors per method so minor DOM changes don't break it.
        """
        import re as _re
        selector_factories = {
            "domain": [
                lambda: page.locator('[role="radio"]').filter(has_text=_re.compile(r'domínio', _re.IGNORECASE)).first,
                lambda: page.get_by_text("Verificação de domínio", exact=False).first,
            ],
            "sms": [
                lambda: page.locator('[role="radio"]').filter(has_text=_re.compile(r'SMS', _re.IGNORECASE)).first,
                lambda: page.get_by_text("Mensagem de texto", exact=False).first,
            ],
            "call": [
                lambda: page.locator('[role="radio"]').filter(has_text=_re.compile(r'ligação|telefônica', _re.IGNORECASE)).first,
                lambda: page.get_by_text("Ligação telefônica", exact=False).first,
            ],
        }
        for sel_fn in selector_factories.get(target, []):
            try:
                el = sel_fn()
                if el.is_visible(timeout=2_000):
                    el.scroll_into_view_if_needed()
                    el.click()
                    print(f"[VERIFY] _click_method_option: clicked '{target}' via DOM selector")
                    return True
            except Exception:
                continue
        return False

    def _wiz_select_method(self, page: Page) -> str:
        """
        Wait for the method selection screen to fully load, then select the best
        available verification method (domain > SMS > call) and advance.

        Primary path: DOM-based selectors — no LLM API calls needed.
        Fallback: LLM vision when all DOM selectors fail.

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

        # ── DOM-first: try domain → sms → call ─────────────────────────────
        target = None
        for method in ("domain", "sms", "call"):
            if self._click_method_option(page, method):
                target = method
                break

        # ── LLM fallback: only when all DOM selectors failed ───────────────
        if target is None:
            print("[VERIFY] DOM selectors found no method option — falling back to LLM")
            self._shot(page, "method_select_dom_failed")
            methods = self._llm_detect_methods(page)
            for method in ("domain", "sms", "call"):
                if methods.get(method):
                    goal = {
                        "domain": "select the domain verification option (radio button or list item)",
                        "sms":    "select the SMS text message verification option (radio button or list item)",
                        "call":   "select the phone call verification option (radio button or list item)",
                    }[method]
                    option_text = self._llm_find_action(page, goal)
                    if option_text:
                        try:
                            page.get_by_text(option_text, exact=False).first.click(timeout=4_000)
                            target = method
                            print(f"[LLM] Clicked method option via LLM: '{option_text}'")
                            break
                        except Exception as e:
                            print(f"[LLM] Could not click '{option_text}': {e}")

            # Last resort: first radio
            if target is None:
                try:
                    page.locator('[role="radio"]').first.click(timeout=2_000)
                    target = "sms"
                    print("[VERIFY] Last resort: clicked first radio, assuming SMS")
                except Exception:
                    pass

        _wait(0.5)
        self._shot(page, f"method_select_after_{target}")

        # ── Click Avançar (DOM — no LLM needed) ────────────────────────────
        try:
            page.get_by_role("button", name="Avançar").click(timeout=5_000)
        except Exception as e:
            print(f"[VERIFY] Avançar click failed: {e}")
        _wait(1.5)

        # Some flows show a second confirmation Avançar
        try:
            page.get_by_role("button", name="Avançar").click(timeout=2_000)
            _wait(1.5)
        except Exception:
            pass

        self._shot(page, f"method_select_done_{target}")
        return target or "sms"

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
                    tel_fmt, pdf_path = self.gerador.change_phone(run_id, phone_pdf)
                    # Keep self.run in sync so _validate_pdf_data checks the virtual phone
                    self.run["telefone_digits"] = re.sub(r"\D", "", tel_fmt)
                    _log_phone_match(pdf_path, phone_fb, tel_fmt)
                    print(f"[VERIFY] PDF updated — wizard={phone_fb} documento={tel_fmt}")
                except Exception as e:
                    print(f"[VERIFY] PDF update failed: {e}")
                    self.sms.cancel(activation_id)
                    activation_id = None
                    self._sms_activation_id = None
                    continue

                # Update website HTML and Facebook business_info with the new phone
                try:
                    self.gerador.change_website_phone(run_id, phone_pdf)
                except Exception as e:
                    print(f"[VERIFY] Website phone update failed: {e}")
                self._update_business_phone(page, phone_pdf)

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
                self._validate_pdf_data(pdf_path)
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
        shot_path = self._shot(page, "sms_all_attempts_exhausted")
        raise VerificationStepError(
            "sms_otp_exhausted",
            f"Todos os {self.sms_max_attempts} tentativas de SMS falharam — código OTP não recebido em nenhuma tentativa",
            page.url,
            page.content(),
            screenshot_path=shot_path or "",
        )

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
            "Analisando suas informações",
            "Verificando suas informações",
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
