"""
CNPJ generation pipeline orchestrator.
Replaces GeradorClient HTTP calls with direct local function calls.
Manages CNPJRun records in the local DB and files in GERADOR_STORAGE_DIR.

Public API (mirrors GeradorClient interface):
    generate_cnpj_run(cfg)           -> CNPJRun   (full pipeline)
    get_run_data(run_id)             -> dict       (replaces GeradorClient.get_run)
    download_pdf(run_id, dest_path)  -> str        (replaces GeradorClient.download_pdf)
    change_phone(run_id, phone)      -> (str, str) (replaces GeradorClient.change_phone)
    inject_meta_tag(run_id, tag)     -> bool       (replaces GeradorClient.inject_meta_tag)
    acquire_run(cfg)                 -> int        (replaces GeradorClient.acquire_run + wait_for_run)
"""
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import config as cfg_module

from services.cnpj_search import encontrar_um_cnpj_por_filtros, DEFAULT_FILTROS
from services.cnpj_lookup import consulta_casa_dos_dados, extrair_campos_empresa
from services.website_generator import gerar_html_loja
from services.cnpj_cartao import (
    gerar_pdf_cartao,
    gerar_cartao_cnpj_com_telefone,
    formatar_telefone_bruto,
)
from services.cloudpanel_deploy import (
    publicar_em_subdominio_proprio,
    atualizar_index_html_no_cloudpanel,
)

UF_NOMES = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas",
    "BA": "Bahia", "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo",
    "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
    "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina",
    "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins",
}


# ── Storage helpers ───────────────────────────────────────────────────────────

def _sanitize_filename(name: str) -> str:
    name = name or "documento"
    name = re.sub(r'[\\/:*?"<>|]+', "", name).strip().replace(" ", "_")
    return name[:120] or "documento"


def _storage_paths(storage_dir: Path, day_key: str, cnpj: str, razao: str) -> dict:
    safe_name = "".join(ch for ch in (razao or "") if ch.isalnum() or ch in (" ", "-", "_")).strip().replace(" ", "_")
    safe_name = safe_name[:80] or "empresa"
    folder = storage_dir / day_key / f"{cnpj}__{safe_name}"
    folder.mkdir(parents=True, exist_ok=True)
    pdf_name = _sanitize_filename(razao or "empresa") + ".pdf"
    return {
        "folder": folder,
        "index": folder / "index.html",
        "link": folder / "link.txt",
        "pdf": folder / pdf_name,
    }


def _get_storage_dir() -> Path:
    d = cfg_module.GERADOR_STORAGE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deploy_config() -> dict:
    return {
        "vps_ip":              cfg_module.CLOUDPANEL_VPS_IP,
        "vps_user":            cfg_module.CLOUDPANEL_VPS_USER,
        "vps_pass":            cfg_module.CLOUDPANEL_VPS_PASS,
        "site_pass":           cfg_module.CLOUDPANEL_SITE_PASS,
        "spaceship_api_key":   cfg_module.SPACESHIP_API_KEY,
        "spaceship_api_secret": cfg_module.SPACESHIP_API_SECRET,
        "dominios":            cfg_module.CLOUDPANEL_DOMAINS,
        "php_version":         cfg_module.CLOUDPANEL_PHP_VERSION,
    }


# ── Main pipeline ─────────────────────────────────────────────────────────────

