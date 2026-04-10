"""
CloudPanel VPS deployment for CNPJ-generated websites.
Handles DNS via Spaceship API + site creation + Nginx config via SSH.
Ported from Gerador CNPJ/services/cloudpanel_deploy.py.
All credentials moved to config.py / .env (no more hardcoded secrets).
"""
import json
import random
import re
import shlex
import time
import unicodedata
from typing import Any

import paramiko
import requests


# ── Spaceship DNS API ─────────────────────────────────────────────────────────

def _spaceship_headers(api_key: str, api_secret: str) -> dict:
    return {
        "X-API-Key": api_key,
        "X-API-Secret": api_secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def ss_request_json(
    method: str,
    path: str,
    api_key: str,
    api_secret: str,
    *,
    params=None,
    json_body=None,
    api_base: str = "https://spaceship.dev/api/v1",
    retries: int = 4,
    timeout: int = 40,
) -> Any:
    url = f"{api_base}{path}"
    headers = _spaceship_headers(api_key, api_secret)

    for attempt in range(retries):
        try:
            r = requests.request(
                method, url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout,
            )

            if r.status_code == 401:
                print(f"[SPACESHIP] 401 Unauthorized — verifique as credenciais.")
                raise RuntimeError("Spaceship: credenciais inválidas (401)")

            if r.status_code == 202:
                op_id = r.headers.get("spaceship-async-operationid", "N/A")
                print(f"[SPACESHIP] Operação async enfileirada (ID: {op_id})")
                return None

            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 60))
                print(f"[SPACESHIP] Rate limit — aguardando {retry_after}s...")
                time.sleep(retry_after)
                continue

            if 500 <= r.status_code < 600:
                time.sleep(2 ** attempt)
                continue

            r.raise_for_status()
            return r.json() if r.text.strip() else None

        except Exception as e:
            print(f"[SPACESHIP] Tentativa {attempt + 1} falhou: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"Spaceship: falha total após {retries} tentativas")


def configurar_dns_subdominio(
    parent_domain: str,
    sub_label: str,
    vps_ip: str,
    api_key: str,
    api_secret: str,
) -> bool:
    """Add A records for sub_label.parent_domain and www.sub_label.parent_domain."""
    records = [
        {"type": "A", "name": sub_label,           "address": vps_ip, "ttl": 300},
        {"type": "A", "name": f"www.{sub_label}",  "address": vps_ip, "ttl": 300},
    ]
    payload = {"force": True, "items": records}

    try:
        headers = _spaceship_headers(api_key, api_secret)
        r = requests.put(
            f"https://spaceship.dev/api/v1/dns/records/{parent_domain}",
            headers=headers,
            json=payload,
            timeout=40,
        )
        if r.status_code in (200, 202, 204):
            print(f"[DNS] {sub_label}.{parent_domain} criado com sucesso")
            return True
        print(f"[DNS] Erro {r.status_code}: {r.text[:300]}")
        return False
    except Exception as e:
        print(f"[DNS] Falha: {e}")
        return False


# ── Subdomain name generation ─────────────────────────────────────────────────

def limpar_para_subdominio(razao_social: str) -> str:
    texto = unicodedata.normalize("NFD", razao_social)
    texto = "".join(ch for ch in texto if unicodedata.category(ch) != "Mn")
    texto = re.sub(r"[^a-zA-Z0-9\s]", "", texto.lower())
    texto = re.sub(r"\s+", "-", texto.strip())
    texto = re.sub(r"-+", "-", texto)
    texto = texto[:40].strip("-")
    return texto or "empresa"


def gerar_subdominio(razao_social: str, dominios: list[str]) -> tuple[str, str, str]:
    """Returns (sub_label, parent_domain, fqdn)."""
    sub_label = limpar_para_subdominio(razao_social)
    parent_domain = random.choice(dominios)
    fqdn = f"{sub_label}.{parent_domain}"
    return sub_label, parent_domain, fqdn


# ── CloudPanel SSH deployment ─────────────────────────────────────────────────

def deploy_no_cloudpanel(
    dominio: str,
    html_content: str,
    vps_ip: str,
    vps_user: str,
    vps_pass: str,
    site_pass: str,
    php_version: str = "8.3",
) -> str | None:
    """
    Create a new CloudPanel site for dominio, configure SSL from wildcard cert,
    deploy index.html, and restart Nginx. Returns the https URL or None on failure.
    """
    wildcard_base = ".".join(dominio.split(".")[-2:])
    wildcard_fullchain = f"/etc/letsencrypt/live/{wildcard_base}/fullchain.pem"
    wildcard_privkey = f"/etc/letsencrypt/live/{wildcard_base}/privkey.pem"
    ssl_crt = f"/etc/nginx/ssl-certificates/{dominio}.crt"
    ssl_key = f"/etc/nginx/ssl-certificates/{dominio}.key"

    domain_no_tld = dominio.split(".")[0][:32]
    htdocs_path = f"/home/{domain_no_tld}/htdocs/{dominio}"
    vhost_enabled = f"/etc/nginx/sites-enabled/{dominio}.conf"

    safe_html = html_content.replace("EOF", "EO_F")

    comandos = [
        f"sudo clpctl site:add:php --domainName={shlex.quote(dominio)} "
        f"--phpVersion={shlex.quote(php_version)} --vhostTemplate='Generic' "
        f"--siteUser={shlex.quote(domain_no_tld)} --siteUserPassword={shlex.quote(site_pass)}",

        f"sudo test -f {shlex.quote(vhost_enabled)} || "
        f"(echo '[ERRO] vhost não encontrado: {vhost_enabled}' && exit 1)",

        f"sudo cp {shlex.quote(wildcard_fullchain)} {shlex.quote(ssl_crt)}",
        f"sudo cp {shlex.quote(wildcard_privkey)} {shlex.quote(ssl_key)}",

        f"sudo sed -i "
        f"-e 's#^\\s*ssl_certificate\\s\\+.*;#  ssl_certificate {ssl_crt};#' "
        f"-e 's#^\\s*ssl_certificate_key\\s\\+.*;#  ssl_certificate_key {ssl_key};#' "
        f"{shlex.quote(vhost_enabled)}",

        f"sudo rm -f {shlex.quote(htdocs_path)}/index.php || true",
        f"sudo mkdir -p {shlex.quote(htdocs_path)}",
        f"sudo tee {shlex.quote(htdocs_path)}/index.html > /dev/null << 'EOF'\n{safe_html}\nEOF",
        f"sudo chown -R {shlex.quote(domain_no_tld)}:{shlex.quote(domain_no_tld)} {shlex.quote(htdocs_path)}",
        "sudo nginx -t",
        "sudo systemctl restart nginx",
    ]

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(vps_ip, username=vps_user, password=vps_pass, timeout=30)
        for cmd in comandos:
            stdin, stdout, stderr = client.exec_command(cmd)
            out = stdout.read().decode(errors="ignore").strip()
            err = stderr.read().decode(errors="ignore").strip()
            if out:
                print(f"[SSH] {out}")
            if err:
                ign = ("already exists", "already installed", "warn", "warning")
                if not any(x in err.lower() for x in ign):
                    print(f"[SSH ERR] {err}")
            time.sleep(1)

        url_final = f"https://{dominio}"
        print(f"[DEPLOY] Site publicado: {url_final}")
        return url_final

    except Exception as e:
        print(f"[DEPLOY] Falha SSH: {e}")
        return None
    finally:
        client.close()


def atualizar_index_html_no_cloudpanel(
    dominio: str,
    novo_html: str,
    vps_ip: str,
    vps_user: str,
    vps_pass: str,
) -> bool:
    """
    Push updated index.html to an existing CloudPanel site via SSH.
    Used by inject_meta_tag and website regeneration.
    """
    domain_no_tld = dominio.split(".")[0][:32]
    index_path = f"/home/{domain_no_tld}/htdocs/{dominio}/index.html"
    safe_html = novo_html.replace("EOF", "EO_F")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(vps_ip, username=vps_user, password=vps_pass, timeout=30)

        comandos = [
            f"sudo tee {index_path} > /dev/null << 'EOF'\n{safe_html}\nEOF",
            f"sudo chown {domain_no_tld}:{domain_no_tld} {index_path}",
        ]
        for cmd in comandos:
            stdin, stdout, stderr = client.exec_command(cmd)
            stdout.read()
            stderr.read()

        print(f"[SSH UPDATE] HTML atualizado em {dominio}")
        return True

    except Exception as e:
        print(f"[SSH UPDATE] Falha: {e}")
        return False
    finally:
        client.close()


# ── Main entry point ──────────────────────────────────────────────────────────

def publicar_em_subdominio_proprio(
    razao_social: str,
    html_content: str,
    vps_ip: str,
    vps_user: str,
    vps_pass: str,
    site_pass: str,
    spaceship_api_key: str,
    spaceship_api_secret: str,
    dominios: list[str],
    php_version: str = "8.3",
) -> str | None:
    """
    Full deployment pipeline:
    1. Generate sanitized subdomain from company name
    2. Configure DNS A records via Spaceship API
    3. Wait 15s for DNS propagation
    4. Create CloudPanel site + configure SSL + deploy HTML
    Returns https URL or None on failure.
    """
    if not dominios:
        print("[DEPLOY] Nenhum domínio configurado em CLOUDPANEL_DOMAINS")
        return None

    sub_label, parent_domain, fqdn = gerar_subdominio(razao_social, dominios)
    print(f"[DEPLOY] Subdomínio: {fqdn}")

    if not configurar_dns_subdominio(parent_domain, sub_label, vps_ip, spaceship_api_key, spaceship_api_secret):
        print("[DEPLOY] Falha ao configurar DNS")
        return None

    time.sleep(15)

    return deploy_no_cloudpanel(fqdn, html_content, vps_ip, vps_user, vps_pass, site_pass, php_version)
