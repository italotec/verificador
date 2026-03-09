"""
Client for the Gerador CNPJ internal API.
Provides run data, PDF download, phone change, meta-tag injection,
and run acquisition (claim from pre-generated bank or trigger async generation).
"""
import os
import tempfile
import time
import requests


class GeradorClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key}

    # ── run data ──────────────────────────────────────────────────────────────

    def get_run(self, run_id: int) -> dict:
        """
        Returns a dict with keys:
            run_id, cnpj_digits, cnpj_formatted, razao_social, email,
            telefone_formatted, telefone_digits,
            logradouro, bairro, municipio, estado_sigla, estado_nome,
            cep_digits, cep_formatted, deploy_url, pdf_filename
        """
        r = requests.get(
            f"{self.base}/api/run/{run_id}",
            headers=self.headers,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    # ── PDF ───────────────────────────────────────────────────────────────────

    def download_pdf(self, run_id: int, dest_path: str | None = None) -> str:
        """
        Download the current PDF for *run_id*.
        Saves to *dest_path* or a temp file.
        Returns the local file path.
        """
        r = requests.get(
            f"{self.base}/api/run/{run_id}/pdf",
            headers=self.headers,
            timeout=60,
            stream=True,
        )
        r.raise_for_status()
        if dest_path is None:
            fd, dest_path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest_path

    # ── phone / PDF update ────────────────────────────────────────────────────

    def change_phone(self, run_id: int, phone_local: str) -> tuple[str, str]:
        """
        Tell the Gerador to regenerate the cartão PDF with *phone_local*
        (digits only, no country code, e.g. '1151041946').

        Returns (phone_formatted, local_pdf_path).
        """
        r = requests.post(
            f"{self.base}/api/run/{run_id}/change-phone",
            headers=self.headers,
            json={"phone": phone_local},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"change-phone failed: {data.get('error')}")
        formatted = data.get("phone_formatted", phone_local)
        # Download the freshly-generated PDF
        pdf_path = self.download_pdf(run_id)
        return formatted, pdf_path

    # ── run acquisition ───────────────────────────────────────────────────────

    def acquire_run(self) -> dict:
        """
        Ask the Gerador to give us a run to verify.

        If the pre-generated bank has an entry it is claimed immediately:
            {"run_id": 123, "source": "bank"}

        If the bank is empty, async generation is started:
            {"job_id": "uuid-...", "source": "generate"}
        Poll wait_for_run(job_id) to block until it's ready.
        """
        r = requests.post(
            f"{self.base}/api/run/acquire",
            headers=self.headers,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def wait_for_run(
        self,
        job_id: str,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> int:
        """
        Poll /api/generate/status/<job_id> until the run is ready.
        Returns run_id on success.
        Raises RuntimeError on generation error, TimeoutError on timeout.
        """
        start = time.time()
        while time.time() - start < timeout:
            r = requests.get(
                f"{self.base}/api/generate/status/{job_id}",
                headers=self.headers,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            status = data.get("status")

            if status == "done":
                return data["run_id"]

            if status == "error":
                raise RuntimeError(
                    f"Gerador generation failed: {data.get('message')}"
                )

            elapsed = int(time.time() - start)
            print(f"[GERADOR] Job {job_id[:8]}… pending ({elapsed}s elapsed)")
            time.sleep(poll_interval)

        raise TimeoutError(
            f"Gerador job {job_id} timed out after {timeout}s"
        )

    # ── domain meta-tag ───────────────────────────────────────────────────────

    def inject_meta_tag(self, run_id: int, meta_tag: str) -> bool:
        """
        Inject the Facebook domain-verification <meta> tag into the deployed
        website (updates local index.html + pushes to CloudPanel via SSH).
        Returns True on success.
        """
        r = requests.post(
            f"{self.base}/api/run/{run_id}/inject-meta",
            headers=self.headers,
            json={"meta_tag": meta_tag},
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("success", False)
