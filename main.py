"""
Verificador agent — two modes of operation:

MODE 1 — create_and_verify(payload)
    Receives a JSON payload with Facebook account credentials + Gerador run_id,
    creates a new AdsPower profile, then immediately runs the full verification.

    Payload schema (all fields optional — defaults auto-selected):
    {
        "run_id":      123,             # omit → auto-claim from Gerador bank
                                        #         (or trigger generation if bank empty)
        "username":    "user@email.com",# omit → random account from accounts.json
        "password":    "secret",
        "fakey":       "TOTP_SECRET",   # optional 2-FA key
        "cookies":     "c_user=...;xs=...", # optional FB cookies (semicolon-separated)
        "proxy": {                      # omit → random proxy from proxies.json
            "proxy_soft": "other",
            "proxy_type": "socks5",
            "proxy_host": "1.2.3.4",
            "proxy_port": "1080",
            "proxy_user": "u",
            "proxy_password": "p"
        },
        "email_mode":  "own"            # "own" | "temp"  (default: "own")
    }

MODE 2 — process_verificar_group()
    Reads all AdsPower profiles in the "Verificar" group.
    Each profile remark must contain a GERADOR block:

        ---GERADOR---
        {"run_id": 123, "email_mode": "own"}

    After successful verification the profile is moved out of "Verificar"
    (group_id = "0") so it is not retried.

Usage (CLI):
    python main.py --mode 1 --payload path/to/payload.json
    python main.py --mode 2
"""
import argparse
import json
import random
import sys
from datetime import datetime

import os

import config
from services.adspower import AdsPowerClient
from services.sms_factory import get_sms_service
from services.facebook_bot import FacebookBot


# ── shared service instances ─────────────────────────────────────────────────

adspower = AdsPowerClient(config.ADSPOWER_BASE)


def _make_gerador():
    vps_url = os.getenv("VPS_URL")
    worker_key = os.getenv("WORKER_API_KEY")
    if vps_url and worker_key:
        from services.gerador_remote_client import GeradorRemoteClient
        return GeradorRemoteClient(vps_url, worker_key)
    from services.gerador_facade import GeradorService
    return GeradorService()


gerador = _make_gerador()
# SMS service is resolved per-job (not at import time) so admin provider changes
# take effect immediately without restarting the agent.
# Do NOT cache this at module level.


# ── proxy / account loaders ───────────────────────────────────────────────────

def _load_proxies() -> list[dict]:
    """
    Load proxies from PROXIES_FILE (list of "ip:port:user:pass" strings).
    Returns a list of AdsPower-ready proxy config dicts.
    """
    with open(config.PROXIES_FILE, encoding="utf-8") as f:
        raw: list[str] = json.load(f)

    result = []
    for entry in raw:
        parts = entry.strip().split(":")
        # Supported formats:
        #   ip:port:user:pass          → type defaults to "http"
        #   ip:port:user:pass:type     → explicit type (http | https | socks5)
        if len(parts) == 4:
            host, port, user, password = parts
            proxy_type = "http"
        elif len(parts) == 5:
            host, port, user, password, proxy_type = parts
        else:
            print(f"[PROXIES] Skipping malformed entry: {entry}")
            continue
        result.append({
            "proxy_soft": "other",
            "proxy_type": proxy_type,
            "proxy_host": host,
            "proxy_port": port,
            "proxy_user": user,
            "proxy_password": password,
        })
    return result


