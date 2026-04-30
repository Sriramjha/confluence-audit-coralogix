Fresh copy from GitHub for integration testing.

Contents:
  confluence-audit-coralogix/  — clone of https://github.com/Sriramjha/confluence-audit-coralogix

Update to latest:
  cd confluence-audit-coralogix && git pull

Setup inside the clone:
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  cp env.example env.sh
  # edit env.sh with real secrets, chmod 600 env.sh
  source env.sh
  .venv/bin/python main.py --diagnose   # optional
  .venv/bin/python main.py --dry-run

This folder is listed in the parent repo’s .gitignore so the clone is not committed.