def generate_cnpj_run(specific_cnpj: str | None = None) -> "CNPJRun":
    """
    Full generation pipeline:
    1. Find a valid CNPJ (or use specific_cnpj)
    2. Lookup company details
    3. Generate HTML website via Claude
    4. Deploy to CloudPanel subdomain
    5. Generate PDF business card
    6. Save CNPJRun to DB
    Returns the CNPJRun instance.
    """
    from web_app import db
    from web_app.models import CNPJRun, UsedCNPJ

    storage_dir = _get_storage_dir()

    # Step 1: Find CNPJ
    if specific_cnpj:
        cnpj = re.sub(r"\D", "", specific_cnpj)
    else:
        # Get set of already-used CNPJs for local pre-check
        used_set = {row.cnpj for row in UsedCNPJ.query.with_entities(UsedCNPJ.cnpj).all()}
        cnpj = encontrar_um_cnpj_por_filtros(
            csv_path=cfg_module.POP_CSV_PATH,
            pop_min=0,
            pop_max=200_000,
            casadosdados_api_key=cfg_module.CASADOSDADOS_API_KEY,
            filtros=DEFAULT_FILTROS,
            used_cnpjs=used_set,
        )
        if not cnpj:
            raise RuntimeError("Casa dos Dados não retornou CNPJ com os filtros configurados.")

    # Step 2: Company details
    data_raw = consulta_casa_dos_dados(cnpj, cfg_module.CASADOSDADOS_API_KEY)
    data_empresa = extrair_campos_empresa(data_raw)
    razao_social = data_empresa.get("razao_social") or "Empresa"

    # Step 3: Generate HTML
    from web_app.models import SystemSetting
    ai_provider = SystemSetting.get("AI_PROVIDER", "anthropic")
    if ai_provider == "openai":
        ai_key   = SystemSetting.get("OPENAI_API_KEY_CNPJ", cfg_module.OPENAI_API_KEY)
        ai_model = SystemSetting.get("OPENAI_MODEL_CNPJ", getattr(cfg_module, "OPENAI_MODEL", "gpt-4.1-mini"))
    else:
        ai_key   = SystemSetting.get("ANTHROPIC_API_KEY_CNPJ", cfg_module.ANTHROPIC_API_KEY)
        ai_model = SystemSetting.get("ANTHROPIC_MODEL_CNPJ", cfg_module.CLAUDE_FAST_MODEL)
    html_code = gerar_html_loja(ai_provider, ai_key, ai_model, data_empresa)

    # Step 4: Deploy to CloudPanel
    dcfg = _deploy_config()
    deploy_url = publicar_em_subdominio_proprio(
        razao_social=razao_social,
        html_content=html_code,
        **dcfg,
    )
    if not deploy_url:
        raise RuntimeError("Falha ao publicar o site no subdomínio próprio.")

    # Step 5: Generate PDF
    pdf_bytes = gerar_pdf_cartao(data_raw, cfg_module.CNPJ_CARTAO_TEMPLATE)

    # Step 6: Save files + DB record
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    paths = _storage_paths(storage_dir, day_key, cnpj, razao_social)

    paths["index"].write_text(html_code, encoding="utf-8")
    paths["link"].write_text(deploy_url + "\n", encoding="utf-8")
    paths["pdf"].write_bytes(pdf_bytes)

    # Cache company data for fast get_run lookups
    empresa_cache = {
        "cnpj": data_empresa.get("cnpj", ""),
        "razao_social": razao_social,
        "email": data_empresa.get("email", ""),
        "telefone": data_empresa.get("telefone", ""),
        "logradouro": data_empresa.get("logradouro", ""),
        "bairro": data_empresa.get("bairro", ""),
        "municipio": data_empresa.get("cidade", ""),
        "estado": data_empresa.get("estado", ""),
        "cep": re.sub(r"\D", "", str(data_raw.get("endereco", {}).get("cep") or "")),
    }
    (paths["folder"] / "data.json").write_text(json.dumps(empresa_cache, ensure_ascii=False), encoding="utf-8")

    # Register as used
    try:
        db.session.add(UsedCNPJ(cnpj=cnpj))
        db.session.commit()
    except Exception:
        db.session.rollback()  # already exists — fine

    run = CNPJRun(
        cnpj=cnpj,
        razao_social=razao_social,
        day_key=day_key,
        folder_rel=str(paths["folder"].relative_to(storage_dir)),
        index_rel=str(paths["index"].relative_to(storage_dir)),
        link_rel=str(paths["link"].relative_to(storage_dir)),
        pdf_rel=str(paths["pdf"].relative_to(storage_dir)),
        site_url=deploy_url,
        deploy_url=deploy_url,
        data_json=json.dumps(empresa_cache, ensure_ascii=False),
    )
    db.session.add(run)
    db.session.commit()

    # Patch run_id into data.json now that we have it
    empresa_cache["run_id"] = run.id
    (paths["folder"] / "data.json").write_text(json.dumps(empresa_cache, ensure_ascii=False), encoding="utf-8")

    return run


# ── GeradorClient replacement functions ──────────────────────────────────────

