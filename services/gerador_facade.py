"""
Drop-in replacement for GeradorClient.
All methods have the exact same signatures and return types as the old HTTP client,
but call local pipeline functions instead of making API requests.

Wraps every call in a Flask app context so this works from both:
- The Flask web process (already has context)
- Standalone scripts like main.py and Celery tasks (no context by default)

Usage (replaces GeradorClient instantiation):
    from services.gerador_facade import GeradorService
    gerador = GeradorService()
"""
from contextlib import contextmanager


def _get_flask_app():
    from web_app import create_app
    return create_app()


@contextmanager
def _app_ctx():
    """Push a Flask app context if one isn't already active."""
    from flask import has_app_context
    if has_app_context():
        yield  # already inside Flask — no-op
    else:
        app = _get_flask_app()
        with app.app_context():
            yield


class GeradorService:
    """
    Local replacement for GeradorClient.
    No network calls — all operations run in-process.
    """

    def get_run(self, run_id: int) -> dict:
        """
        Returns a dict with keys:
            run_id, cnpj_digits, cnpj_formatted, razao_social, email,
            telefone_formatted, telefone_digits,
            logradouro, bairro, municipio, estado_sigla, estado_nome,
            cep_digits, cep_formatted, deploy_url, pdf_filename
        """
        from services.cnpj_pipeline import get_run_data
        with _app_ctx():
            return get_run_data(run_id)

    def download_pdf(self, run_id: int, dest_path: str | None = None) -> str:
        """
        Returns the local path to the PDF.
        If dest_path is given, copies the PDF there and returns dest_path.
        """
        from services.cnpj_pipeline import download_pdf
        with _app_ctx():
            return download_pdf(run_id, dest_path)

    def change_phone(self, run_id: int, phone_local: str) -> tuple[str, str]:
        """
        Regenerate the cartão PDF with phone_local (digits only, no country code).
        Returns (phone_formatted, local_pdf_path).
        """
        from services.cnpj_pipeline import change_phone
        with _app_ctx():
            return change_phone(run_id, phone_local)

    def change_website_phone(self, run_id: int, phone_local: str) -> bool:
        """
        Update the phone number in the deployed website HTML.
        phone_local: digits only, no country code (e.g. "71987654321").
        Updates local index.html and pushes to CloudPanel via SSH.
        Returns True on success.
        """
        from services.cnpj_pipeline import change_website_phone
        with _app_ctx():
            return change_website_phone(run_id, phone_local)

    def inject_meta_tag(self, run_id: int, meta_tag: str) -> bool:
        """
        Inject the Facebook domain-verification <meta> tag into the deployed site.
        Updates local index.html and pushes to CloudPanel via SSH.
        Returns True on success.
        """
        from services.cnpj_pipeline import inject_meta_tag
        with _app_ctx():
            return inject_meta_tag(run_id, meta_tag)

    def acquire_run(self) -> dict:
        """
        Claim a pre-generated run or generate one synchronously.
        Always returns {"run_id": N, "source": "local"} — no polling needed.
        """
        from services.cnpj_pipeline import acquire_run
        with _app_ctx():
            run_id = acquire_run()
        return {"run_id": run_id, "source": "local"}

    def wait_for_run(self, job_id: str, **kwargs) -> int:  # noqa: ARG002
        """
        Not needed anymore — acquire_run() returns synchronously.
        Kept for interface compatibility; raises NotImplementedError if called.
        """
        raise NotImplementedError(
            "wait_for_run is no longer needed — acquire_run() always returns a run_id directly."
        )
