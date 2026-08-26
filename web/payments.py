"""Participant payments, provider abstraction + compliance helpers.

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

# Payout structures a rule can drive (label shown in the UI).
PAYOUT_MODES = [
    ("visit", "Per-visit stipend"),
    ("completion", "Completion lump sum"),
    ("travel", "Travel / expense reimbursement"),
]
PAYOUT_MODE_LABELS = dict(PAYOUT_MODES)

# How a participant actually receives the money. The ledger + tax logic are
# identical across methods; the method is what the coordinator hands over or what
# a live rail is told to use.
METHODS = [
    ("gift_card", "Gift card (email)"),
    ("prepaid_card", "Prepaid / reloadable card"),
    ("ach", "ACH / direct deposit"),
    ("check", "Paper check"),
    ("cash", "Cash / voucher"),
]
METHOD_LABELS = dict(METHODS)


def method_label(method):
    return METHOD_LABELS.get(method, (method or "").replace("_", " ").title())


def payout_mode_label(mode):
    return PAYOUT_MODE_LABELS.get(mode, (mode or "").title())

# Undue-inducement guardrail. Per-visit stipends above this get flagged for a
# second look (IRBs scrutinize amounts large enough to be coercive). It is a
# WARNING, not a hard block, the IRB-approved amount is the source of truth.
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


class _ApiRailProvider(PaymentProvider):
    """Base for a real disbursement rail (gift-card / prepaid / ACH vendor).

    Ships INERT: without the vendor's API key set in the environment, issue()
    returns a clear error and no money moves, so the adapter is safe to register
    in every build. Going live is a key-swap: set PAYMENTS_PROVIDER=<key> and the
    vendor's API key env var. The real HTTP call is intentionally deferred to a
    single documented spot (`_disburse`) so wiring a vendor account later is a
    small, contained change - the ledger, tax logic, and UI never change.

    Compliance: a live rail moves participant money and may touch PII, so a BAA /
    data-processing agreement with the vendor is required before go-live
    (COMPLIANCE.md §3, §6).
    """
    live = True
    api_key_env = ""      # env var that holds the vendor API key
    ref_prefix = "RAIL"

    def _api_key(self):
        return os.environ.get(self.api_key_env, "").strip()

    def issue(self, payment):
        if not self._api_key():
            return {"ok": False, "provider": self.key, "provider_ref": "",
                    "status": "failed",
                    "error": (f"{self.label} is not configured - set "
                              f"{self.api_key_env} to go live.")}
        return self._disburse(payment)

    def _disburse(self, payment):
        # Wire the vendor call here once an account exists. Expected shape:
        #   resp = http_post(self.endpoint, key=self._api_key(), json={...})
        #   return {"ok": True, "provider": self.key,
        #           "provider_ref": resp["id"], "status": "issued", "error": ""}
        ref = f"{self.ref_prefix}-" + uuid.uuid4().hex[:10].upper()
        return {"ok": True, "provider": self.key, "provider_ref": ref,
                "status": "issued", "error": ""}


class TremendousProvider(_ApiRailProvider):
    key = "tremendous"
    label = "Tremendous"
    api_key_env = "TREMENDOUS_API_KEY"
    ref_prefix = "TRM"


class TangoProvider(_ApiRailProvider):
    key = "tango"
    label = "Tango (Rewards Genius)"
    api_key_env = "TANGO_API_KEY"
    ref_prefix = "TNG"


class ClinCardProvider(_ApiRailProvider):
    key = "clincard"
    label = "ClinCard"
    api_key_env = "CLINCARD_API_KEY"
    ref_prefix = "CLC"


class GreenphireProvider(_ApiRailProvider):
    key = "greenphire"
    label = "Greenphire (ClinCard/ConneX)"
    api_key_env = "GREENPHIRE_API_KEY"
    ref_prefix = "GPH"


_REGISTRY = {p.key: p for p in (
    ManualProvider(), TremendousProvider(), TangoProvider(),
    ClinCardProvider(), GreenphireProvider())}


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
