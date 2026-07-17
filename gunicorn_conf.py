# gunicorn_conf.py
#
# The original app needed a 300s timeout because request handlers were
# calling Yahoo Finance live. Now that app.py only reads SQLite, requests
# should complete in milliseconds -- these numbers can be much smaller.
# Bump `workers` back up if your host's free tier allows more than 2.
timeout = 30
graceful_timeout = 15
workers = 2
threads = 4
