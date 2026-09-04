# Backing up the production database

## What has to be backed up

One file: `/app/data/db.sqlite3`, on Fly volume `wad_data` (`vol_r1jznjp0zpyd5jwr`, region `fra`).

There is nothing else. No `FileField`, no `MEDIA_ROOT`, no object storage; `STORAGES["default"]` is
the filesystem backend but nothing writes through it. Everything else on the volume is
reconstructible - `collectstatic` reruns at every start.

The database is not just application state, it is the archive of record. `Filing.xml`,
`Filing.upo`, `TaxReturn.upo`, `Invoice.xml` and `Invoice.upo` are columns, so the KSeF
confirmations and the submitted JPK_EWP bodies exist nowhere else. Polish retention for these is
five years.

## What exists today

Two things, in this order:

1. **A daily copy at two providers**, taken by `.github/workflows/backup.yml` at 02:37 UTC and
   kept forever: Tigris, and a Hetzner Storage Box. This is what to restore from when the
   question is "what did this row say last month", and what outlives the machine.
2. **Fly scheduled volume snapshots**, retention 5 days:

   ```
   $ fly volumes show vol_r1jznjp0zpyd5jwr
    Snapshot retention: 5
    Scheduled snapshots: true
   ```

   Worth keeping as the first line, because restoring one is faster than anything else here: it
   needs no tooling that has to have been set up correctly in advance. On its own it would be
   thin, though. Five days means an error noticed after a week has no clean copy to go back to,
   and one provider, one account and one region means it does not survive `fly apps destroy`, a
   billing lapse, or losing access to the account.

## The daily copy

Three steps, run from a GitHub Actions schedule because a Fly volume attaches to exactly one
machine, so nothing scheduled alongside the app can read the database file:

```bash
flyctl ssh console -a workanotherday -C "/app/.venv/bin/python /app/manage.py backup_database /app/data/backup.sqlite3"
flyctl sftp get /app/data/backup.sqlite3 backup.sqlite3 -a workanotherday
flyctl ssh console -a workanotherday -C "rm -f /app/data/backup.sqlite3"
```

`backup_database` is `VACUUM INTO`, which is safe against the live database and produces one
self-contained file. The copy is removed from the volume whether or not the fetch worked, and
the command removes a leftover before it writes, because `VACUUM INTO` refuses to write over a
file that exists.

The runner then opens the file it fetched, checks `pragma integrity_check` and that
`django_migrations` is populated, gzips it, and names it `db-<UTC timestamp>.sqlite3.gz`. One
file per run, named for the moment it was taken, so nothing is ever written over and a listing
sorts oldest first. The same name goes to both destinations, so the two copies of a run are
visibly the same copy. At a database in the low tens of MB, a year of them is a couple of GB, and
Polish retention for these records is five years, so nothing expires them.

### Tigris

```
s3://app-backups/workanotherday/database/db-<UTC timestamp>.sqlite3.gz
```

