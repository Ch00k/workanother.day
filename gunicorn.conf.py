"""gunicorn's access log, which run.sh points gunicorn at; the rest of its settings are there.

One line per request: the method, the path, the status, the bytes sent, the time taken and the
user agent. It is what tells apart whatever wakes the suspended machine - a calendar polling its
feed, the MCP connector, a crawler.
"""

accesslog = "-"
access_log_format = '%(m)s %(U)s %(s)s %(b)s %(M)sms "%(a)s"'
logger_class = "wad.access_log.AccessLogger"
