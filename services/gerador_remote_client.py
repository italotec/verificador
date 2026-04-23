"""
HTTP client for the Gerador API running on the VPS.
Used by agent.py / worker.py when running on the local Windows machine,
where the VPS database and PDF files are not accessible directly.

Drop-in replacement for GeradorService — same method signatures and return types.
"""
import os
import tempfile
import time

import requests


def _check(r: requests.Response) -> requests.Response:
    """Raise with the server's JSON error body when the response is not 2xx."""
    if not r.ok:
        try:
            detail = r.json().get("error", r.text[:300])
        except Exception:
            detail = r.text[:300]
        raise RuntimeError(f"VPS {r.status_code}: {detail}")
    return r


class GeradorRemoteClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.headers = {"X-Worker-Key": api_key}

    def get_run(self, run_id: int) -> dict:
        r = requests.get(
            f"{self.base}/worker/gerador/runs/{run_id}",
            headers=self.headers,
            timeout=30,
        )
        return _check(r).json()

    def download_pdf(self, run_id: int, dest_path: str | None = None) -> str:
        r = requests.get(
            f"{self.base}/worker/gerador/runs/{run_id}/pdf",
            headers=self.headers,
            timeout=60,
            stream=True,
        )
        _check(r)
        if dest_path is None:
            fd, dest_path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest_path

    def change_phone(self, run_id: int, phone_local: str) -> tuple[str, str]:
        r = requests.post(
            f"{self.base}/worker/gerador/runs/{run_id}/change-phone",
            headers=self.headers,
            json={"phone": phone_local},
            timeout=60,
        )
        data = _check(r).json()
        if not data.get("success"):
            raise RuntimeError(f"change-phone falhou: {data.get('error')}")
        phone_formatted = data["phone_formatted"]
        pdf_path = self.download_pdf(run_id)
        return phone_formatted, pdf_path

    def change_website_phone(self, run_id: int, phone_local: str) -> bool:
        r = requests.post(
            f"{self.base}/worker/gerador/runs/{run_id}/change-website-phone",
            headers=self.headers,
            json={"phone": phone_local},
            timeout=60,
        )
        return _check(r).json().get("success", False)

    def inject_meta_tag(self, run_id: int, meta_tag: str) -> bool:
        r = requests.post(
            f"{self.base}/worker/gerador/runs/{run_id}/inject-meta-tag",
            headers=self.headers,
            json={"meta_tag": meta_tag},
            timeout=60,
        )
        return _check(r).json().get("success", False)

    def acquire_run(self) -> dict:
        r = requests.post(
            f"{self.base}/worker/gerador/acquire-run",
            headers=self.headers,
            timeout=30,
        )
        data = _check(r).json()
        if data.get("status") != "pending":
            return data  # bank hit — immediate return

        # Bank was empty; VPS is generating async — poll until done
        poll_id = data["poll_id"]
        for _ in range(40):  # up to ~10 minutes (40 × 15 s)
            time.sleep(15)
            r2 = requests.get(
                f"{self.base}/worker/gerador/acquire-run/{poll_id}",
                headers=self.headers,
                timeout=15,
            )
            d2 = _check(r2).json()
            if d2.get("status") == "done":
                return d2
            if d2.get("status") == "error":
                raise RuntimeError(f"Gerador generation failed: {d2.get('error')}")
            # status == "pending" → keep polling
        raise TimeoutError("Gerador acquire-run timed out after 10 minutes")

    def wait_for_run(self, *args, **kwargs) -> int:  # noqa: ARG002
        raise NotImplementedError(
            "wait_for_run não é necessário — acquire_run() retorna o run_id diretamente."
        )
