"""
Status lifecycle engine for WABA records.

Manages all status transitions and enforces valid state changes.
Records every transition in the StatusTransition audit table.
"""

from datetime import datetime, timedelta
from web_app import db
from web_app.models import (
    WabaRecord, StatusTransition,
    WABA_STATUS_AGUARDANDO, WABA_STATUS_EXECUTANDO, WABA_STATUS_EM_REVISAO,
    WABA_STATUS_NAO_VERIFICOU, WABA_STATUS_MONITORANDO_LIMITE,
    WABA_STATUS_250, WABA_STATUS_2K, WABA_STATUS_RESTRITA,
    WABA_STATUS_DESATIVADA, WABA_STATUS_ERRO,
)


VALID_TRANSITIONS = {
    WABA_STATUS_AGUARDANDO:         [WABA_STATUS_EXECUTANDO, WABA_STATUS_ERRO],
    WABA_STATUS_EXECUTANDO:         [WABA_STATUS_EM_REVISAO, WABA_STATUS_ERRO, WABA_STATUS_RESTRITA, WABA_STATUS_DESATIVADA],
    WABA_STATUS_EM_REVISAO:         [WABA_STATUS_MONITORANDO_LIMITE, WABA_STATUS_NAO_VERIFICOU, WABA_STATUS_RESTRITA, WABA_STATUS_DESATIVADA],
    WABA_STATUS_NAO_VERIFICOU:      [WABA_STATUS_MONITORANDO_LIMITE, WABA_STATUS_RESTRITA, WABA_STATUS_DESATIVADA],
    WABA_STATUS_MONITORANDO_LIMITE: [WABA_STATUS_250, WABA_STATUS_2K, WABA_STATUS_RESTRITA, WABA_STATUS_DESATIVADA],
    WABA_STATUS_250:                [WABA_STATUS_RESTRITA, WABA_STATUS_DESATIVADA],
    WABA_STATUS_2K:                 [WABA_STATUS_RESTRITA, WABA_STATUS_DESATIVADA],
    WABA_STATUS_RESTRITA:           [WABA_STATUS_DESATIVADA],
    WABA_STATUS_DESATIVADA:         [],
    WABA_STATUS_ERRO:               [WABA_STATUS_AGUARDANDO],  # retry
}

# How long a WABA can stay in "em_revisao" before it becomes "nao_verificou"
REVIEW_TIMEOUT_HOURS = 24

# How many days at TIER_250 before classifying as "250"
LIMIT_250_DAYS = 4


class StatusManager:
    """Manages WABA status transitions with validation and audit logging."""

    @staticmethod
    def transition(waba: WabaRecord, new_status: str, reason: str | None = None, *, force: bool = False) -> bool:
        """
        Transition a WabaRecord to a new status.
        Returns True if transition succeeded, False if invalid.
        """
        old_status = waba.status

        if old_status == new_status:
            return True  # no-op

        if not force:
            allowed = VALID_TRANSITIONS.get(old_status, [])
            if new_status not in allowed:
                return False

        waba.status = new_status
        waba.updated_at = datetime.utcnow()

        # Set specific timestamps based on new status
        if new_status == WABA_STATUS_EM_REVISAO and not waba.submitted_at:
            waba.submitted_at = datetime.utcnow()
        elif new_status == WABA_STATUS_MONITORANDO_LIMITE and not waba.verified_at:
            waba.verified_at = datetime.utcnow()
        elif new_status == WABA_STATUS_RESTRITA:
            waba.restricted_at = datetime.utcnow()
        elif new_status == WABA_STATUS_DESATIVADA:
            waba.disabled_at = datetime.utcnow()

        # Record transition
        transition = StatusTransition(
            waba_record_id=waba.id,
            from_status=old_status,
            to_status=new_status,
            reason=reason,
        )
        db.session.add(transition)
        db.session.commit()
        return True

    @staticmethod
    def check_review_timeout(waba: WabaRecord) -> bool:
        """
        If a WABA has been in 'em_revisao' for 24+ hours, move to 'nao_verificou'.
        Returns True if transition happened.
        """
        if waba.status != WABA_STATUS_EM_REVISAO:
            return False

        submitted = waba.submitted_at or waba.updated_at
        if datetime.utcnow() - submitted > timedelta(hours=REVIEW_TIMEOUT_HOURS):
            return StatusManager.transition(
                waba, WABA_STATUS_NAO_VERIFICOU,
                reason=f"Tempo limite de {REVIEW_TIMEOUT_HOURS}h em revisão excedido"
            )
        return False

    @staticmethod
    def evaluate_limit(waba: WabaRecord, tier: str) -> bool:
        """
        Evaluate the messaging limit tier and transition accordingly.
        - TIER_250 for 4+ days → status '250'
        - TIER_1K or higher → status '2k'
        Returns True if a transition happened.
        """
        waba.messaging_limit = tier
        waba.last_limit_check = datetime.utcnow()

        if waba.status != WABA_STATUS_MONITORANDO_LIMITE:
            db.session.commit()
            return False

        # Check for upgrade to 2K+
        high_tiers = {"TIER_1K", "TIER_2K", "TIER_10K", "TIER_100K", "TIER_UNLIMITED"}
        if tier in high_tiers:
            return StatusManager.transition(
                waba, WABA_STATUS_2K,
                reason=f"Limite atualizado para {tier}"
            )

        # Check for 250 classification (4+ days stuck)
        if tier == "TIER_250":
            if not waba.limit_first_seen_at:
                waba.limit_first_seen_at = datetime.utcnow()
                db.session.commit()
                return False

            days_at_250 = (datetime.utcnow() - waba.limit_first_seen_at).days
            if days_at_250 >= LIMIT_250_DAYS:
                return StatusManager.transition(
                    waba, WABA_STATUS_250,
                    reason=f"Limite em 250 por {days_at_250} dias (>= {LIMIT_250_DAYS})"
                )

        db.session.commit()
        return False

    @staticmethod
    def detect_restriction(waba: WabaRecord, *, restricted: bool = False, disabled: bool = False) -> bool:
        """
        Check for restricted/disabled status from browser detection results.
        Returns True if a transition happened.
        """
        if disabled:
            return StatusManager.transition(
                waba, WABA_STATUS_DESATIVADA,
                reason="Conta desativada detectada via verificação do navegador"
            )
        if restricted:
            return StatusManager.transition(
                waba, WABA_STATUS_RESTRITA,
                reason="Conta restrita detectada via verificação do navegador"
            )
        return False

    @staticmethod
    def check_all_review_timeouts():
        """Check all WABAs in em_revisao for timeout. Called by Celery Beat."""
        wabas = WabaRecord.query.filter_by(status=WABA_STATUS_EM_REVISAO).all()
        count = 0
        for waba in wabas:
            if StatusManager.check_review_timeout(waba):
                count += 1
        return count
