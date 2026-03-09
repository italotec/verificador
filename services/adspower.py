"""
AdsPower local API client.
Handles profile CRUD, group management, and browser open/close.
"""
import time
import requests

# Desktop Windows Chrome UA injected into every new profile so FB never sees mobile
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class AdsPowerClient:
    def __init__(self, base_url: str = "http://local.adspower.net:50325"):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self._last_request_at: float = 0.0  # epoch seconds

    # ── low-level ────────────────────────────────────────────────────────────

    def _throttle(self, min_interval: float = 1.1):
        """Ensure at least *min_interval* seconds between consecutive API calls."""
        elapsed = time.time() - self._last_request_at
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_at = time.time()

    def _get(self, path: str, **params):
        self._throttle()
        r = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("code") not in (0,):
            raise RuntimeError(f"AdsPower error [{path}]: {data.get('msg')} | params={params}")
        return data.get("data", {})

    def _post(self, path: str, body: dict):
        r = self.session.post(f"{self.base}{path}", json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("code") not in (0,):
            raise RuntimeError(f"AdsPower error [{path}]: {data.get('msg')} | body={body}")
        return data.get("data", {})

    # ── groups ────────────────────────────────────────────────────────────────

    def get_group_id(self, group_name: str) -> str:
        """Return group_id for *group_name*, creating the group if it doesn't exist."""
        data = self._get("/api/v1/group/list", page=1, page_size=200)
        for g in data.get("list", []):
            if g["group_name"] == group_name:
                return str(g["group_id"])
        # Create it
        result = self._post("/api/v1/group/create", {"group_name": group_name})
        return str(result["group_id"])

    # ── profiles ──────────────────────────────────────────────────────────────

    def list_profiles(self, group_id: str = "", page_size: int = 50) -> list[dict]:
        """Return all profiles, optionally filtered by group_id."""
        profiles = []
        page = 1
        while True:
            params = {"page": page, "page_size": page_size}
            if group_id:
                params["group_id"] = group_id
            data = self._get("/api/v1/user/list", **params)
            batch = data.get("list", [])
            profiles.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return profiles

    def get_profile(self, user_id: str) -> dict:
        data = self._get("/api/v1/user/list", user_id=user_id)
        lst = data.get("list", [])
        if not lst:
            raise RuntimeError(f"Profile {user_id} not found")
        return lst[0]

    def create_profile(
        self,
        name: str,
        username: str = "",
        password: str = "",
        fakey: str = "",
        proxy_config: dict | None = None,
        group_id: str = "0",
        remark: str = "",
        platform: str = "facebook.com",
    ) -> str:
        """Create a new AdsPower profile. Returns the new user_id."""
        payload = {
            "name": name,
            "domain_name": platform,
            "username": username,
            "password": password,
            "fakey": fakey,
            "group_id": str(group_id),
            "remark": remark,
            "user_proxy_config": proxy_config or {"proxy_soft": "no_proxy"},
            # Force desktop fingerprint so Facebook never serves the mobile layout
            "fingerprint_config": {
                "ua": _DESKTOP_UA,
                "os": "windows",
                "browser": "chrome",
            },
        }
        result = self._post("/api/v1/user/create", payload)
        return result["id"]

    def update_profile(self, user_id: str, **fields):
        """Update arbitrary profile fields (group_id, remark, username, etc.)."""
        body = {"user_id": user_id, **fields}
        self._post("/api/v1/user/update", body)

    def move_to_group(self, user_id: str, group_id: str = "0"):
        """Move profile to group_id (use '0' to remove from all groups)."""
        self.update_profile(user_id, group_id=str(group_id))

    # ── browser ───────────────────────────────────────────────────────────────

    def open_browser(self, user_id: str, headless: bool = False) -> dict:
        """
        Start the profile browser.
        Returns dict with keys: ws (puppeteer, selenium), debug_port, webdriver.
        """
        params = {"user_id": user_id}
        if headless:
            params["headless"] = "1"
        # AdsPower may return -1 if another instance is already open; retry once
        for attempt in range(2):
            try:
                return self._get("/api/v1/browser/start", **params)
            except RuntimeError as e:
                if attempt == 0 and "Too many" in str(e):
                    time.sleep(2)
                    continue
                raise

    def close_browser(self, user_id: str):
        """Stop the profile browser (best-effort)."""
        try:
            self._get("/api/v1/browser/stop", user_id=user_id)
        except Exception:
            pass
