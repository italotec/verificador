"""
CNPJ business card PDF generation.
Renders the cartaocnpj.html template with company data via Playwright Chromium.
Ported from Gerador CNPJ/services/cartao.py.
"""
import base64
import mimetypes
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup


# ── Text helpers ──────────────────────────────────────────────────────────────

def normalize(txt):
    if txt is None:
        return ""
    t = unicodedata.normalize("NFKD", str(txt))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def up(txt):
    return str(txt or "").upper()


def formata_cnpj(cnpj):
    digits = re.sub(r"\D", "", str(cnpj or ""))
    if len(digits) != 14:
        return str(cnpj or "")
    return f"{digits[0:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:14]}"


def formata_data(iso_date):
    if not iso_date:
        return ""
    iso_date = str(iso_date)
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(iso_date, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(iso_date.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except Exception:
        return iso_date


def formata_cep(cep):
    digits = re.sub(r"\D", "", str(cep or ""))
    return f"{digits[0:2]}.{digits[2:5]}-{digits[5:8]}" if len(digits) == 8 else (str(cep) or "")


def formatar_codigo_atividade(codigo: str) -> str:
    codigo = re.sub(r"\D", "", str(codigo or ""))
    if len(codigo) < 7:
        return codigo
    return f"{codigo[0:2]}.{codigo[2:4]}-{codigo[4]}-{codigo[5:7]}"


# ── Phone formatting ──────────────────────────────────────────────────────────

def formatar_telefone_cartao(data_raw: dict) -> str:
    """Format phone from raw API data for display on card."""
    telefones = data_raw.get("contato_telefonico") or []
    if not telefones:
        return "(00) 0000-0000"

    tel = telefones[0]

    # If "completo" key is present (set by change_phone), use it directly
    if "completo" in tel and tel["completo"]:
        completo = str(tel["completo"])
        digits = re.sub(r"\D", "", completo)
        if len(digits) == 11 and digits[2] == "9":
            digits = digits[:2] + digits[3:]
        if len(digits) == 10:
            return f"({digits[:2]}) {digits[2:6]}-{digits[6:10]}"
        return completo

    # Normal API format: ddd + numero
    ddd = str(tel.get("ddd") or "00")
    numero = str(tel.get("numero") or "00000000")
    digits = re.sub(r"\D", "", ddd + numero)

    if len(digits) == 11 and digits[2] == "9":
        digits = digits[:2] + digits[3:]

    if len(digits) != 10:
        return "(00) 0000-0000"

    return f"({digits[:2]}) {digits[2:6]}-{digits[6:10]}"


def formatar_telefone_bruto(raw_tel: str) -> str:
    """
    Format raw phone string (e.g. '71988608723') to (71) 8860-8723.
    Removes leading 9 for mobile numbers (11 digits).
    """
    digits = re.sub(r"\D", "", raw_tel or "")

    if len(digits) == 11 and digits[2] == "9":
        digits = digits[:2] + digits[3:]

    if len(digits) != 10:
        return "(00) 0000-0001"

    return f"({digits[:2]}) {digits[2:6]}-{digits[6:10]}"


# ── HTML manipulation helpers ─────────────────────────────────────────────────

def _wrap_with_base(html_str: str, base_uri: str) -> str:
    if "<html" not in html_str.lower():
        return f'<!doctype html><html><head><meta charset="utf-8"><base href="{base_uri}"></head><body>{html_str}</body></html>'
    return re.sub(r"(?is)<head([^>]*)>", rf'<head\1><meta charset="utf-8"><base href="{base_uri}">', html_str, count=1)


def inline_local_images(soup: BeautifulSoup, base_dir: Path):
    for img in soup.find_all("img"):
        src = img.get("src", "").strip()
        if not src or src.startswith(("http://", "https://", "data:")):
            continue
        img_path = (base_dir / src).resolve()
        if not img_path.exists():
            continue
        mime, _ = mimetypes.guess_type(img_path.name)
        if not mime:
            ext = img_path.suffix.lower().lstrip(".")
            mime = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
        b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
        img["src"] = f"data:{mime};base64,{b64}"


def set_b_in_td(td, text, index=0, default_mask="********"):
    bs = td.find_all("b")
    if len(bs) > index:
        bs[index].string = text if (text and str(text).strip()) else default_mask
        return True
    return False


def find_td_by_label(soup: BeautifulSoup, label_text: str):
    target = normalize(label_text)
    exact_matches = []
    partial_matches = []

    for font in soup.find_all("font"):
        label = normalize(font.get_text(" "))
        if label == target:
            td = font.find_parent("td")
            if td:
                exact_matches.append(td)
        elif target in label:
            td = font.find_parent("td")
            if td:
                partial_matches.append(td)

    if exact_matches:
        return exact_matches[0]
    if partial_matches:
        return partial_matches[0]
    return None


def replace_after_label_single_b(soup, label, new_text, default_mask="********", idx=0):
    td = find_td_by_label(soup, label)
    if not td:
        return False
    return set_b_in_td(td, new_text if new_text else default_mask, index=idx, default_mask=default_mask)


def replace_numero_inscricao_by_class(soup, cnpj_fmt, tipo_txt):
    b = soup.select_one("b.numerodeinscricaoclass")
    if not b:
        return False
    b.string = cnpj_fmt or "********"
    font = b.find_parent("font")
    if font:
        bs = font.find_all("b")
        if len(bs) >= 2:
            bs[1].string = up(tipo_txt or "********")
    return True


def replace_atividades_secundarias(soup: BeautifulSoup, atividades_sec):
    td = find_td_by_label(soup, "CÓDIGO E DESCRIÇÃO DAS ATIVIDADES ECONÔMICAS SECUNDÁRIAS")
    if not td:
        return

    td.clear()

    label_font = soup.new_tag("font")
    label_font["face"] = "Arial"
    label_font["style"] = "font-size: 6pt"
    label_font.string = "CÓDIGO E DESCRIÇÃO DAS ATIVIDADES ECONÔMICAS SECUNDÁRIAS"
    td.append(label_font)
    td.append(soup.new_tag("br"))

    def add_linha(texto: str):
        font_tag = soup.new_tag("font")
        font_tag["face"] = "Arial"
        font_tag["style"] = "font-size: 8pt"
        b_tag = soup.new_tag("b")
        b_tag.string = f"\t{texto}"
        font_tag.append(b_tag)
        td.append(font_tag)
        td.append(soup.new_tag("br"))

    if not atividades_sec:
        add_linha("Não informada")
        return

    count_validas = 0
    for item in atividades_sec:
        codigo_raw = str(item.get("codigo") or "").strip()
        if not codigo_raw:
            continue
        codigo_fmt = formatar_codigo_atividade(codigo_raw)
        nome = str(item.get("descricao") or "").strip()
        linha = f"{codigo_fmt} - {nome}".strip(" -")
        add_linha(linha)
        count_validas += 1

    if count_validas == 0:
        add_linha("Não informada")


# ── PDF rendering ─────────────────────────────────────────────────────────────

def html_to_pdf_bytes(html_str: str, base_dir: Path) -> bytes:
    import concurrent.futures

    def _generate():
        from playwright.sync_api import sync_playwright
        base_uri = base_dir.resolve().as_uri() + "/"
        wrapped = _wrap_with_base(html_str, base_uri)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(wrapped, wait_until="load")
            pdf_bytes = page.pdf(
                format="A4",
                margin={"top": "8mm", "right": "8mm", "bottom": "8mm", "left": "8mm"},
                print_background=True,
            )
            browser.close()
            return pdf_bytes

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_generate).result()


# ── Main public functions ─────────────────────────────────────────────────────

def gerar_pdf_cartao(data_raw: dict, template_html_path: str) -> bytes:
    """
    Generate CNPJ business card PDF from raw company API data.
    data_raw is the raw response from consulta_casa_dos_dados().
    template_html_path is the path to cartaocnpj.html.
    """
    template_path = Path(template_html_path)
    if not template_path.exists():
        raise RuntimeError(f"Template não encontrado: {template_path}")

    html_in = template_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html_in, "html.parser")

    # Fixed masked fields
    situacao_especial_txt = "********"
    situacao_especial_data_txt = "********"
    complemento = "********"

    # Phone
    telefone_txt = formatar_telefone_cartao(data_raw)

    # Legal nature
    nat_codigo_raw = str(data_raw.get("codigo_natureza_juridica") or "")
    nat_desc = str(data_raw.get("descricao_natureza_juridica") or "")
    if nat_codigo_raw and len(nat_codigo_raw) >= 4:
        nat_codigo_fmt = f"{nat_codigo_raw[:3]}-{nat_codigo_raw[3]}"
    else:
        nat_codigo_fmt = nat_codigo_raw
    natureza_txt = f"{nat_codigo_fmt} - {nat_desc}".strip(" -") or "********"

    # Company size
    porte_raw = data_raw.get("porte_empresa")
    if isinstance(porte_raw, dict):
        porte_desc = str(porte_raw.get("descricao") or "").upper()
    else:
        porte_desc = str(porte_raw or "").upper()
    if "MICRO EMPRESA" in porte_desc:
        porte = "ME"
    elif "PEQUENO PORTE" in porte_desc:
        porte = "EPP"
    else:
        porte = "********"

    # Registration status
    sit_cad_raw = data_raw.get("situacao_cadastral")
    if isinstance(sit_cad_raw, dict):
        situacao = up(str(sit_cad_raw.get("situacao_atual") or ""))
        situacao_data = formata_data(str(sit_cad_raw.get("data") or ""))
        motivo = str(sit_cad_raw.get("motivo") or "********")
    else:
        situacao = up(str(sit_cad_raw or ""))
        situacao_data = ""
        motivo = "********"

    # Email
    email_raw = data_raw.get("contato_email")
    if isinstance(email_raw, list) and email_raw:
        email = str(email_raw[0].get("email") or "")
    else:
        email = str(email_raw or "")
    email_txt = up(email)

    # Primary activity
    atv_princ_raw = data_raw.get("atividade_principal")
    if isinstance(atv_princ_raw, dict):
        cod_princ = formatar_codigo_atividade(str(atv_princ_raw.get("codigo") or ""))
        desc_princ = str(atv_princ_raw.get("descricao") or "")
    else:
        cod_princ = ""
        desc_princ = str(atv_princ_raw or "")
    atv_principal_txt = f"{cod_princ} - {desc_princ}".strip(" -") or "********"

    # Apply substitutions
    replace_numero_inscricao_by_class(soup, formata_cnpj(data_raw.get("cnpj")), data_raw.get("matriz_filial"))
    replace_after_label_single_b(soup, "DATA DE ABERTURA", formata_data(data_raw.get("data_abertura")))
    replace_after_label_single_b(soup, "NOME EMPRESARIAL", up(data_raw.get("razao_social")))
    replace_after_label_single_b(soup, "TÍTULO DO ESTABELECIMENTO", up(data_raw.get("nome_fantasia") or "********"))
    replace_after_label_single_b(soup, "PORTE", porte)
    replace_after_label_single_b(soup, "ATIVIDADE ECONÔMICA PRINCIPAL", atv_principal_txt)
    replace_atividades_secundarias(soup, data_raw.get("atividade_secundaria") or [])
    replace_after_label_single_b(soup, "NATUREZA JURÍDICA", natureza_txt)
    replace_after_label_single_b(soup, "LOGRADOURO", up(str(data_raw.get("endereco", {}).get("logradouro") or "")))
    replace_after_label_single_b(soup, "NÚMERO", str(data_raw.get("endereco", {}).get("numero") or ""))
    replace_after_label_single_b(soup, "COMPLEMENTO", complemento)
    replace_after_label_single_b(soup, "CEP", formata_cep(str(data_raw.get("endereco", {}).get("cep") or "")))
    replace_after_label_single_b(soup, "BAIRRO/DISTRITO", up(str(data_raw.get("endereco", {}).get("bairro") or "")))
    replace_after_label_single_b(soup, "MUNICÍPIO", up(str(data_raw.get("endereco", {}).get("municipio") or "")))
    replace_after_label_single_b(soup, "UF", up(str(data_raw.get("endereco", {}).get("uf") or "")))
    replace_after_label_single_b(soup, "ENDEREÇO ELETRÔNICO", email_txt)
    replace_after_label_single_b(soup, "TELEFONE", telefone_txt)
    replace_after_label_single_b(soup, "SITUAÇÃO CADASTRAL", situacao)
    replace_after_label_single_b(soup, "DATA DA SITUAÇÃO CADASTRAL", situacao_data)
    replace_after_label_single_b(soup, "MOTIVO DE SITUAÇÃO CADASTRAL", motivo)
    replace_after_label_single_b(soup, "SITUAÇÃO ESPECIAL", situacao_especial_txt)
    replace_after_label_single_b(soup, "DATA DA SITUAÇÃO ESPECIAL", situacao_especial_data_txt)

    inline_local_images(soup, base_dir=template_path.parent)
    return html_to_pdf_bytes(str(soup), base_dir=template_path.parent)


def gerar_cartao_cnpj_com_telefone(data_raw: dict, novo_telefone: str, template_html_path: str) -> bytes:
    """
    Generate PDF card with a custom phone number (used when SMS OTP requires
    a different phone than the company's original number).
    """
    tel_fmt = formatar_telefone_bruto(novo_telefone)
    data_mod = dict(data_raw)
    data_mod["contato_telefonico"] = [{
        "completo": tel_fmt,
        "tipo": "comercial",
        "whatsapp": False,
    }]
    return gerar_pdf_cartao(data_mod, template_html_path)
