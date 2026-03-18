"""
Profile creator — bulk-create AdsPower profiles via manual input or file upload.
Accessible at /profiles (admin only).
"""
import os
import sys
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

bp = Blueprint("profiles", __name__, url_prefix="/profiles")


def _is_admin():
    return current_user.is_authenticated and bool(getattr(current_user, "is_admin", False))


@bp.before_request
def guard():
    if not _is_admin():
        return redirect(url_for("dashboard.dashboard"))


def _get_client():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from services.adspower import AdsPowerClient
    return AdsPowerClient()


def _build_proxy_config(proxy_type: str, host: str, port: str, user: str, pw: str) -> dict | None:
    """Build an AdsPower proxy_config dict, or None for no-proxy."""
    if proxy_type in ("http", "socks5") and host and port:
        return {
            "proxy_soft":     "other",
            "proxy_type":     proxy_type,
            "proxy_host":     host,
            "proxy_port":     port,
            "proxy_user":     user,
            "proxy_password": pw,
        }
    return None


def _strip_scheme(host: str) -> str:
    """Strip http:// or socks5:// scheme prefix from a host string."""
    for prefix in ("socks5://", "http://", "http:"):
        if host.lower().startswith(prefix):
            return host[len(prefix):]
    return host


def _parse_proxies_file(text: str, proxy_type: str) -> list[dict]:
    """
    Parse a proxies .txt file.
    Accepts lines like:
      ip:port:user:pass
      http://ip:port:user:pass
      socks5://ip:port:user:pass
    proxy_type overrides scheme-based detection when a line has no prefix.
    """
    proxies = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Detect type from prefix
        detected = None
        l = line.lower()
        if l.startswith("socks5://"):
            detected = "socks5"
        elif l.startswith("http://") or l.startswith("http:"):
            detected = "http"

        # Strip scheme so we get bare ip:port:user:pass
        bare = _strip_scheme(line)
        parts = bare.split(":")
        if len(parts) < 2:
            continue

        ptype = detected or proxy_type or "http"
        proxies.append({
            "proxy_soft":     "other",
            "proxy_type":     ptype,
            "proxy_host":     parts[0].strip(),
            "proxy_port":     parts[1].strip(),
            "proxy_user":     parts[2].strip() if len(parts) > 2 else "",
            "proxy_password": parts[3].strip() if len(parts) > 3 else "",
        })
    return proxies


def _create_profiles_batch(client, group_id: str, accounts: list[dict],
                            proxies: list[dict]) -> tuple[list, list]:
    """
    Create profiles in AdsPower.
    Each account dict: {email, password, cookies, fakey, proxy_config (optional)}.
    proxies list is cycled if shorter than accounts (used only when account has no proxy_config).
    Returns (created_emails, failed_strings).
    """
    created, failed = [], []
    for i, acc in enumerate(accounts):
        proxy_cfg = acc.get("proxy_config")
        if proxy_cfg is None and proxies:
            proxy_cfg = proxies[i % len(proxies)]
        try:
            profile_id = client.create_profile(
                name=acc["email"],
                username=acc["email"],
                password=acc["password"],
                fakey=acc.get("fakey", ""),
                proxy_config=proxy_cfg,
                group_id=group_id,
            )
            if acc.get("cookies"):
                try:
                    client.update_profile(profile_id, cookie=acc["cookies"])
                except Exception:
                    pass  # non-fatal
            created.append(acc["email"])
        except Exception as e:
            failed.append(f"{acc['email']} — {e}")
    return created, failed


# ── Routes ───────────────────────────────────────────────────────────────────

@bp.route("/", methods=["GET"])
@login_required
def profiles_page():
    return render_template("profiles.html", title="Criar Perfis")


