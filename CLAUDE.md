# The ePIC experiment Hardware Database

## Inspiration
This project is a Django-based implementation of the Component Database,
based on the ideas of the legacy application written in Java, as described
in the legacy user guide in the file assets/docs/The_Legacy_Component_Database_User_Guide.pdf

## seed_hdb

The seed_hdb command should contain the following entities:

* Groups:
  * BEMC
  * BTOF
  * PFRICH

* Users:
  * admin - superuser, staff
  * maxim - superuser, staff
  * gnigmat - user, belongs to groups: BTOF
  * crafts - user, belongs to groups: BEMC

* Technical systems:
  * BEMC-CRYSTAL, group set to "BEMC"
  * BEMC-PM, group set to "BEMC"
  * BTOF-Sensor, group set to "BTOF"
  * BTOF-Readout, group set to "BTOF"

* Locations:
  * CUA, Storage Room
  * UIC, Test Lab

* Components:
 * PbWO4 Crystal (to be used in the BEMC-CRYSTAL technical system)
 * Hamamatsu S14160-3010PS (to be used in the BEMC-PM)
 * AC-LGAD Sensor (to be used in BTOF-Sensor technical system)
 * FCFDv2 Readout (to be used in the BTOF-Readout technical system)

 Create between 2 and 5 component instances for each component.
 

## Deployment (epic-hwdb01, RHEL 9)

Production/target host: `epic-hwdb01`, RHEL 9, user `eicmax`. Code is delivered
via git only: changes are made and committed elsewhere, pushed to GitHub
(`BNLNPPS/epic-hdb`), then pulled on the host. Do not edit files directly in
the web root or the live tree. Never put secrets (passwords, SECRET_KEY) in
this file or in git.

Status legend: **[running]** already installed and working, **[TODO]** planned,
not done yet. Items marked "verify on host" have not been recorded here; inspect
the host and fill in the real values.

### PostgreSQL [running]
* Already installed and running as a system service. Verify on host: version,
  service name, port, data directory, and the `pg_hba.conf` auth method.
* The app should use a dedicated database and role. Verify on host: their names.
  Passwords come from the environment or a root-owned file, never from git.
* Note: the checked-in `hdb_project/settings.py` still uses SQLite
  (`db.sqlite3`). Switching `DATABASES` to PostgreSQL (`psycopg`) is [TODO].
  Data must be migrated or reseeded (`seed_hdb`), not copied as a file.

### Apache httpd [running]
* Already installed and running. Verify on host: version, config files under
  `/etc/httpd/conf.d/`, document root (`/var/www/html`), virtual host, TLS setup.
* Plan [TODO]: httpd serves `/static/` and `/media/` directly and proxies the
  application to a local gunicorn process, unless mod_wsgi is already in use
  (RHEL 9's packaged mod_wsgi targets Python 3.9, not the 3.12 this project
  requires, so gunicorn behind `ProxyPass` is the preferred route).
* SELinux: check `getenforce`. Proxying to gunicorn needs the
  `httpd_can_network_connect` boolean, and static/media directories need the
  `httpd_sys_content_t` (read) or `httpd_sys_rw_content_t` (media, writable)
  context. Do not disable SELinux.
* Media: `MEDIA_ROOT` in settings is `/var/data/hdb/media`; this directory must
  exist, be writable by the app user, and be readable by httpd.

### Python application [TODO, not yet set up]
* Requirements are pinned in `requirements.txt` (Python 3.12+, Django 6.0.8,
  `djangorestframework`, `qrcode[pil]`, `psycopg[binary]` for PostgreSQL, plus
  their transitive dependencies). RHEL 9's default `python3` is 3.9, which is
  too old; install `python3.12` from AppStream (`sudo dnf install python3.12`)
  or use `uv`.
* Note: `requirements.txt` was generated with `pip freeze` from the shared dev
  virtualenv, so it currently also includes packages that belong to the
  `hdb_client` CLI/MCP server (`mcp`, `uvicorn`, `starlette`, `httpx`, `typer`,
  etc.), not the Django web app. Fine for now, but worth splitting into a
  separate `client/requirements.txt` before relying on this file as the
  definition of "what the web app needs" — flag this to whoever sets up the
  production venv.
* Django REST framework is required in production: it's wrapped in a
  `try/except ImportError` in `settings.py`, so a missing install won't crash
  the app, it will just silently disable the whole `/api/` surface that
  `hdb_client` depends on. It's in `requirements.txt` now, so a normal
  `pip install -r requirements.txt` covers it — just don't skip that step.
* Plan:
  1. Clone the repo as `eicmax` outside the web root (location to be decided,
     for example `~/epic-hdb`), and create a virtualenv with Python 3.12.
  2. Install dependencies: `pip install -r requirements.txt`.
  3. Production settings: read `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`,
     `CSRF_TRUSTED_ORIGINS` and the database credentials from the environment
     (currently hardcoded, with `DEBUG = True` and `ALLOWED_HOSTS = ['*']`).
  4. Set `STATIC_ROOT` and run `python manage.py collectstatic`.
  5. `python manage.py migrate`, then create the admin user (do not use the
     dev `seed_hdb` users or passwords in production).
  6. Run gunicorn under a systemd unit (dedicated service user, restart on
     failure, environment file readable only by that user), and point httpd at it.
* After each `git pull` on the host: activate the venv, run
  `pip install -r requirements.txt` again (in case it changed), then `migrate`
  and `collectstatic`, then restart the gunicorn service. Back up the database
  (`pg_dump`) before any migration.

### Working on the host with Claude
* Prefer read-only diagnostics first (versions, `getenforce`, service status,
  config files). Ask before any change that needs `sudo`, touches the
  database, or restarts a service.
* Test changes in a clone, not in `/var/www/html`.
