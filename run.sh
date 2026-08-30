#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

# Above the longest a request can legitimately take, so a downstream that stops answering is
# reported by the view that called it. A worker killed by the arbiter instead is killed
# between the network call and the row that records what became of it, which is the evidence
# an interrupted filing or delivery is resolved from.
#
# The bound is a JPK_EWP submission: three gateway calls at 60 seconds each, around the
# packaging and the writes between them. Sending an invoice by mail is the next longest, at a
# 30 second render followed by an SMTP conversation whose every step is given 30. A PDF on its
# own sits well inside both.
exec gunicorn config.wsgi:application --bind [::]:8080 --timeout 240
