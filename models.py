"""
models.py
=========
SmartPark KE — dynamic database schema (Task 1, part c).

"Dynamic" here means: nothing that management needs to change at runtime
(bay count, rate tiers, payment methods) is hard-coded. It lives in tables
that the admin screens read and write, so growing the car park or changing
prices never requires touching source code.

Tables
------
Bay          one row per physical bay; can be added/retired at runtime.
Vehicle      one row per number plate ever seen (keeps repeat-visit history).
ParkingSession  one row per visit (entry -> exit). The live "core" record.
RateTier     the fee table itself, editable by management.
Payment      one row per payment attempt (supports retries/failed attempts).
AuditLog     append-only ledger of every state-changing event, for
             reconciliation and VAT.
"""

from datetime import datetime
from extensions import db


class Bay(db.Model):
    __tablename__ = "bays"

    id = db.Column(db.Integer, primary_key=True)
    bay_number = db.Column(db.Integer, unique=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True)  # allows retiring a bay
    # 'free' | 'occupied' -- denormalised for a fast board read; the source
    # of truth for "who" is the open ParkingSession row.
    status = db.Column(db.String(10), default="free", nullable=False)

    sessions = db.relationship("ParkingSession", backref="bay", lazy=True)

    def __repr__(self):
        return f"<Bay {self.bay_number} {self.status}>"


class Vehicle(db.Model):
    __tablename__ = "vehicles"

    id = db.Column(db.Integer, primary_key=True)
    plate_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    first_seen = db.Column(db.DateTime, default=datetime.utcnow)

    sessions = db.relationship("ParkingSession", backref="vehicle", lazy=True)


class ParkingSession(db.Model):
    """One row per vehicle visit: entry to exit."""
    __tablename__ = "parking_sessions"

    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False)
    bay_id = db.Column(db.Integer, db.ForeignKey("bays.id"), nullable=False)

    entry_time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    exit_time = db.Column(db.DateTime, nullable=True)

    duration_minutes = db.Column(db.Integer, nullable=True)
    amount_due = db.Column(db.Numeric(10, 2), nullable=True)
    rate_label = db.Column(db.String(50), nullable=True)

    # 'active' | 'awaiting_payment' | 'paid' | 'closed'
    status = db.Column(db.String(20), default="active", nullable=False)

    payments = db.relationship("Payment", backref="session", lazy=True)

    def is_open(self):
        return self.exit_time is None


class RateTier(db.Model):
    """A single fee band, e.g. 'up to 120 minutes -> Kshs 50'.

    upper_bound_minutes = NULL means "and above" (the open-ended top tier).
    Editable from the admin screen at any time -- this is what lets
    management change prices without a software change.
    """
    __tablename__ = "rate_tiers"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(50), nullable=False)
    upper_bound_minutes = db.Column(db.Integer, nullable=True)  # NULL = open-ended
    amount_kshs = db.Column(db.Numeric(10, 2), nullable=False)
    is_active = db.Column(db.Boolean, default=True)


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("parking_sessions.id"), nullable=False)
    method = db.Column(db.String(10), nullable=False)   # mpesa | card | cash
    amount_kshs = db.Column(db.Numeric(10, 2), nullable=False)
    reference = db.Column(db.String(40), nullable=False)  # mock M-Pesa code / card auth code
    status = db.Column(db.String(15), default="pending")  # pending|confirmed|failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    confirmed_at = db.Column(db.DateTime, nullable=True)


class AuditLog(db.Model):
    """Append-only ledger. Never updated or deleted -- only ever inserted
    into -- so it can be handed to an auditor or KRA (VAT) as a trustworthy
    record of every shilling collected and every barrier action taken."""
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    event_type = db.Column(db.String(30), nullable=False)  # entry|exit|payment|barrier|rate_change
    plate_number = db.Column(db.String(20), nullable=True)
    details = db.Column(db.Text, nullable=True)
    amount_kshs = db.Column(db.Numeric(10, 2), nullable=True)
