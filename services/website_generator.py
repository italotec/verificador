"""
AI-powered website HTML generation for CNPJ runs.
Generates a responsive retail clothing store site using company data.
Supports both Anthropic (Claude) and OpenAI (GPT) as providers.
"""
import re


def formatar_cnpj(cnpj_input) -> str:
    """Format CNPJ to 00.000.000/0000-00. Accepts str, int, dict, list, None."""
    if cnpj_input is None:
        return ""
    if isinstance(cnpj_input, dict):
        cnpj_input = cnpj_input.get("cnpj") or cnpj_input.get("cnpj_raiz") or ""
    if isinstance(cnpj_input, (list, tuple)) and cnpj_input:
        cnpj_input = cnpj_input[0]
    cnpj_str = str(cnpj_input).strip()
    apenas_digitos = re.sub(r"\D", "", cnpj_str)
    if len(apenas_digitos) != 14:
        return cnpj_str
    return (
        f"{apenas_digitos[:2]}."
        f"{apenas_digitos[2:5]}."
        f"{apenas_digitos[5:8]}/"
        f"{apenas_digitos[8:12]}-"
        f"{apenas_digitos[12:]}"
    )


def _build_prompt(cnpj_formatado: str, data_empresa: dict) -> str:
    return f"""
Crie um site completo de loja de roupas (vestuário e acessórios) com Tailwind CSS via CDN.
Use apenas HTML + Tailwind + imagens reais do Unsplash (URLs diretas).
O site deve ter no mínimo 5 seções bem definidas, ser 100% responsivo e ter um visual moderno em tons claros.

FOOTER OBRIGATÓRIO — inclua TODOS os campos abaixo, bem visíveis, em uma seção de rodapé escura:
CNPJ: {cnpj_formatado}
Razão Social: {data_empresa["razao_social"]}
E-mail: {data_empresa["email"]}
Telefone: <span id="telefone-comercial">{data_empresa["telefone"]}</span>
Logradouro: {data_empresa["logradouro"]}
Bairro: {data_empresa["bairro"]}
Município: {data_empresa["cidade"]}
Estado: {data_empresa["estado"]}

ATENÇÃO: O footer DEVE conter EXATAMENTE estes 8 campos, todos visíveis e legíveis no HTML final.
Não omita nenhum campo. Não invente dados. Exiba-os exatamente como fornecidos acima.
O elemento que exibe o Telefone DEVE ter id="telefone-comercial" (ex: <span id="telefone-comercial">...</span>).

Adicione links fictícios para "Termos de Uso" e "Política de Privacidade".
NUNCA repita layout de sites anteriores. Seja criativo e varie tudo: grid, cores complementares, tipografia, animações sutis.

RESPONDA APENAS COM O CÓDIGO COMPLETO. SEM MARKDOWN. SEM EXPLICAÇÃO. SEM ```.
""".strip()


def format_br_phone(digits: str) -> str:
    """Format raw digits (no country code) to Brazilian display format.
    11 digits → (DD) DDDDD-DDDD  (mobile)
    10 digits → (DD) DDDD-DDDD   (landline)
    """
    d = re.sub(r"\D", "", digits)
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return digits


def update_phone_in_html(html_content: str, new_phone_formatted: str) -> str:
    """Replace the phone number inside the telefone-comercial element.
    Primary:  looks for id="telefone-comercial" and replaces its text content.
    Fallback: looks for 'Telefone' label followed by a phone-like string.
    Returns the updated HTML (unchanged if phone element is not found).
    """
    # Primary: <tag ... id="telefone-comercial" ...>OLD</tag>
    updated = re.sub(
        r'(<[^>]+\bid=["\']telefone-comercial["\'][^>]*>)[^<]*(</)',
        rf'\g<1>{new_phone_formatted}\2',
        html_content,
    )
    if updated != html_content:
        return updated

    # Fallback: "Telefone" label followed by optional closing tag + digits/separators
    updated = re.sub(
        r'(Telefone[:\s]*(?:</[^>]+>\s*)?)[\d\s()\-\.]{7,20}',
        rf'\g<1>{new_phone_formatted}',
        html_content,
    )
    return updated


def gerar_html_loja_claude(anthropic_api_key: str, model: str, data_empresa: dict) -> str:
    """
    Generate a complete responsive HTML store site using Claude (Anthropic).
    data_empresa must have: cnpj, razao_social, email, telefone,
                            logradouro, bairro, cidade, estado
    Returns raw HTML string.
    """
    if not anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY não configurada.")

    if not isinstance(data_empresa, dict):
        raise TypeError(f"gerar_html_loja_claude esperava dict, recebeu {type(data_empresa)}")

    cnpj_formatado = formatar_cnpj(data_empresa.get("cnpj") or "")
    prompt = _build_prompt(cnpj_formatado, data_empresa)

    import anthropic
    client = anthropic.Anthropic(api_key=anthropic_api_key)
    message = client.messages.create(
        model=model,
        max_tokens=8192,
        system="Você é um gerador de código HTML. Responda somente com HTML+Tailwind completo, sem markdown.",
        messages=[{"role": "user", "content": prompt}],
    )
    html = message.content[0].text

    html = html.strip()
    if html.startswith("```"):
        lines = html.splitlines()
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        html = "\n".join(lines)

    return html


def gerar_html_loja_openai(openai_api_key: str, model: str, data_empresa: dict) -> str:
    """
    Generate a complete responsive HTML store site using OpenAI (GPT).
    data_empresa must have: cnpj, razao_social, email, telefone,
                            logradouro, bairro, cidade, estado
    Returns raw HTML string.
    """
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada.")

    if not isinstance(data_empresa, dict):
        raise TypeError(f"gerar_html_loja_openai esperava dict, recebeu {type(data_empresa)}")

    cnpj_formatado = formatar_cnpj(data_empresa.get("cnpj") or "")
    prompt = _build_prompt(cnpj_formatado, data_empresa)

    from openai import OpenAI
    client = OpenAI(api_key=openai_api_key)
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "Você é um gerador de código HTML. Responda somente com HTML+Tailwind completo, sem markdown."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )
    html = completion.choices[0].message.content

    html = html.strip()
    if html.startswith("```"):
        lines = html.splitlines()
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        html = "\n".join(lines)

    return html


def gerar_html_loja(provider: str, api_key: str, model: str, data_empresa: dict) -> str:
    """
    Dispatcher: routes to Claude or OpenAI based on provider ('anthropic' or 'openai').
    """
    if provider == "openai":
        return gerar_html_loja_openai(api_key, model, data_empresa)
    return gerar_html_loja_claude(api_key, model, data_empresa)
