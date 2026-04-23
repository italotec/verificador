"""
HeroSMS OTP service client.

HeroSMS is 100% SMS-Activate API compatible — same endpoints, same response
format.  Only the base URL and API key differ from SMS24H.
"""
import re
import time
import requests

HEROSMS_BASE_URL = "https://hero-sms.com/stubs/handler_api.php"


class HeroSMSService:
    def __init__(self, api_key: str, country: str = "73", service: str = "fb",
                 max_price: str = ""):
        """
        api_key   : HeroSMS API key
        country   : country code (73 = Brazil, same as SMS-Activate)
        service   : service code (fb = Facebook)
        max_price : maximum price per number in USD (empty = no limit)
        """
        self.api_key = api_key
        self.country = country
        self.service = service
        self.max_price = max_price

    # ── internal ─────────────────────────────────────────────────────────────

    def _req(self, params: dict) -> str:
        params["api_key"] = self.api_key
        try:
            r = requests.get(HEROSMS_BASE_URL, params=params, timeout=30)
            return r.text.strip()
        except Exception as e:
            print(f"[HEROSMS] request error: {e}")
            return ""

    # ── public ────────────────────────────────────────────────────────────────

    def buy_number(self) -> tuple[str | None, str | None]:
        """
        Buy a virtual number.
        Returns (activation_id, full_phone_with_country_code) or (None, None).
        """
        params = {
            "action": "getNumber",
            "service": self.service,
            "country": self.country,
        }
        if self.max_price:
            params["maxPrice"] = self.max_price
            params["fixedPrice"] = "true"
        print(f"[HEROSMS] buy_number params → {params}")
        resp = self._req(params)
        print(f"[HEROSMS] buy_number → {resp}")
        if resp.startswith("ACCESS_NUMBER"):
            _, activation_id, full_phone = resp.split(":", 2)
            return activation_id, full_phone
        return None, None

    def wait_for_otp(self, activation_id: str, timeout: int = 180) -> str | None:
        """
        Poll until an OTP arrives or *timeout* seconds pass.
        Returns the numeric OTP string or None if timed out / cancelled.
        On timeout, cancels the activation automatically (refund).
        """
        start = time.time()
        while time.time() - start < timeout:
            resp = self._req({"action": "getStatus", "id": activation_id})
            print(f"[HEROSMS] status({activation_id}) → {resp}")

            if resp.startswith("STATUS_OK"):
                raw = resp.split(":", 1)[1] if ":" in resp else ""
                otp = re.sub(r"\D", "", raw)
                if otp:
                    return otp

            elif resp == "STATUS_CANCEL":
                print(f"[HEROSMS] activation {activation_id} was cancelled by provider")
                return None

            time.sleep(12)

        print(f"[HEROSMS] timeout for {activation_id}, cancelling")
        self.cancel(activation_id)
        return None

    def cancel(self, activation_id: str):
        """Cancel an activation (triggers refund)."""
        self._req({"action": "setStatus", "id": activation_id, "status": "8"})

    def get_balance(self) -> str | None:
        """Return raw balance string from API or None on error."""
        resp = self._req({"action": "getBalance"})
        if resp.startswith("ACCESS_BALANCE:"):
            return resp.split(":", 1)[1]
        return None

    # ── phone helpers (identical to SMS24H) ──────────────────────────────────

    @staticmethod
    def to_facebook_format(full_phone: str) -> str:
        digits = re.sub(r"\D", "", full_phone)
        if digits.startswith("55"):
            digits = digits[2:]
        return digits

    @staticmethod
    def to_pdf_format(full_phone: str) -> str:
        digits = re.sub(r"\D", "", full_phone)
        if digits.startswith("55"):
            digits = digits[2:]
        return digits