def _load_accounts() -> list[dict]:
    """
    Load Facebook accounts from ACCOUNTS_FILE.
    Each entry: {"username": ..., "password": ..., "fakey": ..., "cookies": ...}
    """
    with open(config.ACCOUNTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def _pick_proxy(index: int | None = None) -> dict | None:
    """Pick a proxy by index or randomly. Returns None if no proxies available."""
    proxies = _load_proxies()
    if not proxies:
        return None
    if index is not None:
        return proxies[index % len(proxies)]
    return random.choice(proxies)


def _pick_account(index: int | None = None) -> dict:
    """Pick an account by index or randomly. Raises if no accounts available."""
    accounts = _load_accounts()
    if not accounts:
        raise RuntimeError(f"No accounts found in {config.ACCOUNTS_FILE}")
    if index is not None:
        return accounts[index % len(accounts)]
    return random.choice(accounts)


# ── Gerador run acquisition ───────────────────────────────────────────────────

def _acquire_run_id() -> int:
    """
    Claim a pre-generated run from the bank, or generate one synchronously.
    Returns the run_id.
    """
    print("[GERADOR] Requesting a run…")
    result = gerador.acquire_run()
    run_id = result["run_id"]
    source = result.get("source", "local")
    print(f"[GERADOR] Got run {run_id} (source: {source})")
    return run_id


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_gerador_block(remark: str) -> dict | None:
    """
    Extract the JSON dict stored after the GERADOR_REMARK_MARKER in the remark.
    Returns None if marker is absent.
    """
    marker = config.GERADOR_REMARK_MARKER
    if marker not in remark:
        return None
    _, _, tail = remark.partition(marker)
    try:
        return json.loads(tail.strip())
    except Exception:
        return None


def _mark_restricted(user_id: str):
    """
    When a BM is detected as restricted:
      1. Append ---RESTRITA--- marker to the remark.
      2. Move the profile to the 'Restrita' group.
    """
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        current_remark = adspower.get_profile(user_id).get("remark", "")
    except Exception as e:
        print(f"[VERIFICADOR] Could not fetch remark for {user_id}: {e}")
        current_remark = ""

    new_remark = current_remark.rstrip() + f"\n\n{config.RESTRITA_REMARK_MARKER}\n{timestamp}"
    try:
        adspower.update_profile(user_id, remark=new_remark)
        print(f"[VERIFICADOR] Tagged '{config.RESTRITA_REMARK_MARKER}' on profile {user_id}")
    except Exception as e:
        print(f"[VERIFICADOR] Could not tag remark for {user_id}: {e}")

    try:
        restrita_id = adspower.get_group_id(config.RESTRITA_GROUP_NAME)
        adspower.move_to_group(user_id, restrita_id)
        print(f"[VERIFICADOR] Moved {user_id} to '{config.RESTRITA_GROUP_NAME}'")
    except Exception as e:
        print(f"[VERIFICADOR] Could not move {user_id} to Restrita: {e}")


def _mark_verified(user_id: str):
    """
    After a BM is successfully sent to review:
      1. Fetch the latest remark (may have been updated by the bot's step flags).
      2. Append ---VERIFICADA--- + ISO timestamp so the profile is visibly tagged.
      3. Move the profile to the 'Verificadas' group.
    """
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Fetch the current remark so any step flags written by the bot are included
    try:
        current_remark = adspower.get_profile(user_id).get("remark", "")
    except Exception as e:
        print(f"[VERIFICADOR] Could not fetch remark for {user_id}: {e}")
        current_remark = ""

    new_remark = current_remark.rstrip() + f"\n\n{config.VERIFICADA_REMARK_MARKER}\n{timestamp}"
    try:
        adspower.update_profile(user_id, remark=new_remark)
        print(f"[VERIFICADOR] Tagged '{config.VERIFICADA_REMARK_MARKER}' on profile {user_id}")
    except Exception as e:
        print(f"[VERIFICADOR] Could not tag remark for {user_id}: {e}")

    try:
        verificadas_id = adspower.get_group_id(config.VERIFICADAS_GROUP_NAME)
        adspower.move_to_group(user_id, verificadas_id)
        print(f"[VERIFICADOR] Moved {user_id} to '{config.VERIFICADAS_GROUP_NAME}'")
    except Exception as e:
        print(f"[VERIFICADOR] Could not move {user_id} to Verificadas: {e}")


def _run_for_profile(
    profile: dict,
    run_id: int,
    email_mode: str = "own",
    business_id: str = "",
    gerador_data: dict | None = None,
    sms_payload: dict | None = None,
) -> bool:
    """
    Open the AdsPower browser for *profile*, run the full Facebook verification,
    then close the browser.  Returns True on success.
    """
    # Resolve business_id from gerador_data if not explicitly passed by caller
    business_id = business_id or (gerador_data or {}).get("business_id", "")

    user_id = profile["user_id"]
    print(f"\n[VERIFICADOR] Processing profile {user_id} — {profile.get('name', '')}")

    # Fetch company data from Gerador
    try:
        run_data = gerador.get_run(run_id)
        run_data["run_id"] = run_id  # ensure it's in the dict
        # Always read from DB. Caller (jobs.py / Celery) may already have an app context;
        # agent.py does not, so we lazily push one for this lookup.
        from flask import has_app_context
        from web_app.models import SystemSetting as _SS
        if has_app_context():
            run_data["domain_verification_method"] = _SS.get("DOMAIN_VERIFICATION_METHOD", "meta_tag")
        else:
            from web_app import create_app
            with create_app().app_context():
                run_data["domain_verification_method"] = _SS.get("DOMAIN_VERIFICATION_METHOD", "meta_tag")
    except Exception as e:
        raise RuntimeError(f"Falha ao buscar dados do Gerador (run {run_id}): {e}") from e

    # Open browser
    try:
        browser_info = adspower.open_browser(user_id)
        ws = browser_info["ws"]["puppeteer"]
    except Exception as e:
        raise RuntimeError(f"Falha ao abrir browser AdsPower para {user_id}: {e}") from e

    # Credentials
    username = profile.get("username", "")
    password = profile.get("password", "")
    fakey = profile.get("fakey", "")

    # Cookies: prefer an explicit field; fall back to scanning the remark
    cookies = profile.get("cookies", "")
    if not cookies:
        remark = profile.get("remark", "")
        pipe_parts = remark.split("|")
        for part in pipe_parts:
            if "c_user=" in part:
                cookies = part.strip()
                break

    try:
        bot = FacebookBot(
            ws_endpoint=ws,
            run_data=run_data,
            gerador=gerador,
            sms=get_sms_service(sms_payload),
            email_mode=email_mode,
            sms_timeout=config.SMS_WAIT_TIMEOUT,
            sms_max_attempts=config.SMS_MAX_ATTEMPTS,
            adspower_client=adspower,
            profile_user_id=user_id,
            profile_remark=profile.get("remark", ""),
            gerador_data=gerador_data or {},
        )
        bot.run_verification(username, password, fakey, cookies, business_id=business_id)
    except Exception as e:
        adspower.close_browser(user_id)
        raise
    else:
        adspower.close_browser(user_id)

    print(f"[VERIFICADOR] ✓ {user_id} verified successfully")
    return True


# ── mode 1 ────────────────────────────────────────────────────────────────────

def create_and_verify(payload: dict) -> bool:
    """
    Create a new AdsPower profile from *payload* and immediately verify it.

    - run_id is optional: if omitted, a CNPJ run is auto-acquired from the
      Gerador bank (or generated on-demand if the bank is empty).
    - Proxy and account fields are optional: loaded from proxies.json /
      accounts.json when not present in the payload.

    Minimal payload: {}   (run_id, account, and proxy all auto-selected)
    Full payload schema: see module docstring.
    """
    email_mode = payload.get("email_mode", "own")

    # run_id: use payload value if given, otherwise claim/generate from Gerador
    if "run_id" in payload:
        run_id = payload["run_id"]
    else:
        run_id = _acquire_run_id()
        if run_id is None:
            print("[MODE1] Could not acquire a run from Gerador — aborting")
            return False

    # Account: use payload fields if present, otherwise pick from accounts.json
    if "username" in payload:
        username = payload["username"]
        password = payload["password"]
        fakey = payload.get("fakey", "")
        cookies = payload.get("cookies", "")
    else:
        account = _pick_account()
        username = account["username"]
        password = account["password"]
        fakey = account.get("fakey", "")
        cookies = account.get("cookies", "")

    # Proxy: use payload field if present, otherwise pick from proxies.json
    proxy_cfg = payload.get("proxy") or _pick_proxy()

    # Fetch company data to build the profile name
    run_data = gerador.get_run(run_id)

    # Encode Gerador data into the remark
    gerador_block = json.dumps({"run_id": run_id, "email_mode": email_mode})
    remark = f"{run_data['razao_social']}\n\n{config.GERADOR_REMARK_MARKER}\n{gerador_block}"

    # Create profile in AdsPower
    try:
        group_id = adspower.get_group_id(config.VERIFICAR_GROUP_NAME)
        profile_id = adspower.create_profile(
            name=run_data["razao_social"][:80],
            username=username,
            password=password,
            fakey=fakey,
            proxy_config=proxy_cfg,
            group_id=group_id,
            remark=remark,
            platform="facebook.com",
        )
        print(f"[MODE1] Created profile {profile_id}")
    except Exception as e:
        print(f"[MODE1] Profile creation failed: {e}")
        return False

    profile = {
        "user_id": profile_id,
        "username": username,
        "password": password,
        "fakey": fakey,
        "cookies": cookies,
        "remark": remark,
    }

    initial_gerador_data = {"run_id": run_id, "email_mode": email_mode}
    try:
        _run_for_profile(profile, run_id, email_mode, gerador_data=initial_gerador_data)
        _mark_verified(profile_id)
        return True
    except RuntimeError as e:
        print(f"[MODE1] Failed: {e}")
        return False


# ── mode 2 ────────────────────────────────────────────────────────────────────

def process_verificar_group() -> dict:
    """
    Process all profiles in the "Verificar" AdsPower group.
    Returns a summary dict: {"processed": N, "success": M, "failed": K}
    """
    try:
        group_id = adspower.get_group_id(config.VERIFICAR_GROUP_NAME)
    except Exception as e:
        print(f"[MODE2] Could not find/create group: {e}")
        return {"processed": 0, "success": 0, "failed": 0}

    profiles = adspower.list_profiles(group_id=group_id)
    print(f"[MODE2] Found {len(profiles)} profiles in '{config.VERIFICAR_GROUP_NAME}'")

    results = {"processed": 0, "success": 0, "failed": 0}

    for profile in profiles:
        results["processed"] += 1
        remark = profile.get("remark", "")
        gerador_data = _parse_gerador_block(remark)

        if not gerador_data or "run_id" not in gerador_data:
            print(f"[MODE2] Profile {profile['user_id']} has no GERADOR block — auto-assigning run")
            run_id = _acquire_run_id()
            if run_id is None:
                print(f"[MODE2] Could not acquire run for {profile['user_id']} — skipping")
                results["failed"] += 1
                continue
            email_mode = "own"
            gerador_data = {"run_id": run_id, "email_mode": email_mode}
            new_remark = remark.rstrip() + f"\n\n{config.GERADOR_REMARK_MARKER}\n{json.dumps(gerador_data)}"
            try:
                adspower.update_profile(profile["user_id"], remark=new_remark)
                profile = dict(profile)
                profile["remark"] = new_remark
                remark = new_remark
                print(f"[MODE2] Auto-assigned run_id {run_id} to profile {profile['user_id']}")
            except Exception as e:
                print(f"[MODE2] Could not write GERADOR block to profile {profile['user_id']}: {e}")
                results["failed"] += 1
                continue

        run_id = gerador_data["run_id"]
        email_mode = gerador_data.get("email_mode", "own")
        business_id = gerador_data.get("business_id", "")

        try:
            _run_for_profile(profile, run_id, email_mode, business_id=business_id, gerador_data=gerador_data)
            results["success"] += 1
            _mark_verified(profile["user_id"])
        except RuntimeError as e:
            print(f"[MODE2] Failed {profile['user_id']}: {e}")
            results["failed"] += 1

    print(f"\n[MODE2] Done — {results}")
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Verificador agent")
    parser.add_argument(
        "--mode",
        type=int,
        choices=[1, 2],
        required=True,
        help="1 = create new profile + verify  |  2 = process 'Verificar' group",
    )
    parser.add_argument(
        "--payload",
        type=str,
        help="(Mode 1 only) path to JSON file with account + run_id data",
    )
    args = parser.parse_args()

    if args.mode == 1:
        payload = {}
        if args.payload:
            with open(args.payload, encoding="utf-8") as f:
                payload = json.load(f)
        ok = create_and_verify(payload)
        sys.exit(0 if ok else 1)

    elif args.mode == 2:
        summary = process_verificar_group()
        sys.exit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
