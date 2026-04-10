"""
Company data lookup via Casa dos Dados API v4.
Returns normalized company data dict used by website generator and PDF card.
Ported from Gerador CNPJ/services/cnpj_biz.py.
"""
import re
import requests


def consulta_casa_dos_dados(cnpj: str, api_key: str) -> dict:
    """
    Fetch full company details from Casa dos Dados v4 API.
    Returns the raw JSON dict.
    """
    url = f"https://api.casadosdados.com.br/v4/cnpj/{cnpj}"
    headers = {
        "api-key": api_key.strip(),
        "Accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"Resposta inesperada (não é dict): {type(data)}")
        return data

    except requests.exceptions.HTTPError as e:
        error_text = e.response.text[:300] if e.response else ""
        raise RuntimeError(f"Erro HTTP na Casa dos Dados: {e} — {error_text}")
    except Exception as e:
        raise RuntimeError(f"Falha na consulta Casa dos Dados: {e}")


def extrair_campos_empresa(data_raw: dict) -> dict:
    """
    Normalize raw Casa dos Dados v4 API JSON into the standard company dict
    expected by website_generator and cnpj_cartao.

    Casa dos Dados v4 field layout:
      - contato_telefonico: [{ddd, numero, ...}]   (NOT "telefones")
      - contato_email:      [{email, ...}]          (NOT top-level "email")
      - endereco.municipio: string                  (NOT endereco.cidade object)
      - endereco.uf:        string                  (NOT endereco.estado object)

    Returns keys: cnpj, razao_social, telefone, logradouro, bairro,
                  cidade, estado, email, dominio
    """
    if not isinstance(data_raw, dict):
        raise TypeError(f"extrair_campos_empresa esperava dict, recebeu {type(data_raw)}")

    cnpj = data_raw.get("cnpj", "")
    razao_social = data_raw.get("razao_social", "")

    # ── Phone ─────────────────────────────────────────────────────────────────
    # CDD v4: contato_telefonico [{ddd, numero, ...}]
    telefone = ""
    contato_tel = data_raw.get("contato_telefonico") or []
    if contato_tel and isinstance(contato_tel[0], dict):
        t = contato_tel[0]
        if t.get("completo"):
            telefone = str(t["completo"])
        elif t.get("ddd") and t.get("numero"):
            telefone = str(t["ddd"]) + str(t["numero"])
    # Fallback: old "telefones" key (CNPJ.biz format)
    if not telefone:
        telefones = data_raw.get("telefones") or []
        if telefones:
            telefone = str(telefones[0].get("telefone", "") if isinstance(telefones[0], dict) else telefones[0])

    # ── Address ───────────────────────────────────────────────────────────────
    end = data_raw.get("endereco") or {}
    tipo_logradouro = end.get("tipo_logradouro") or ""
    logradouro_nome = end.get("logradouro") or ""
    numero = end.get("numero") or ""
    bairro = end.get("bairro") or ""

    # CDD v4: municipio and uf are plain strings
    cidade = end.get("municipio") or ""
    estado = end.get("uf") or ""

    # Fallback: CNPJ.biz nested objects
    if not cidade:
        cidade_obj = end.get("cidade") or {}
        cidade = cidade_obj.get("nome") or cidade_obj.get("descricao") or ""
    if not estado:
        estado_obj = end.get("estado") or {}
        estado = estado_obj.get("sigla") or estado_obj.get("nome") or ""

    logradouro_partes = []
    if tipo_logradouro:
        logradouro_partes.append(tipo_logradouro)
    if logradouro_nome:
        logradouro_partes.append(logradouro_nome)
    logradouro = " ".join(logradouro_partes).strip()
    if numero:
        logradouro = f"{logradouro}, {numero}" if logradouro else numero

    # ── Email ─────────────────────────────────────────────────────────────────
    # CDD v4: contato_email [{email, ...}]
    email = ""
    contato_email = data_raw.get("contato_email") or []
    if contato_email and isinstance(contato_email[0], dict):
        email = contato_email[0].get("email", "")
    # Fallback: top-level "email" (CNPJ.biz format)
    if not email:
        email = data_raw.get("email", "")

    dominio = email.split("@", 1)[1] if email and "@" in email else "exemplo.com.br"

    return {
        "cnpj": re.sub(r"\D", "", cnpj),
        "razao_social": razao_social,
        "telefone": telefone,
        "logradouro": logradouro,
        "bairro": bairro,
        "cidade": cidade,
        "estado": estado,
        "email": email,
        "dominio": dominio,
    }
