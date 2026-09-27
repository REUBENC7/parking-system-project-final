"""
app.py
======
SmartPark KE -- a modern, web-based parking management system.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000/

Screens:
  /            live slot-availability board (the "display board at the
               entrance" from the ToR)
  /entry       record a vehicle arrival
  /exit        look up a plate, see duration & fee, take payment, open
               barrier
  /admin/rates view & edit the parking rate tiers (no code change needed)
  /admin/reports  audit trail / revenue report for reconciliation & VAT
"""

from datetime import datetime, date

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash

from extensions import db
from models import Bay, RateTier, ParkingSession, AuditLog, Payment
from data_structures import RateTable
from services.slot_manager import slot_manager
from services.payment import initiate_payment

DEFAULT_BAY_COUNT = 20

# Default rate tiers, taken directly from the assignment brief:
#   up to 30 minutes free; up to 2h Kshs 50; up to 4h Kshs 100;
#   up to 6h Kshs 300; over 6h Kshs 500.
DEFAULT_TIERS = [
    (30, 0, "Up to 30 minutes (free)"),
    (120, 50, "Up to 2 hours"),
    (240, 100, "Up to 4 hours"),
    (360, 300, "Up to 6 hours"),
    (None, 500, "Over 6 hours"),
]

rate_table = RateTable([])  # populated in bootstrap()


def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///smartpark.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = "dev-secret-change-in-production"

    db.init_app(app)

    with app.app_context():
        db.create_all()
        bootstrap(app)

    register_routes(app)
    return app


def bootstrap(app):
    """Seed bays and default rate tiers the first time the app runs, then
    load the rate table and rebuild in-memory structures from the DB."""
    if Bay.query.count() == 0:
        for n in range(1, DEFAULT_BAY_COUNT + 1):
            db.session.add(Bay(bay_number=n, status="free"))
        db.session.commit()

    if RateTier.query.count() == 0:
        for upper, amount, label in DEFAULT_TIERS:
            db.session.add(RateTier(label=label, upper_bound_minutes=upper, amount_kshs=amount))
        db.session.commit()

    reload_rate_table()
    slot_manager.rebuild_from_db()


def reload_rate_table():
    tiers = RateTier.query.filter_by(is_active=True).all()
    rate_table.reload([(t.upper_bound_minutes, float(t.amount_kshs), t.label) for t in tiers])


def register_routes(app):

    # ---- live board -----------------------------------------------------
    @app.route("/")
    def board():
        return render_template("board.html", snapshot=slot_manager.board_snapshot())

    @app.route("/api/slots")
    def api_slots():
        return jsonify(slot_manager.board_snapshot())

    # ---- entry ------------------------------------------------------------
    @app.route("/entry", methods=["GET", "POST"])
    def entry():
        result = None
        if request.method == "POST":
            plate = request.form.get("plate_number", "")
            result = slot_manager.handle_arrival(plate)
        return render_template("entry.html", result=result,
                                snapshot=slot_manager.board_snapshot())

    # ---- exit: lookup, then pay --------------------------------------------
    @app.route("/exit", methods=["GET", "POST"])
    def exit_lookup():
        lookup = None
        if request.method == "POST":
            plate = request.form.get("plate_number", "")
            lookup = slot_manager.handle_exit_lookup(plate, rate_table)
        return render_template("exit.html", lookup=lookup)

    @app.route("/exit/pay", methods=["POST"])
    def exit_pay():
        session_id = request.form.get("session_id")
        method = request.form.get("method")
        session = ParkingSession.query.get_or_404(session_id)
        outcome = initiate_payment(session, method, slot_manager)
        return render_template("receipt.html", outcome=outcome, session=session)

    # ---- admin: exceptions & manual overrides ------------------------
    @app.route("/admin/exceptions", methods=["GET", "POST"])
    def admin_exceptions():
        if request.method == "POST":
            session_id = request.form.get("session_id")
            reason = request.form.get("reason", "No reason given")
            slot_manager.force_release(session_id, reason)
            return redirect(url_for("admin_exceptions"))

        overstays = slot_manager.list_overstays()
        failed_payments = (Payment.query.filter_by(status="failed")
                            .order_by(Payment.created_at.desc()).limit(20).all())
        return render_template("admin_exceptions.html", overstays=overstays,
                                failed_payments=failed_payments)

    # ---- admin: rates (dynamic pricing, no redeploy needed) --------------
    @app.route("/admin/rates", methods=["GET", "POST"])
    def admin_rates():
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                upper = request.form.get("upper_bound_minutes") or None
                db.session.add(RateTier(
                    label=request.form.get("label", "New tier"),
                    upper_bound_minutes=int(upper) if upper else None,
                    amount_kshs=float(request.form.get("amount_kshs", 0)),
                ))
                db.session.add(AuditLog(event_type="rate_change",
                                         details=f"Added tier {request.form.get('label')}"))
            elif action == "delete":
                tier = RateTier.query.get(request.form.get("tier_id"))
                if tier:
                    db.session.add(AuditLog(event_type="rate_change",
                                             details=f"Removed tier {tier.label}"))
                    db.session.delete(tier)
            elif action == "toggle":
                tier = RateTier.query.get(request.form.get("tier_id"))
                if tier:
                    tier.is_active = not tier.is_active
                    db.session.add(AuditLog(event_type="rate_change",
                                             details=f"Toggled tier {tier.label} -> {tier.is_active}"))
            db.session.commit()
            reload_rate_table()
            return redirect(url_for("admin_rates"))

        tiers = RateTier.query.order_by(RateTier.upper_bound_minutes.is_(None),
                                         RateTier.upper_bound_minutes).all()
        return render_template("admin_rates.html", tiers=tiers)

    # ---- admin: reports / audit trail --------------------------------------
    @app.route("/admin/reports")
    def admin_reports():
        day_str = request.args.get("date", date.today().isoformat())
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            day = date.today()

        sessions_today = ParkingSession.query.filter(
            db.func.date(ParkingSession.entry_time) == day.isoformat()
        ).all()
        revenue_today = sum(float(p.amount_kshs) for s in sessions_today
                             for p in s.payments if p.status == "confirmed")

        audit_entries = (AuditLog.query
                          .filter(db.func.date(AuditLog.timestamp) == day.isoformat())
                          .order_by(AuditLog.timestamp.desc()).all())

        return render_template("admin_reports.html", day=day, sessions=sessions_today,
                                revenue_today=revenue_today, audit_entries=audit_entries)


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
