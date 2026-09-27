"""
services/slot_manager.py
=========================
Module 1 (Slot Monitoring & Display) and Module 2 (Entry Lane Control),
per the Terms of Reference.

Holds the in-memory data structures that mirror the database so that the
live board and the barrier logic never have to scan the whole session
table on every request. On process start, rebuild_from_db() replays the
database to repopulate them (so a restart never loses state).

ALGORITHM: rebuild_from_db()
  1. Read every Bay row.
  2. For each bay whose status == 'occupied', mark it NOT free.
  3. Push every remaining bay number onto the free-bay min-heap.
  4. For every open ParkingSession (exit_time IS NULL), insert
     {plate_number: session_id} into the active-session hash map.

ALGORITHM: handle_arrival(plate_number)
  1. If plate_number already has an open session -> reject (duplicate
     entry / already inside).
  2. bay = free_bay_heap.allocate()
        - if None: lot is full -> enqueue plate in OverflowQueue, return
          "queued".
  3. Look up or create the Vehicle row for plate_number.
  4. Create a ParkingSession row: entry_time = now, bay = bay, status =
     'active'.
  5. Mark the Bay row 'occupied'.
  6. Insert {plate_number: session.id} into the active-session hash map.
  7. Append an 'entry' event to the audit chain / AuditLog table.
  8. Return the bay number and entry time (ticket data).

ALGORITHM: handle_exit_lookup(plate_number)
  1. session_id = active_sessions.get(plate_number)   # O(1)
  2. If not found -> reject ("no open session for this plate").
  3. duration = now - session.entry_time
  4. amount = RateTable.fee_for(duration_in_minutes)   # O(log tiers)
  5. Store duration & amount on the session, status = 'awaiting_payment'.
  6. Return duration & amount to the exit screen for the driver to pay.

ALGORITHM: handle_barrier_open(session_id)
  (called only after a payment is confirmed, see services/payment.py)
  1. session.status = 'closed'; session.exit_time = now.
  2. bay.status = 'free'; free_bay_heap.release(bay.bay_number).
  3. Remove plate_number from active-session hash map.
  4. If OverflowQueue is non-empty -> dequeue the next waiting plate and
     let the attendant know a bay just freed up.
  5. Append a 'barrier_open' audit event.

EXCEPTION HANDLING (Module 7, per the ToR scope list)
  Real deployments hit cases outside the "happy path" above. This module
  handles them explicitly rather than letting them crash the request or
  silently corrupt state:

  - Invalid plate input (empty / too short) -> rejected before any DB
    write, with a clear message, at both handle_arrival() and
    handle_exit_lookup().
  - Exit requested for a plate that is only in the waiting queue (never
    actually allocated a bay) -> handle_exit_lookup() detects this and
    returns a distinct message instead of the generic "not found" one,
    so the attendant isn't confused about why there's no fee to collect.
  - Overstays: a session left open far longer than any realistic parking
    duration (default threshold 24h) -- usually a sensor fault, an
    attendant forgetting to process an exit, or a vehicle abandoned in
    the lot. list_overstays() surfaces these for manual review rather
    than letting them silently occupy a bay forever.
  - Manual override: force_release() lets an admin close a stuck session
    and free its bay without going through the normal payment flow, for
    cases like a broken barrier or a written-off, unrecoverable session.
    Every override is written to AuditLog with the reason given, so the
    audit trail still explains why the money for that session, if any,
    doesn't reconcile through the normal payment record.
"""

from datetime import datetime, timedelta

from extensions import db
from models import Bay, Vehicle, ParkingSession, AuditLog
from data_structures import FreeBayHeap, OverflowQueue

MIN_PLATE_LENGTH = 4          # shortest plausible Kenyan plate, e.g. "KAA1"
OVERSTAY_HOURS = 24            # sessions open longer than this are flagged