@bp.route("/create-manual", methods=["POST"])
@login_required
def profiles_create_manual():
    """Accepts JSON: {group_name, profiles: [{email, password, cookies, fakey,
       proxy_type, proxy_host, proxy_port, proxy_user, proxy_pass}]}"""
    body = request.get_json(silent=True) or {}
    group_name = (body.get("group_name") or "").strip()
    profiles   = body.get("profiles") or []

    if not group_name:
        return jsonify({"error": "group_name obrigatório"}), 400
    if not profiles:
        return jsonify({"error": "Nenhum perfil enviado"}), 400

    client = _get_client()
    try:
        group_id = client.get_group_id(group_name)
    except Exception as e:
        return jsonify({"error": f"Erro ao obter grupo: {e}"}), 500

    accounts = []
    for p in profiles:
        email = (p.get("email") or "").strip()
        pwd   = (p.get("password") or "").strip()
        if not email or not pwd:
            continue
        proxy_cfg = _build_proxy_config(
            p.get("proxy_type", "none"),
            (p.get("proxy_host") or "").strip(),
            (p.get("proxy_port") or "").strip(),
            (p.get("proxy_user") or "").strip(),
            (p.get("proxy_pass") or "").strip(),
        )
        accounts.append({
            "email":        email,
            "password":     pwd,
            "cookies":      (p.get("cookies") or "").strip(),
            "fakey":        (p.get("fakey") or "").strip(),
            "proxy_config": proxy_cfg,
        })

    created, failed = _create_profiles_batch(client, group_id, accounts, [])
    return jsonify({"created": created, "failed": failed})


@bp.route("/create-file", methods=["POST"])
@login_required
def profiles_create_file():
    """
    Accepts form fields:
      group_name, accounts_raw, col_email, col_password, col_cookies, col_fakey,
      proxies_raw, proxy_type
    """
    group_name   = (request.form.get("group_name") or "").strip()
    accounts_raw = request.form.get("accounts_raw", "")
    proxies_raw  = request.form.get("proxies_raw", "")
    proxy_type   = (request.form.get("proxy_type") or "http").strip()

    # Column indices (-1 = not mapped)
    def _col(name):
        v = request.form.get(name, "-1")
        try:
            return int(v)
        except (ValueError, TypeError):
            return -1

    col_email    = _col("col_email")
    col_password = _col("col_password")
    col_cookies  = _col("col_cookies")
    col_fakey    = _col("col_fakey")

    if not group_name:
        flash("Informe o nome do grupo.", "error")
        return redirect(url_for("profiles.profiles_page"))

    if col_email < 0 or col_password < 0:
        flash("Mapeie pelo menos as colunas Email e Senha.", "error")
        return redirect(url_for("profiles.profiles_page"))

    # Parse accounts using column mapping
    accounts = []
    for line in accounts_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        def _get(idx):
            if idx < 0 or idx >= len(parts):
                return ""
            return parts[idx].strip()
        email = _get(col_email)
        pwd   = _get(col_password)
        if not email or not pwd:
            continue
        accounts.append({
            "email":    email,
            "password": pwd,
            "cookies":  _get(col_cookies),
            "fakey":    _get(col_fakey),
            "proxy_config": None,
        })

    if not accounts:
        flash("Nenhuma conta válida encontrada com o mapeamento selecionado.", "error")
        return redirect(url_for("profiles.profiles_page"))

    proxies = _parse_proxies_file(proxies_raw, proxy_type) if proxies_raw.strip() else []

    client = _get_client()
    try:
        group_id = client.get_group_id(group_name)
    except Exception as e:
        flash(f"Erro ao obter/criar grupo '{group_name}': {e}", "error")
        return redirect(url_for("profiles.profiles_page"))

    created, failed = _create_profiles_batch(client, group_id, accounts, proxies)

    parts = []
    if created:
        parts.append(f"{len(created)} perfil(s) criado(s) com sucesso.")
    if failed:
        parts.append(f"{len(failed)} falhou: " + " | ".join(failed))

    category = "success" if created and not failed else ("error" if not created else "success")
    flash(" ".join(parts), category)
    return redirect(url_for("profiles.profiles_page"))
