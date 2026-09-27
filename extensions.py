"""extensions.py -- shared Flask extension instances, kept separate from
app.py to avoid circular imports between app.py and models.py."""

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
