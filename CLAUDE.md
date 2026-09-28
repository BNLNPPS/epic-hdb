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

Every time the tool "pip" is mentioned, use "pip3" on the deployment RHEL machine to
sidestep ambiguity with the system version of pip, which is tied to an older
Python. References to "pip" in this document are already pointing to "pip3" explicitely.

Production/target host: `epic-hwdb01`, RHEL 9, user `eicmax`. Public URL:
`https://epic-hwdb.sdcc.bnl.gov` (via SDCC's reverse proxy; see the Apache
section below for the actual topology). Code is delivered via git only:
changes are made and committed elsewhere, pushed to GitHub
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
* [running] `hdb_project/settings.py` now switches `DATABASES` on the
  `DJANGO_DB_TYPE` environment variable (`sqlite` for local dev, `postgres`
  here). Postgres mode reads `DJANGO_DB_NAME`/`USER`/`PASSWORD`/`HOST`/`PORT`
  from the environment — set via the `/etc/hdb/env` file, never hardcoded
  or committed. Data must be migrated/reseeded (`seed_hdb`), not copied as
  a file.
* **Gotcha (bit us once):** `DJANGO_DB_TYPE` defaults silently to `sqlite`
  when unset — no error, no warning, just the wrong database. Any bare shell
  on the host that hasn't sourced `/etc/hdb/env` for *that* session will
  quietly create/use a throwaway `db.sqlite3` in the repo instead of talking
  to Postgres. This once caused a real, confusing migration-graph conflict
  (two different `0004` migrations — one generated against sqlite by
  mistake, the other the real one from git) that took real effort to
  untangle. `/etc/hdb/env` is intentionally root-only-readable (it holds the
  DB password), so a plain `eicmax` shell can't just `source` it — either
  wrap the command in `sudo bash -c '...'` (see the smoke-check example
  below), or set up a dedicated group for it. **[TODO]**: add
  `export DJANGO_DB_TYPE=postgres` to `~/.virtualenvs/hdb/bin/activate` so a
  bare shell fails loudly (`ImproperlyConfigured`, missing credentials)
  instead of silently succeeding against sqlite.

### Apache httpd [running]
* Config lives in `/etc/httpd/conf/httpd.conf` (the `<VirtualHost *:80>`
  block, `ServerName epic-hwdb.sdcc.bnl.gov`) and
  `/etc/httpd/conf.d/ssl.conf` (the `<VirtualHost _default_:443>` block).
  A versioned copy of the `:80` fragment is under `deploy/httpd/hdb.conf`
  in this repo — **note: that file's `ServerName` is stale**
  (still says `epic-hwdb01...` instead of the corrected
  `epic-hwdb.sdcc.bnl.gov`), and it doesn't reflect the `:443` block at all
  yet; needs reconciling.
* **Actual traffic path (cost real debugging time to find):** SDCC's
  institutional reverse proxy terminates TLS for
  `https://epic-hwdb.sdcc.bnl.gov` at its own edge (port 443, external) and
  forwards plain HTTP internally — but to *this* Apache's port **443**, not
  80. The `:80` vhost exists and works for direct/internal HTTP access, but
  is not what the public URL actually reaches. If the public site ever
  appears to serve stale/wrong content despite the `:80` vhost looking
  correctly configured (`httpd -S`, `apachectl configtest` all clean),
  check the `:443` vhost's directives first.
* The `:443` vhost reuses the stock self-signed cert
  (`/etc/pki/tls/certs/localhost.crt` / `/etc/pki/tls/private/localhost.key`)
  — no dedicated host cert was needed, since the reverse proxy completes the
  TLS handshake without validating the backend's cert chain/hostname.
* Both vhosts proxy to gunicorn the same way: `/static/` and `/media/` are
  served directly via `Alias` (excluded from the proxy with `ProxyPass ... !`),
  everything else goes to `http://127.0.0.1:8002/`.
* Institutional SSO (`mod_auth_openidc`, CILogon) sits in front of this too —
  an unauthenticated request gets redirected to `cilogon.org` before it ever
  reaches this app's content. Its config file hasn't been located yet (it's
  not caught by a `ServerName|VirtualHost` grep of `conf.d/` — look for
  `OIDCProviderMetadataURL`/`OIDCRedirectURI`/`<Location>` instead).
* SELinux: **[TODO, not yet confirmed applied]**. Check `getenforce`.
  Proxying to gunicorn needs the `httpd_can_network_connect` boolean, and
  static/media directories need the `httpd_sys_content_t` (read) or
  `httpd_sys_rw_content_t` (media, writable) context. Do not disable
  SELinux.
* Media: `MEDIA_ROOT` in settings is `/var/data/hdb/media`; this directory
  must exist, be writable by the app user, and be readable by httpd.
