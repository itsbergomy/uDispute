"""Dev server launcher."""
import os, sys
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(APP_DIR, '.env'))
from config import create_app
app = create_app()
app.run(host='0.0.0.0', port=5001, debug=True)
