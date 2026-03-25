"""
Application entry point.
Uses the app factory from config.py to create and run the Flask application.
"""

import os
from config import create_app

app = create_app()

if __name__ == '__main__':
    with app.app_context():
        from models import db
        db.create_all()
    debug = os.getenv('FLASK_ENV') == 'development'
    app.run(debug=debug, port=5001)
