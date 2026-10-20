#!/bin/sh
# Weekly review-gate audit (Korean plain text). Delivered verbatim by Hermes cron (no_agent).
exec timeout 300 /usr/bin/python3 /home/justin/.hermes/scripts/review_audit_report.py
