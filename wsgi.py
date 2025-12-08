# wsgi.py
import logging
from main import app, start_background_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.info("wsgi: starting bot thread...")
# Start the bot in background when gunicorn imports this module
start_background_bot()

# Expose Flask WSGI application callable as 'app' (gunicorn expects 'app' or 'application')
application = app
app = application