at `https://fly.storage.tigris.dev`, written with an access key scoped to a single action on a
single prefix:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::app-backups/workanotherday/database/*"
    }
  ]
}
```

So a leaked runner cannot read the invoices back out of the bucket and cannot delete anything.
What it could still do is write over an object whose name it gives exactly, which is what the
timestamp in the name is for: nothing a run writes lands on a name already taken. Restoring is
done with a different key, or from the Tigris dashboard.

### The Storage Box

Tigris is provisioned and billed through Fly, so it is a second bucket rather than a second
relationship: `fly apps destroy` is covered, an account lost is not. The Storage Box is the
destination that has nothing to do with any of the rest of this, and it is the reason the daily
copy can be called off-site without qualification.

Written by `rsync` over port 23, which is the Storage Box's SSH service - port 22 speaks only
SCP and SFTP. Both the sub-account it connects as and the account's own settings need **SSH
support** and **external reachability** switched on. rsync uploads to a temporary name and
renames when the transfer completes, so a run cut off halfway leaves nothing that looks like a
backup.

The sub-account exists so the credential in GitHub reaches one directory and nothing else on the
box, and its home directory is where the copies land, so there is no path to get wrong. It
authenticates with an SSH key, not the account password:

```bash
ssh-keygen -t ed25519 -f storage_box -C "workanotherday backup" -N ""
cat storage_box.pub | ssh -p23 uXXXXXX-subN@uXXXXXX.your-storagebox.de install-ssh-key
ssh-keyscan -p 23 uXXXXXX.your-storagebox.de
```

Unlike the Tigris key, a sub-account can read back and delete what is in its directory - the
protocol has no way to say otherwise. **Automatic snapshots on the Storage Box are what answers
that**, because snapshots belong to the main account and a sub-account cannot see or touch them.
Without them, anything that reaches the runner's key reaches the whole history there.

### Secrets

| Secret | What it is |
| --- | --- |
| `FLY_SSH_TOKEN` | `fly tokens create ssh -a workanotherday`. A different token from the one CI deploys with: reading the database means an SSH session, and issuing a certificate for one needs a permission a deploy token does not carry. |
| `TIGRIS_ACCESS_KEY_ID`, `TIGRIS_SECRET_ACCESS_KEY` | The scoped bucket key above. |
| `STORAGE_BOX_SSH_KEY` | The private half of the key installed on the sub-account. |
| `STORAGE_BOX_HOST` | `uXXXXXX-subN@uXXXXXX.your-storagebox.de`. Secret only because this repository is public and the hostname carries the account number. |
| `STORAGE_BOX_KNOWN_HOSTS` | What `ssh-keyscan -p 23` printed. Pinned rather than accepted on sight, because a host key accepted blindly is a database handed to whoever answered. |

Things to know about it:

- **The schedule is best-effort.** Actions cron can be delayed by an hour or more under load.
- **A public repository with no commits for 60 days has its scheduled workflows disabled**, with
  an email first. A quiet quarter would otherwise stop the backups silently.
- **A failed run emails the repository owner**, which is the only alarm there is. A run that
  produced nothing is a run that failed, because every step aborts the job rather than continuing
  quietly.
- **Both copies are written by one job**, which is the single thing the two providers share: a
  Fly token and both sets of storage credentials sit in the same repository. That is what the
  scoped Tigris key and the Storage Box's snapshots are for.
- **The Storage Box is finite** where the bucket is not. Nothing prunes it, and at this size
  nothing needs to for years, but it is a disk with an edge and the bucket is a bill.

## Setting it up

Once, in this order. Nothing is backing anything up until the whole list is done, and step 8 is
what says so.

1. **Mint the Tigris access key.** `fly storage dashboard` opens the Tigris console signed in as
   the Fly account that owns `app-backups`. Create a key there and attach the policy above by
   hand: the ReadOnly/Editor/Admin presets are whole-bucket roles, and this wants one action on
   one prefix. The secret half is shown once.

2. **Mint the Fly SSH token.**

   ```bash
   fly tokens create ssh -a workanotherday --name backup-workflow
   ```

   It defaults to 20 years; `-x 8760h` makes it a year if it should be rotated instead. Also
   shown once.

3. **Make the Storage Box sub-account**, in the Hetzner console, with a directory of its own.
   Switch on **SSH support** and **external reachability**, on the sub-account and on the box
   itself - a sub-account cannot be reachable through a box that is not.

4. **Put a key on the sub-account.** Its own key, used by nothing else, so that revoking it costs
   nothing but a backup run:

   ```bash
   ssh-keygen -t ed25519 -f storage_box -C "workanotherday backup" -N ""
   cat storage_box.pub | ssh -p23 uXXXXXX-subN@uXXXXXX.your-storagebox.de install-ssh-key
   ssh-keyscan -p 23 uXXXXXX.your-storagebox.de > storage_box_known_hosts
   ```

   Check it before going further, because a failure here is unambiguous now and cryptic from
   inside a workflow:

   ```bash
   ssh -p23 -i storage_box uXXXXXX-subN@uXXXXXX.your-storagebox.de ls
   ```

5. **Turn on automatic snapshots** for the box. This is not decoration: the sub-account can
   delete everything in its own directory, and snapshots, which belong to the main account, are
   the only thing that survives a key that ends up somewhere it should not.

6. **Set the five secrets** on the repository:

   ```bash
   gh secret set FLY_SSH_TOKEN
   gh secret set TIGRIS_ACCESS_KEY_ID
   gh secret set TIGRIS_SECRET_ACCESS_KEY
   gh secret set STORAGE_BOX_HOST
   gh secret set STORAGE_BOX_SSH_KEY < storage_box
   gh secret set STORAGE_BOX_KNOWN_HOSTS < storage_box_known_hosts
   ```

   Then the private key belongs in the password manager or nowhere - it can be replaced by
   redoing step 4, so keeping a stray copy on a laptop buys nothing.

7. **Deploy**, because the workflow calls `manage.py backup_database` inside the running
   container and the image has to have it. That is a release tag, as in Deployment in
   `README.md`.

8. **Run it by hand and watch it**:

   ```bash
   gh workflow run backup.yml
   gh run watch
   aws s3 ls s3://app-backups/workanotherday/database/ --endpoint-url https://fly.storage.tigris.dev
   ssh -p23 -i storage_box uXXXXXX-subN@uXXXXXX.your-storagebox.de ls
   ```

   The same filename in both listings is the whole setup confirmed.

9. **Restore one, on purpose**, following the section below, before the day it matters.

Two things that are not this workflow but belong to the same job:

- **`DJANGO_KSEF_TOKEN_KEY` into the password manager**, if it is not there already. Fly will not
  print it back, and without it a restored database has an unreadable KSeF token. See below.
- **`fly volumes update vol_r1jznjp0zpyd5jwr --snapshot-retention 60 -a workanotherday`**, which
  makes the fastest restore path useful for more than a work week.

### If the first run fails

- **`ImproperlyConfigured` on the first step.** Django refuses to start without
  `DJANGO_SECRET_KEY`, so this says the SSH session did not carry the machine's environment.
  `fly ssh console -a workanotherday -C "/usr/bin/env"` shows what it does carry; the fix is to
  name what settings need on the command itself.
- **A checksum or signature error from `put-object`.** aws-cli v2 sends CRC64NVME trailing
  checksums by default and not every S3-compatible backend takes them. Adding
  `AWS_REQUEST_CHECKSUM_CALCULATION: when_required` to that step's `env` turns it off.
- **`Host key verification failed`.** `ssh-keyscan` was run against the wrong name, or the box was
  migrated. Rerun step 4's keyscan and set the secret again. Do not reach for
  `StrictHostKeyChecking=no`: it would hand the database to whatever answered.

## Constraints that shape the options

- **The runtime image has no `sqlite3` CLI.** Any copy made inside the container goes through
  Python's `sqlite3` module. The bundled SQLite is 3.46.1, so `VACUUM INTO` (3.27+) is available.
- **A Fly volume attaches to exactly one machine.** A separate scheduled machine cannot mount
  `wad_data` and read the file. This is the same constraint that forces `strategy = "immediate"`
  in `fly.toml`, and it rules out the obvious "add a cron machine" shape.
- **The DB is in WAL mode.** A copy of `db.sqlite3` alone, taken while gunicorn is serving, can
  miss committed transactions still in `-wal` and can capture a torn read. `VACUUM INTO` produces
  a single consistent self-contained file instead, and is safe to run against a live database.

## Other shapes this could take

### Raising snapshot retention

```bash
fly volumes update vol_r1jznjp0zpyd5jwr --snapshot-retention 60 -a workanotherday
```

One command, and it makes the first line useful for more than a work week. It addresses nothing
about single-provider risk, so it supplements the daily copy rather than replacing it.

### Litestream continuous replication

Continuous WAL shipping to object storage, with point-in-time recovery to the second. The
standard answer for SQLite on Fly.

Costs a second process in the container - a supervisor, or `litestream replicate -exec` wrapping
gunicorn - and puts object-store credentials inside the app. Buys recovery from "a bug wrote bad
rows on Tuesday, noticed Friday", which daily copies handle only to the nearest day.

For this database's size and change rate that is more machinery than the problem needs. It
becomes the right answer if losing a day of filings stops being acceptable.

### Pull-based from a machine you own

A cron doing `fly sftp get` to a laptop or home server. No new infra and no credentials anywhere
new, but it runs only when that machine is awake, so it is a second copy rather than a schedule
to depend on.

## Two things that bite regardless

### The Fernet key is part of the backup

`Seller.ksef_token` is an `EncryptedTextField`, encrypted under `KSEF_TOKEN_KEY`, which exists
only as a Fly secret. A database copy restored without that key comes back with the token column
unreadable; `from_db_value` reports `InvalidToken` as absent, so the seller lands in the form to
enter the token again.

So the key has to be backed up too - and deliberately **not** in the same place as the database
copies. Storing both together throws away exactly the property the field was written for, which
is that a copy of the volume is not enough to issue invoices under the NIP. A password manager is
the place for it; `app-backups` and the GitHub repository are not, and Fly will not print it back
(`fly secrets list` shows a digest).

### `dumpdata` is not a safe backup format

`manage.py dumpdata` serializes through the model layer, so `from_db_value` runs and the KSeF
token is written to the JSON **in the clear**.

A logical dump alongside the binary file is genuinely useful - it survives SQLite file-level
corruption and is readable without a matching SQLite version - but if one is kept it has to be
treated as a secret-bearing artifact, encrypted at rest, and it must not be the thing that gets
casually copied around for debugging.

## Restore

Do this once before it is needed, not during an incident.

Restoring a Fly snapshot creates a **new volume**, which means a new machine and a volume name
that no longer matches `fly.toml`:

```bash
fly volumes snapshots list vol_r1jznjp0zpyd5jwr
fly volumes create wad_data --snapshot-id <snapshot-id> --region fra --size 1 -a workanotherday
```

A daily copy comes from either provider. From Tigris, with a key that can read the prefix - which
is not the one the workflow runs under:

```bash
aws s3 ls s3://app-backups/workanotherday/database/ --endpoint-url https://fly.storage.tigris.dev
aws s3 cp s3://app-backups/workanotherday/database/db-20260903T023700Z.sqlite3.gz . --endpoint-url https://fly.storage.tigris.dev
```

Or from the Storage Box, where the sub-account reads back what it wrote:

```bash
ssh -p23 uXXXXXX-subN@uXXXXXX.your-storagebox.de ls
rsync --progress -e "ssh -p 23" uXXXXXX-subN@uXXXXXX.your-storagebox.de:db-20260903T023700Z.sqlite3.gz .
```

If what is wanted is older than what is in the directory, or the directory has been emptied, it
is in a snapshot, which only the main account can reach.

Then, either way:

```bash
gunzip db-20260903T023700Z.sqlite3.gz
fly sftp put db-20260903T023700Z.sqlite3 /app/data/import.sqlite3 -a workanotherday
```

Then swap it in. Remove `db.sqlite3` **and** its `-wal`/`-shm`/`-journal` sidecars before moving
the copy into place: stale sidecars against a swapped-in database can corrupt it on reopen. The
uploaded file arrives owned by root, and the application runs as `wad`, which has to be able to
write to it:

```bash
fly ssh console -a workanotherday -C "sh -c 'rm -f /app/data/db.sqlite3 /app/data/db.sqlite3-wal /app/data/db.sqlite3-shm /app/data/db.sqlite3-journal && mv /app/data/import.sqlite3 /app/data/db.sqlite3 && chown wad:wad /app/data/db.sqlite3'"
fly apps restart workanotherday
```

Verify with the integrity check and a row count before trusting a restore, remembering there is
no `sqlite3` CLI in the image:

```bash
fly ssh console -a workanotherday -C "python -c \"import sqlite3; c=sqlite3.connect('/app/data/db.sqlite3'); print(c.execute('pragma integrity_check').fetchone()); print(c.execute('select count(*) from wad_filing').fetchone())\""
```