class SlotManager:
    def __init__(self):
        self.free_bays = FreeBayHeap([])      # rebuilt in rebuild_from_db()
        self.overflow = OverflowQueue()
        self.active_sessions = {}             # hash map: plate -> session_id

    # -- bootstrapping ----------------------------------------------------
    def rebuild_from_db(self):
        free_numbers = []
        for bay in Bay.query.filter_by(is_active=True).all():
            if bay.status == "free":
                free_numbers.append(bay.bay_number)
        self.free_bays = FreeBayHeap(free_numbers)

        self.active_sessions.clear()
        for s in ParkingSession.query.filter(ParkingSession.exit_time.is_(None)).all():
            self.active_sessions[s.vehicle.plate_number] = s.id

    # -- board --------------------------------------------------------------
    def board_snapshot(self):
        bays = Bay.query.filter_by(is_active=True).order_by(Bay.bay_number).all()
        total = len(bays)
        free = self.free_bays.peek_count()
        return {
            "total_bays": total,
            "free_bays": free,
            "occupied_bays": total - free,
            "bays": [{"number": b.bay_number, "status": b.status} for b in bays],
            "waiting_queue": self.overflow.as_list(),
        }

    # -- entry ----------------------------------------------------------
    def handle_arrival(self, plate_number):
        plate_number = (plate_number or "").strip().upper()

        # -- exception: invalid input -----------------------------------
        if len(plate_number) < MIN_PLATE_LENGTH:
            return {"ok": False, "error": "Please enter a valid number plate."}

        if plate_number in self.active_sessions:
            return {"ok": False, "error": f"{plate_number} already has an open session."}

        if plate_number in self.overflow.as_list():
            return {"ok": False, "error": f"{plate_number} is already waiting in the queue."}

        bay_number = self.free_bays.allocate()
        if bay_number is None:
            self.overflow.enqueue(plate_number)
            self._log("entry_queued", plate_number, f"Lot full; {plate_number} queued.")
            return {"ok": False, "queued": True,
                     "error": "Parking is full. Vehicle added to the waiting queue."}

        vehicle = Vehicle.query.filter_by(plate_number=plate_number).first()
        if vehicle is None:
            vehicle = Vehicle(plate_number=plate_number)
            db.session.add(vehicle)
            db.session.flush()

        bay = Bay.query.filter_by(bay_number=bay_number).first()
        bay.status = "occupied"

        session = ParkingSession(vehicle_id=vehicle.id, bay_id=bay.id,
                                  entry_time=datetime.utcnow(), status="active")
        db.session.add(session)
        db.session.commit()

        self.active_sessions[plate_number] = session.id
        self._log("entry", plate_number, f"Allocated bay {bay_number}.")

        return {"ok": True, "bay_number": bay_number, "entry_time": session.entry_time,
                "session_id": session.id}

    # -- exit lookup / fee calc ------------------------------------------
    def handle_exit_lookup(self, plate_number, rate_table):
        plate_number = (plate_number or "").strip().upper()

        # -- exception: invalid input -----------------------------------
        if len(plate_number) < MIN_PLATE_LENGTH:
            return {"ok": False, "error": "Please enter a valid number plate."}

        session_id = self.active_sessions.get(plate_number)

        if session_id is None:
            # -- exception: plate is waiting at the gate, never allocated
            #    a bay, so there is nothing to charge yet.
            if plate_number in self.overflow.as_list():
                return {"ok": False, "error": f"{plate_number} is still waiting for a bay "
                                                "and has not been charged yet."}
            return {"ok": False, "error": f"No open session found for {plate_number}."}

        session = ParkingSession.query.get(session_id)
        now = datetime.utcnow()
        duration_minutes = max(0, int((now - session.entry_time).total_seconds() // 60))
        amount, label = rate_table.fee_for(duration_minutes)

        session.duration_minutes = duration_minutes
        session.amount_due = amount
        session.rate_label = label
        session.status = "awaiting_payment"
        db.session.commit()

        return {"ok": True, "session_id": session.id, "plate_number": plate_number,
                "duration_minutes": duration_minutes, "amount_due": float(amount),
                "rate_label": label, "bay_number": session.bay.bay_number}

    # -- barrier open (post-payment) --------------------------------------
    def open_barrier_for(self, session):
        bay = session.bay
        bay.status = "free"
        session.status = "closed"
        session.exit_time = datetime.utcnow()
        db.session.commit()

        self.free_bays.release(bay.bay_number)
        self.active_sessions.pop(session.vehicle.plate_number, None)
        self._log("barrier_open", session.vehicle.plate_number,
                   f"Bay {bay.bay_number} released.")

        next_plate = self.overflow.dequeue()
        return next_plate  # None, or the next plate that can now be let in

    # -- exception handling: overstays & manual override --------------------
    def list_overstays(self, hours=OVERSTAY_HOURS):
        """Open sessions that have been sitting far longer than any
        realistic parking duration -- flagged for manual attendant
        review rather than left to occupy a bay indefinitely."""
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        return (ParkingSession.query
                .filter(ParkingSession.exit_time.is_(None))
                .filter(ParkingSession.entry_time < cutoff)
                .all())

    def force_release(self, session_id, reason):
        """Manually close a stuck session and free its bay outside the
        normal payment flow (e.g. a broken barrier, an abandoned
        vehicle, a sensor fault). Always logged with the reason given,
        so the audit trail explains the gap in the payment record."""
        session = ParkingSession.query.get(session_id)
        if session is None or session.exit_time is not None:
            return {"ok": False, "error": "Session not found or already closed."}

        bay = session.bay
        plate = session.vehicle.plate_number
        bay.status = "free"
        session.status = "closed"
        session.exit_time = datetime.utcnow()
        db.session.commit()

        self.free_bays.release(bay.bay_number)
        self.active_sessions.pop(plate, None)
        self._log("exception_override", plate,
                   f"Force-released bay {bay.bay_number}. Reason: {reason}")

        next_plate = self.overflow.dequeue()
        return {"ok": True, "next_queued_plate": next_plate}

    # -- audit --------------------------------------------------------------
    def _log(self, event_type, plate_number, details, amount=None):
        db.session.add(AuditLog(event_type=event_type, plate_number=plate_number,
                                 details=details, amount_kshs=amount))
        db.session.commit()


# single shared instance used across the app (mirrors the one physical car park)
slot_manager = SlotManager()
