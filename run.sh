#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

# Above the longest a request can legitimately take, so a downstream that stops answering is
# reported by the view that called it. A worker killed by the arbiter instead is killed
# between the network call and the row that records what became of it, which is the evidence
# an interrupted filing or delivery is resolved from.
#
# The bound is a JPK_EWP submission, at three gateway calls of 60 seconds each around the
# packaging and the writes between them. Sending an invoice by mail is the next longest: a
# render of well under a second, followed by an SMTP conversation whose every step is given 30.
#
# The same bound is how long a deploy's SIGTERM waits for the requests in progress to finish,
# which is fly.toml's kill_timeout.
exec gunicorn config.wsgi:application --bind [::]:8080 --timeout 300 --graceful-timeout 300
