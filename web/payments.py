"""Participant payments — provider abstraction + compliance helpers.

Scope (see COMPLIANCE.md §5): this pays STUDY SUBJECTS a reasonable, IRB/REB-
approved stipend for their time and travel. It NEVER pays a referral source and
is NEVER tied to enrollment. Amounts are set in advance per protocol.

Money movement goes through a pluggable Provider so a site can start on the
`manual` provider (records the disbursement + builds the auditable ledger, no
external API) and later drop in a real gift-card/ACH rail (Tremendous, Tango,
ClinCard, Greenphire) by implementing one `issue()` method and setting a key.
Nothing about the ledger, tax logic, or UI changes when the provider changes.
"""
import os
import uuid

# US IRS reporting threshold: aggregate participant payments >= $600 in a
# calendar year require a 1099 (and therefore a W-9 on file first).
IRS_1099_THRESHOLD_CENTS = 60000

# Undue-inducement guardrail. Per-visit stipends above this get flagged for a
# second look (IRBs scrutinize amounts large enough to be coercive). It is a
# WARNING, not a hard block — the IRB-approved amount is the source of truth.
UNDUE_INDUCEMENT_CENTS = int(os.environ.get("UNDUE_INDUCEMENT_CENTS", "20000"))

# Active provider. `manual` = record-only (no external disbursement); it still
# marks the payment issued and stamps a reference, which is exactly what a
# site needs to run the ledger before wiring a live rail.
PROVIDER = os.environ.get("PAYMENTS_PROVIDER", "manual").lower()


def format_cents(cents, currency="USD"):
    cents = int(cents or 0)
    sym = {"USD": "$", "CAD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(currency, "")
    return f"{sym}{cents / 100:,.2f}"


class PaymentProvider:
    key = "base"
    label = "Base"
    live = False

    def issue(self, payment):
        """Disburse a queued payment. Returns a dict:
        {ok, provider, provider_ref, status, error}. `status` is 'issued' on
        success (or 'paid' if the rail confirms instantly)."""
        raise NotImplementedError


class ManualProvider(PaymentProvider):
    """Record-only rail. Marks the payment issued and stamps a reference so the
    ledger + audit trail are complete. No real money moves; a coordinator hands
    over / mails the gift card and the record proves it happened."""
    key = "manual"
    label = "Manual / gift card (record-only)"
    live = False

    def issue(self, payment):
        ref = "MAN-" + uuid.uuid4().hex[:10].upper()
        return {"ok": True, "provider": self.key, "provider_ref": ref,
                "status": "issued", "error": ""}


# Real rails are added here once an account + API key exist. Each only needs an
# issue() that calls the vendor and returns the same dict shape. Example stub:
#
#   class TremendousProvider(PaymentProvider):
#       key = "tremendous"; label = "Tremendous"; live = True
#       def issue(self, payment):
#           # POST to Tremendous /orders with amount + recipient email, then
#           # return {"ok": True, "provider": "tremendous",
#           #         "provider_ref": order_id, "status": "issued", "error": ""}
#           ...
#
_REGISTRY = {p.key: p for p in (ManualProvider(),)}


def active_provider():
    return _REGISTRY.get(PROVIDER, _REGISTRY["manual"])


def provider_label():
    return active_provider().label


def provider_is_live():
    return active_provider().live


def issue_payment(payment):
    """Issue a single payment row through the active provider. Never raises;
    returns the provider result dict (with ok=False + error on failure)."""
    prov = active_provider()
    try:
        return prov.issue(payment)
    except Exception as e:  # noqa: BLE001 - surface any rail error to the ledger
        return {"ok": False, "provider": prov.key, "provider_ref": "",
                "status": "failed", "error": str(e)}