* `deploy/systemd/hdb-gunicorn.service` in this repo mirrors the installed
  `/etc/systemd/system/hdb-gunicorn.service` unit.

### Python application [running]
* Requirements are pinned in `requirements.txt` (Python 3.12+, Django 6.0.8,
  `djangorestframework`, `qrcode[pil]`, `psycopg[binary]` for PostgreSQL, plus
  their transitive dependencies). RHEL 9's default `python3` is 3.9, which is
  too old; install `python3.12` from AppStream (`sudo dnf install python3.12`)
  or use `uv`.
* Note: `requirements.txt` was generated with `pip3 freeze` from the shared dev
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
  `pip3 install -r requirements.txt` covers it — just don't skip that step.
* Plan:
  1. [done] Cloned as `eicmax` outside the web root, at `~/projects/epic-hdb`
     (real absolute path `/direct/eic+u/eicmax/projects/epic-hdb` — an NFS
     automount; use the absolute path in scripts/services, `~` is fine
     interactively), with a `virtualenvwrapper`-style venv at
     `~/.virtualenvs/hdb` (`/direct/eic+u/eicmax/.virtualenvs/hdb`),
     Python 3.12.
  2. [done] `pip3 install -r requirements.txt`.
  3. Production settings: `CSRF_TRUSTED_ORIGINS` now includes
     `https://epic-hwdb.sdcc.bnl.gov` **[done]**. `SECRET_KEY`, `DEBUG=False`,
     `ALLOWED_HOSTS` still **[TODO]** — currently hardcoded, with
     `DEBUG = True` and `ALLOWED_HOSTS = ['*']`.
  4. [done] `STATIC_ROOT` set (`/var/data/hdb/static`), `collectstatic` run.
  5. [done] `python manage.py migrate`, admin user created (not the dev
     `seed_hdb` users/passwords). See the `DJANGO_DB_TYPE` gotcha above —
     always confirm `showmigrations hdb` shows a single clean chain (no
     unexpected extra leaf) before trusting a `migrate` run on this host.
  6. [done] gunicorn runs under `hdb-gunicorn.service` (systemd), bound to
     `127.0.0.1:8002`, `enable`d so it survives a reboot.
  7. Smoke-check the app and database directly (no server, no HTTP, no auth)
     using the local CLI client already in the repo. `/etc/hdb/env` is
     root-only-readable, so wrap this in `sudo`:
     ```
     sudo bash -c '
       set -a; source /etc/hdb/env; set +a
       cd /direct/eic+u/eicmax/projects/epic-hdb
       source /direct/eic+u/eicmax/.virtualenvs/hdb/bin/activate

       python client/hdb.py institutions
       python client/hdb.py systems
       python client/hdb.py search Crystal
       python client/hdb.py component "PbWO4 Crystal"

       # exercises DesignTemplate.source_path/source_sha256/source_git_commit
       # and the on_delete=PROTECT location relationships (migrations 0004-0007):
       python client/hdb.py verify-template data/btof_split/btof_stave.yaml
       python client/hdb.py verify-template data/btof_split/btof_half_stave.yaml
       python client/hdb.py verify-template data/btof_split/btof_stavelet.yaml
       python client/hdb.py verify-template data/bemc_tower_template.yaml
       python client/hdb.py bom-template "BTOF Stave"
       python client/hdb.py bom-template "BEMC tower"
     '
     ```
     This queries the same Postgres DB the running app uses, in-process —
     confirms `migrate`/`seed_hdb` actually landed real data. Don't confuse
     this with `client/mcp_server.py` + `client/smoke_test.py`: that pair
     stands up its own HTTP+MCP server process to test the network-facing
     MCP endpoint (for an agent/connector talking to HDB remotely) — a
     separate concern, not part of the running deployment unless/until
     that's wired up on its own.
     Note: `data/btof_stave_templates.yaml` is deprecated (superseded by
     `data/btof_split/*.yaml`) — don't load/verify against it.
* After each `git pull` on the host: activate the venv, run
  `pip3 install -r requirements.txt` again (in case it changed), then `migrate`
  and `collectstatic`, then restart the gunicorn service. Back up the database
  (`pg_dump`) before any migration. **Confirm `DJANGO_DB_TYPE=postgres` and
  the rest of `/etc/hdb/env` are actually sourced in that shell first** — see
  the gotcha above; this is the single most likely way to silently do the
  wrong thing on this host.

### Working on the host with Claude
* Prefer read-only diagnostics first (versions, `getenforce`, service status,
  config files). Ask before any change that needs `sudo`, touches the
  database, or restarts a service.
* Test changes in a clone, not in `/var/www/html`.
* Dev-side filesystem quirk: this repo is accessed both natively on Windows
  and through a Linux mount of the same NTFS volume, which can spuriously
  flip a file's executable bit with no content change. `core.fileMode false`
  is set locally to stop git from treating that as a diff — leave it set.
