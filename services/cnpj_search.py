"""
CNPJ Search via Casa dos Dados API.
Finds active companies matching business filters, filtered by city population.
Ported from Gerador CNPJ/services/casadosdados.py.
"""
import csv
import json
import random
import requests

CDD_BASE_URL = "https://api.casadosdados.com.br"
CDD_EMPRESA_SEARCH_ENDPOINT = f"{CDD_BASE_URL}/v5/cnpj/pesquisa"

UF_BY_CODE = {
    11: "ro", 12: "ac", 13: "am", 14: "rr", 15: "pa", 16: "ap", 17: "to",
    21: "ma", 22: "pi", 23: "ce", 24: "rn", 25: "pb", 26: "pe", 27: "al",
    28: "se", 29: "ba", 31: "mg", 32: "es", 33: "rj", 35: "sp", 41: "pr",
    42: "sc", 43: "rs", 50: "ms", 51: "mt", 52: "go", 53: "df",
}


def ler_cidades_por_populacao(csv_path: str, pop_min: int, pop_max: int) -> list[tuple[str, str, int, str]]:
    """
    Returns list of (municipio_api, uf, pop, nome_original) within population range.
    """
    cidades = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                pop = int(float(row["populacao"]))
                id_mun = int(row["id_municipio"])
            except Exception:
                continue

            if not (pop_min <= pop <= pop_max):
                continue

            nome_municipio = (row.get("id_municipio_nome") or "").strip()
            if not nome_municipio:
                continue

            uf_cod = int(str(id_mun)[:2])
            uf = UF_BY_CODE.get(uf_cod)
            if not uf:
                continue

            municipio_api = nome_municipio.lower()
            cidades.append((municipio_api, uf, pop, nome_municipio))

    return cidades


def montar_payload_casadosdados(
    municipio: str,
    uf: str,
    codigo_atividade_principal: list[str],
    incluir_atividade_secundaria: bool,
    codigo_atividade_secundaria: list[str],
    codigo_natureza_juridica: list[str],
    situacao_cadastral: list[str],
    matriz_filial: str,
    capital_minimo_reais: int,
    capital_maximo_reais: int,
    mais_filtros: dict,
    limite_por_pagina: int = 50,
) -> dict:
    return {
        "cnpj": [],
        "busca_textual": [],
        "codigo_atividade_principal": codigo_atividade_principal,
        "incluir_atividade_secundaria": incluir_atividade_secundaria,
        "codigo_atividade_secundaria": codigo_atividade_secundaria,
        "codigo_natureza_juridica": codigo_natureza_juridica,
        "situacao_cadastral": situacao_cadastral,
        "matriz_filial": matriz_filial if matriz_filial in ("MATRIZ", "FILIAL") else "",
        "cnpj_raiz": [],
        "cep": [],
        "endereco_numero": [],
        "uf": [uf] if uf else [],
        "municipio": [municipio] if municipio else [],
        "bairro": [],
        "ddd": [],
        "telefone": [],
        "data_abertura": {"inicio": "", "fim": "", "ultimos_dias": 0},
        "capital_social": {"minimo": capital_minimo_reais, "maximo": capital_maximo_reais},
        "mei": None,
        "simples": None,
        "mais_filtros": mais_filtros,
        "excluir": {"cnpj": []},
        "limite": limite_por_pagina,
        "pagina": 1,
    }


def buscar_cnpjs_casadosdados(api_key: str, payload: dict) -> list[dict]:
    if not api_key:
        raise RuntimeError("CASADOSDADOS_API_KEY não configurada.")

    headers = {"api-key": api_key, "Content-Type": "application/json"}
    resp = requests.post(
        CDD_EMPRESA_SEARCH_ENDPOINT,
        headers=headers,
        data=json.dumps(payload),
        timeout=60,
    )

    if resp.status_code == 422:
        return []

    resp.raise_for_status()
    dados = resp.json()

    cnpjs = dados.get("cnpjs") or dados.get("cnpj") or []
    if not isinstance(cnpjs, list):
        return []
    return cnpjs


def encontrar_um_cnpj_por_filtros(
    csv_path: str,
    pop_min: int,
    pop_max: int,
    casadosdados_api_key: str,
    filtros: dict,
    used_cnpjs: set[str] | None = None,
) -> str | None:
    """
    Returns 1 CNPJ string matching the filters.
    Pass used_cnpjs (set of 14-digit strings) to skip already-used CNPJs locally
    before hitting the DB unique constraint.
    """
    cidades = ler_cidades_por_populacao(csv_path, pop_min, pop_max)
    if not cidades:
        return None

    random.shuffle(cidades)

    for municipio_api, uf, pop, nome_original in cidades:
        payload = montar_payload_casadosdados(
            municipio=municipio_api,
            uf=uf,
            codigo_atividade_principal=filtros["CODIGO_ATIVIDADE_PRINCIPAL"],
            incluir_atividade_secundaria=filtros["INCLUIR_ATIVIDADE_SECUNDARIA"],
            codigo_atividade_secundaria=filtros["CODIGO_ATIVIDADE_SECUNDARIA"],
            codigo_natureza_juridica=filtros["CODIGO_NATUREZA_JURIDICA"],
            situacao_cadastral=filtros["SITUACAO_CADASTRAL"],
            matriz_filial=filtros["MATRIZ_FILIAL"],
            capital_minimo_reais=filtros["CAPITAL_MINIMO_REAIS"],
            capital_maximo_reais=filtros["CAPITAL_MAXIMO_REAIS"],
            mais_filtros=filtros["MAIS_FILTROS"],
            limite_por_pagina=filtros.get("LIMITE_POR_PAGINA", 50),
        )

        empresas = buscar_cnpjs_casadosdados(casadosdados_api_key, payload)
        for emp in empresas:
            cnpj = emp.get("cnpj")
            if not cnpj:
                continue
            cnpj_str = str(cnpj)
            if used_cnpjs and cnpj_str in used_cnpjs:
                continue
            return cnpj_str

    return None


# Default search filters for WABA verification
DEFAULT_FILTROS = {
    "CODIGO_ATIVIDADE_PRINCIPAL": ["8211300"],
    "CODIGO_ATIVIDADE_SECUNDARIA": [],
    "INCLUIR_ATIVIDADE_SECUNDARIA": False,
    "CODIGO_NATUREZA_JURIDICA": [],
    "SITUACAO_CADASTRAL": ["ATIVA"],
    "MATRIZ_FILIAL": "MATRIZ",
    "CAPITAL_MINIMO_REAIS": 10_000,
    "CAPITAL_MAXIMO_REAIS": 5_000_000,
    "MAIS_FILTROS": {
        "somente_matriz": True,
        "somente_filial": False,
        "com_email": True,
        "com_telefone": True,
        "somente_fixo": False,
        "somente_celular": False,
        "excluir_empresas_visualizadas": False,
        "excluir_email_contab": True,
    },
    "LIMITE_POR_PAGINA": 50,
}
