"""
SMS24H OTP service client.
Used to buy a Brazilian virtual phone number, wait for a Facebook verification
SMS or call code, then cancel if it times out.
"""
import re
import time
import requests

SMS24H_BASE_URL = "https://api.sms24h.org/stubs/handler_api"


class SMS24HService:
    def __init__(self, api_key: str, country: str = "73", service: str = "fb"):
        """
        api_key  : SMS24H API key
        country  : SMS24H country code (73 = Brazil)
        service  : SMS24H service code (fb = Facebook, wa = WhatsApp)
        """
        self.api_key = api_key
        self.country = country
        self.service = service

    # ── internal ─────────────────────────────────────────────────────────────

    def _req(self, params: dict) -> str:
        params["api_key"] = self.api_key
        try:
            r = requests.get(SMS24H_BASE_URL, params=params, timeout=30)
            return r.text.strip()
        except Exception as e:
            print(f"[SMS24H] request error: {e}")
            return ""

    # ── public ────────────────────────────────────────────────────────────────

    def buy_number(self) -> tuple[str | None, str | None]:
        """
        Buy a virtual number.
        Returns (activation_id, full_phone_with_country_code) or (None, None).
        full_phone example: '5511987654321'  (55 + DDD + number)
        """
        resp = self._req({
            "action": "getNumber",
            "service": self.service,
            "country": self.country,
        })
        print(f"[SMS24H] buy_number → {resp}")
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
            print(f"[SMS24H] status({activation_id}) → {resp}")

            if resp.startswith("STATUS_OK"):
                raw = resp.split(":", 1)[1] if ":" in resp else ""
                otp = re.sub(r"\D", "", raw)
                if otp:
                    return otp

            elif resp == "STATUS_CANCEL":
                print(f"[SMS24H] activation {activation_id} was cancelled by provider")
                return None

            time.sleep(12)

        # Timed out — cancel for refund
        print(f"[SMS24H] timeout for {activation_id}, cancelling")
        self.cancel(activation_id)
        return None

    def cancel(self, activation_id: str):
        """Cancel an activation (triggers refund on SMS24H side)."""
        self._req({"action": "setStatus", "id": activation_id, "status": "8"})

    def get_balance(self) -> str | None:
        """Return raw balance string from API or None on error."""
        resp = self._req({"action": "getBalance"})
        if resp.startswith("ACCESS_BALANCE:"):
            return resp.split(":", 1)[1]
        return None

    # ── phone helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def to_facebook_format(full_phone: str) -> str:
        """
        Convert a raw phone from SMS24H (e.g. '5571976049747') to the format
        Facebook accepts in the wizard phone field (BR+55 country already set):
        only the country code 55 is removed; the mandatory 9th digit of
        Brazilian mobile numbers is kept (DDD 2 + 9 + 8 = 11 local digits).
        Returns e.g. '71976049747'.
        """
        digits = re.sub(r"\D", "", full_phone)
        if digits.startswith("55"):
            digits = digits[2:]
        return digits

    @staticmethod
    def to_pdf_format(full_phone: str) -> str:
        """
        Same normalization but only removes country code.
        Used when calling the Gerador change-phone endpoint.
        Returns local digits without country code (10 or 11 digits).
        """
        digits = re.sub(r"\D", "", full_phone)
        if digits.startswith("55"):
            digits = digits[2:]
        return digits
