"""
services/payment.py
=====================
Module 4 (Payment Collection) per the Terms of Reference:
"Collect payment by M-Pesa, card or cash, and open the barrier only on
confirmed payment."

This is a mock gateway -- it simulates the STK push / card authorisation
so the whole flow can be demonstrated and graded without a real payment
provider. The important design point for the assignment is the *shape* of
the flow, which is exactly what a real M-Pesa Daraja / card integration
would look like:

ALGORITHM: initiate_payment(session, method)
  1. Create a Payment row, status='pending', with a mock reference code.
  2. If method == 'cash': mark it 'confirmed' immediately (attendant has
     physically received the cash) and go straight to step 4.
  3. If method in ('mpesa', 'card'): in a real system we would call the
     provider's API here and return, waiting for a webhook/callback to
     confirm. In this simulation we confirm immediately for demo purposes,
     but the state machine (pending -> confirmed/failed) is what a real
     async confirmation would flip.
  4. On confirmed: append a 'payment' audit event with the amount, then
     call slot_manager.open_barrier_for(session) -- the barrier is
     PHYSICALLY GATED on this call, i.e. it is unreachable from any code
     path that has not gone through a confirmed Payment row.
  5. On failed: append a 'payment_failed' audit event, session stays
     'awaiting_payment' so the driver can immediately retry with the
     same or a different method -- this is the exception-handling path
     for a declined card, an expired M-Pesa STK prompt, or a network
     timeout to the provider.

EXCEPTION HANDLING
  mpesa and card go through a real provider in production, and real
  providers decline or time out sometimes -- a parking system that
  assumes every payment attempt succeeds would leave a driver stuck at
  a barrier with no way forward. MOCK_FAILURE_RATE simulates that here
  so the retry path in the UI (see templates/exit.html / receipt.html)
  is genuinely exercised rather than a rare, effectively-dead branch.
  Cash never "fails" in this model -- the attendant has already
  physically received it by the time it's recorded.
"""

import random
import string
from datetime import datetime

from extensions import db
from models import Payment, AuditLog

# Simulated decline/timeout rate for mpesa & card, to exercise the retry
# path. Set to 0 to always succeed, e.g. for a quick demo.
MOCK_FAILURE_RATE = 0.15


def _mock_reference(method):
    prefix = {"mpesa": "MP", "card": "CD", "cash": "CS"}.get(method, "PM")
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"{prefix}{suffix}"


def initiate_payment(session, method, slot_manager):
    method = method.lower()
    if method not in ("mpesa", "card", "cash"):
        return {"ok": False, "error": "Unknown payment method."}

    if session.exit_time is not None:
        # exception: session was already closed (e.g. paid in another
        # tab, or force-released by an admin) -- don't double-process.
        return {"ok": False, "error": "This session has already been closed."}

    payment = Payment(session_id=session.id, method=method,
                       amount_kshs=session.amount_due,
                       reference=_mock_reference(method), status="pending")
    db.session.add(payment)
    db.session.commit()

    # Cash is confirmed by the attendant on the spot. mpesa/card stand in
    # for an async provider callback, which we simulate as sometimes
    # declining/timing out (see MOCK_FAILURE_RATE above).
    simulated_failure = method in ("mpesa", "card") and random.random() < MOCK_FAILURE_RATE

    if simulated_failure:
        payment.status = "failed"
        db.session.commit()
        db.session.add(AuditLog(event_type="payment_failed",
                                 plate_number=session.vehicle.plate_number,
                                 details=f"{method.upper()} ref {payment.reference} declined/timed out",
                                 amount_kshs=session.amount_due))
        db.session.commit()
        return {"ok": False, "retry": True, "session_id": session.id,
                "error": f"{method.upper()} payment was declined or timed out. "
                          "Please try again or choose a different method."}

    payment.status = "confirmed"
    payment.confirmed_at = datetime.utcnow()
    db.session.commit()

    db.session.add(AuditLog(event_type="payment", plate_number=session.vehicle.plate_number,
                             details=f"{method.upper()} ref {payment.reference}",
                             amount_kshs=session.amount_due))
    db.session.commit()

    next_plate = slot_manager.open_barrier_for(session)

    return {"ok": True, "reference": payment.reference, "method": method,
            "amount_kshs": float(session.amount_due), "barrier": "open",
            "next_queued_plate": next_plate}