def get_run_data(run_id: int) -> dict:
    """
    Replaces GeradorClient.get_run().
    Returns the same dict shape as the old /api/run/<id> endpoint.
    """
    from web_app.models import CNPJRun
    run = CNPJRun.query.get(run_id)
    if not run:
        raise RuntimeError(f"CNPJRun {run_id} não encontrado.")

    storage_dir = _get_storage_dir()

    def _cache_complete(d: dict) -> bool:
        """Return True only if the cached dict has all critical fields populated."""
        return bool(d.get("municipio") and d.get("estado") and d.get("telefone"))

    # Try cached data.json first — but only if all critical fields are present
    empresa = None
    data_json_path = storage_dir / run.folder_rel / "data.json"
    if data_json_path.exists():
        try:
            candidate = json.loads(data_json_path.read_text(encoding="utf-8"))
            if _cache_complete(candidate):
                empresa = candidate
        except Exception:
            pass

    if not empresa and run.data_json:
        try:
            candidate = json.loads(run.data_json)
            if _cache_complete(candidate):
                empresa = candidate
        except Exception:
            pass

    if not empresa:
        # Live lookup — cache was missing or had empty critical fields
        from services.cnpj_lookup import extrair_campos_empresa
        data_raw = consulta_casa_dos_dados(run.cnpj, cfg_module.CASADOSDADOS_API_KEY)
        data_emp = extrair_campos_empresa(data_raw)
        end = data_raw.get("endereco") or {}
        empresa = {
            "run_id": run.id,
            "cnpj": data_emp.get("cnpj", ""),
            "razao_social": data_emp.get("razao_social", ""),
            "email": data_emp.get("email", ""),
            "telefone": data_emp.get("telefone", ""),
            "logradouro": data_emp.get("logradouro", ""),
            "bairro": data_emp.get("bairro", ""),
            "municipio": data_emp.get("cidade", ""),
            "estado": data_emp.get("estado", ""),
            "cep": re.sub(r"\D", "", end.get("cep", "")),
        }
        # Refresh stale cache so next call is fast
        try:
            from web_app import db
            run.data_json = json.dumps(empresa, ensure_ascii=False)
            db.session.commit()
            data_json_path.parent.mkdir(parents=True, exist_ok=True)
            data_json_path.write_text(json.dumps(empresa, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    cep_d = re.sub(r"\D", "", empresa.get("cep", ""))
    tel_d = re.sub(r"\D", "", empresa.get("telefone", ""))
    cnpj_d = re.sub(r"\D", "", empresa.get("cnpj", ""))
    estado_sigla = (empresa.get("estado") or "").upper()

    # Build formatted CNPJ
    cnpj_fmt = (
        f"{cnpj_d[:2]}.{cnpj_d[2:5]}.{cnpj_d[5:8]}/{cnpj_d[8:12]}-{cnpj_d[12:14]}"
        if len(cnpj_d) == 14 else cnpj_d
    )

    return {
        "run_id": run.id,
        "cnpj_digits": cnpj_d,
        "cnpj_formatted": cnpj_fmt,
        "razao_social": empresa.get("razao_social", "") or run.razao_social,
        "email": empresa.get("email", ""),
        "telefone_formatted": empresa.get("telefone", ""),
        "telefone_digits": tel_d,
        "logradouro": empresa.get("logradouro", ""),
        "bairro": empresa.get("bairro", ""),
        "municipio": empresa.get("municipio", ""),
        "estado_sigla": estado_sigla,
        "estado_nome": UF_NOMES.get(estado_sigla, estado_sigla),
        "cep_digits": cep_d,
        "cep_formatted": f"{cep_d[:5]}-{cep_d[5:]}" if len(cep_d) == 8 else empresa.get("cep", ""),
        "deploy_url": run.deploy_url or "",
        "pdf_filename": Path(run.pdf_rel).name if run.pdf_rel else "",
        # DNS-TXT verification needs the parent-domain pool + Spaceship API creds
        "dominios": cfg_module.CLOUDPANEL_DOMAINS,
        "spaceship_api_key": cfg_module.SPACESHIP_API_KEY,
        "spaceship_api_secret": cfg_module.SPACESHIP_API_SECRET,
    }


def download_pdf(run_id: int, dest_path: str | None = None) -> str:
    """
    Replaces GeradorClient.download_pdf().
    Returns the local path to the PDF file (no HTTP transfer needed).
    """
    from web_app.models import CNPJRun
    run = CNPJRun.query.get(run_id)
    if not run:
        raise RuntimeError(f"CNPJRun {run_id} não encontrado.")

    storage_dir = _get_storage_dir()
    pdf_path = (storage_dir / run.pdf_rel).resolve()

    if not pdf_path.exists():
        raise RuntimeError(f"PDF não encontrado: {pdf_path}")

    if dest_path is None:
        return str(pdf_path)

    import shutil
    shutil.copy2(pdf_path, dest_path)
    return dest_path


def change_phone(run_id: int, phone_local: str) -> tuple[str, str]:
    """
    Replaces GeradorClient.change_phone().
    Regenerates the PDF with a new phone number.
    Returns (phone_formatted, pdf_path).
    """
    from web_app.models import CNPJRun
    run = CNPJRun.query.get(run_id)
    if not run:
        raise RuntimeError(f"CNPJRun {run_id} não encontrado.")

    storage_dir = _get_storage_dir()
    pdf_path = (storage_dir / run.pdf_rel).resolve()

    data_raw = consulta_casa_dos_dados(run.cnpj, cfg_module.CASADOSDADOS_API_KEY)
    pdf_bytes = gerar_cartao_cnpj_com_telefone(
        data_raw, phone_local, cfg_module.CNPJ_CARTAO_TEMPLATE
    )
    pdf_path.write_bytes(pdf_bytes)

    tel_fmt = formatar_telefone_bruto(phone_local)
    return tel_fmt, str(pdf_path)


def change_website_phone(run_id: int, phone_local: str) -> bool:
    """
    Update the phone number displayed in the deployed website HTML.
    phone_local: digits only, no country code (e.g. "71987654321").
    Updates local index.html and pushes to CloudPanel via SSH.
    Returns True on success.
    """
    from web_app.models import CNPJRun
    from services.website_generator import update_phone_in_html, format_br_phone

    run = CNPJRun.query.get(run_id)
    if not run or not run.deploy_url or not run.index_rel:
        return False

    storage_dir = _get_storage_dir()
    index_path = (storage_dir / run.index_rel).resolve()

    if not index_path.exists():
        print(f"[WEBSITE PHONE] index.html not found for run {run_id}")
        return False

    current_html = index_path.read_text(encoding="utf-8")
    new_phone = format_br_phone(phone_local)
    new_html = update_phone_in_html(current_html, new_phone)

    if new_html == current_html:
        print(f"[WEBSITE PHONE] Phone element not found in HTML for run {run_id}")
        return False

    index_path.write_text(new_html, encoding="utf-8")

    dominio = urlparse(run.deploy_url).netloc
    return atualizar_index_html_no_cloudpanel(
        dominio=dominio,
        novo_html=new_html,
        vps_ip=cfg_module.CLOUDPANEL_VPS_IP,
        vps_user=cfg_module.CLOUDPANEL_VPS_USER,
        vps_pass=cfg_module.CLOUDPANEL_VPS_PASS,
    )


def inject_meta_tag(run_id: int, meta_tag: str) -> bool:
    """
    Replaces GeradorClient.inject_meta_tag().
    Injects a Facebook domain verification <meta> tag into the deployed site.
    Updates the local index.html and pushes via SSH.
    """
    from web_app.models import CNPJRun
    run = CNPJRun.query.get(run_id)
    if not run:
        raise RuntimeError(f"CNPJRun {run_id} não encontrado.")
    if not run.deploy_url:
        raise RuntimeError(f"CNPJRun {run_id} não tem deploy_url.")

    storage_dir = _get_storage_dir()
    index_path = (storage_dir / run.index_rel).resolve()

    current_html = index_path.read_text(encoding="utf-8")
    if "<head>" in current_html:
        new_html = current_html.replace("<head>", f"<head>\n    {meta_tag}")
    elif "<head " in current_html.lower():
        new_html = re.sub(r"(?i)<head([^>]*)>", rf"<head\1>\n    {meta_tag}", current_html, count=1)
    else:
        new_html = current_html + f"\n    {meta_tag}\n"

    index_path.write_text(new_html, encoding="utf-8")

    dominio = urlparse(run.deploy_url).netloc
    return atualizar_index_html_no_cloudpanel(
        dominio=dominio,
        novo_html=new_html,
        vps_ip=cfg_module.CLOUDPANEL_VPS_IP,
        vps_user=cfg_module.CLOUDPANEL_VPS_USER,
        vps_pass=cfg_module.CLOUDPANEL_VPS_PASS,
    )


def acquire_run() -> int:
    """
    Replaces GeradorClient.acquire_run() + wait_for_run().
    Claims the oldest pre-generated run from the bank, or generates one
    synchronously if the bank is empty. Returns run_id.
    """
    from web_app import db
    from web_app.models import CNPJRun

    pre = (
        CNPJRun.query
        .filter_by(is_pre_generated=True, claimed_at=None)
        .order_by(CNPJRun.created_at.asc())
        .first()
    )
    if pre:
        pre.claimed_at = datetime.utcnow()
        db.session.commit()
        print(f"[ACQUIRE] Claimed run {pre.id} from bank")
        return pre.id

    print("[ACQUIRE] Bank empty — generating synchronously")
    run = generate_cnpj_run()
    return run.id
