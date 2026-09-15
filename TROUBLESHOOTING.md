# Troubleshooting — when something looks down

A symptom-first triage guide. It assumes the **Option A** topology from
[REMOTE_ACCESS.md](REMOTE_ACCESS.md): Flask on a Raspberry Pi at home, a Cloudflare Tunnel
to a public hostname, optionally Cloudflare Access in front of it, and Tailscale for admin
access. If you're on Option B, the Flask and backup sections still apply — the Cloudflare
ones don't.

Everything here is organized by **what you actually see first**, not by which component
failed. That's deliberate: several very different faults present as "the site is down", and
the fastest way out is to tell them apart before you start fixing.

Haven't deployed yet? Start with [First run, on your own computer](#first-run-on-your-own-computer);
the rest of this guide is for an instance that's already running on a server.

## First run, on your own computer

Problems that show up while you're installing, before any of the networking below exists.

**"Port 5000 is already in use."** Common on macOS, where AirPlay Receiver uses port 5000.
Turn AirPlay Receiver off in System Settings, or edit `app.py`, change `port=5000` to
`port=5001`, and visit `http://localhost:5001`.

**The login page won't accept anything.** `setup.py` doesn't create an account. Make one:

```bash
python create_user.py --admin
```

**"No module named ..." when you start the app.** You're not in the virtual environment.
Run `source .venv/bin/activate` (Mac/Linux) or `.venv\Scripts\activate` (Windows) first.

**UV data shows all zeros.** Check your longitude sign: in North America it's negative
(Oklahoma City is `35.4676, -97.5164`). Fix it in `config.json`, then run
`python backfill_uv.py --force`.

**Can't reach it from your phone.** The phone and computer need to be on the same WiFi, the
app has to be running, and the address starts with `http://`, not `https://`. Check that no
firewall is blocking port 5000.

## Placeholders

Substitute your own values throughout. Where an address appears, `x` stands in for a digit.

| Placeholder | Yours will look like |
|---|---|
| `app.yourdomain.com` | the public hostname routed to your tunnel |
| `<pi-hostname>` | your Pi's hostname, e.g. what you set at first boot |
| `192.168.x.x` | your actual LAN IP for the Pi — find it with `ip addr` on the Pi, or `ping <pi-hostname>.local` |
| `100.x.x.x` | your actual Tailscale IP for the Pi — `tailscale ip -4` on the Pi |
| `<your-team-name>` | your Zero Trust team name, `<team>.cloudflareaccess.com` |

## The map

```
 CLIENTS                  CLOUDFLARE EDGE              HOME (behind CGNAT)

 you, a browser  ------>  +---------------------+
 MFA + login              | Access gate         |
                          |   whole hostname    |
 clinician       ------>  +---------------------+       +------------------+
 token in URL             | Bypass  /portal/*   | ====> | Pi <pi-hostname> |
                          +---------------------+ tunnel|  flask :5000     |
 phone/wearable  ------>  | Bypass  /api/*      |       |  cloudflared     |
 bearer token             +---------------------+       |  tailscaled      |
                                                        +--------+---------+
                                                                 |
                                                                 v
                                                         biotracking.db
 ---------------------------------------------------------------------------
 you, admin (ssh) ---+
                     +--- Tailscale, does not touch Cloudflare ---> (same Pi)
 backup machine   ---+
```

The two halves share only their destination. **A Tailscale fault and a Cloudflare fault are
never the same fault** — an expired Tailscale key takes out SSH and your backups while the
public site keeps serving perfectly, and a tunnel or Access problem does the reverse. Half
of triage is working out which half you're in.

## Three front doors, one database

If you've added Access and the portal, three independent credentials now reach the same
SQLite file. They're issued, revoked and broken separately — changing your own password does
nothing to the other two.

| Who | Credential | Skips | Revoke by |
|---|---|---|---|
| You | Access identity + MFA, then the Flask login | nothing | Access policy, or change the password |
| Clinicians | capability token in the URL, time-limited, logged | Access **and** the Flask login | `/portals` → revoke |
| Phone apps | a signed client from `api_clients`, or the `api_token` bearer header until it's retired | Access **and** the Flask login | remove the client's entry and restart; or rotate `api_token` and re-enter it on each device |
| A device / wearable | a signed counter client, or the `wearable_token` bearer header | Access **and** the Flask login | remove the client's entry and restart; or rotate `wearable_token`, then reflash the device |

## A — the site won't load

1. **Check it cold**, from cellular rather than your home WiFi.

   ```bash
   curl -sI https://app.yourdomain.com/ | head -1
   ```

   A `302` toward `<your-team-name>.cloudflareaccess.com` is **healthy** — that's the Access
   gate doing its job, not an error.

2. A Cloudflare **error 1033** or an Argo Tunnel error page means the edge is up but
   `cloudflared` isn't connected. The fault is on the Pi side — skip to step 4.

3. **Get to the Pi.** Try the LAN name first; it works even when Tailscale doesn't.

   ```bash
   ssh you@<pi-hostname>.local
   ```

   No route at all? That's section B, a different failure.

4. **On the Pi, separate Flask from the tunnel.**

   ```bash
   systemctl status cloudflared
   curl -sI localhost:5000/login | head -1
   ```

   Flask `200` with cloudflared dead is a tunnel fault. No `200` means the app itself
   stopped — restart it.

5. Everything up but the browser is refused? That's an Access **policy** fault, not
   infrastructure. Confirm your applications still hold their destinations.

## B — you can't SSH to the Pi

1. **Name the cause** before assuming the Pi is down.

   ```bash
   tailscale ping <pi-hostname>
   ```

   `peer's node key has expired` is Tailscale's periodic node-key expiry (180 days by
   default). The Pi is fine and the public site is still serving. Only admin access and
   anything running over Tailscale — like a backup pull — are broken.

   This one is worth internalizing: `tailscale status` reports the machine as `offline`,
   which reads exactly like a dead Pi. It isn't. The Pi is still checking in with the
   control plane; peers just can't build a connection to it.

2. **Recover over the LAN.** Tailscale is the broken thing, so don't route through it.

   ```bash
   ssh you@<pi-hostname>.local        # 192.168.x.x
   ```

   If your `~/.ssh/config` points the short host name at the *Tailscale* address, the short
   name will fail during an expiry. Use the `.local` name or the LAN IP.

3. **Bring the tailnet back** from on the Pi, then open the URL it prints.

   ```bash
   sudo tailscale up
   ```

4. **Durable fix:** Tailscale admin console → Machines → your Pi → Disable key expiry. Key
   expiry on an always-on server is a trap — it locks you out of the exact machine you need
   Tailscale to reach.

5. If `<pi-hostname>.local` won't resolve either, the Pi really is down. Power, storage, or
   an unclean reboot.

## C — backups have stopped arriving

Two different faults produce the same silence, and the logs tell them apart.

1. **Read the receiving side first.**

   ```bash
   tail -5 ~/backups/pi-biotracking/pull.log
   ```

   `FAIL` lines mean a network or Tailscale partition. The snapshots still exist on the Pi
   and backfill on recovery — nothing is lost.

2. **If the pull looks clean, check whether the snapshot was ever made.**

   ```bash
   ssh <pi-hostname> 'tail -5 ~/backups/biotracking/backup.log'
   ```

   A missing night means the Pi was down through the backup window. That snapshot is gone
   permanently — but no health data is lost, because the live database is cumulative. You
   lose a restore point, not records.

3. If you schedule the snapshot with `cron`, a reboot through the backup window skips it
   silently and never catches up. A systemd timer with `Persistent=true` fires the missed
   run on the next boot instead. Worth the switch if your power isn't reliable.

## D — someone says their portal link is dead

1. **Ask what they see.** A *Cloudflare login page* means your Access carve-out broke. *Your
   own 403 page* means the token expired or was revoked — ordinary, and expected.

2. **Test the carve-out cold.** No real token needed:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://app.yourdomain.com/portal/badtoken
   ```

   `403` is correct — traffic reached the Pi. A `302` means your bypass application lost its
   `/portal/*` destination.

3. If it's only expiry, issue a fresh link from `/portals`. The old one stays revoked and the
   access log keeps its history.

## E — a phone or device stopped syncing

1. **Prove the lane still reaches Flask.** Use POST — this matters:

   ```bash
   curl -s -X POST -H 'Content-Type: application/json' -d '{}' \
     https://app.yourdomain.com/api/health-sync
   ```

   `{"error":"unauthorized"}` is **correct**: Access let it through and the bearer check ran.
   Getting HTML back means the `/api/*` bypass broke.

2. A **GET** on that same route returns `302` to `/login`, not `401`. That's `require_login`
   seeing `request.endpoint` as `None` on a 405 method mismatch — not an Access fault, and
   not worth chasing. Always test with POST.

3. If it's only one device, suspect the device before the server — especially anything with
   flaky WiFi or no real-time clock. Check when its last ingest actually landed.

4. **A 401 the device can't explain?** The server logs the reason; the device never sees it.

   ```bash
   journalctl -u <your-service> --since "1 hour ago" | grep "api auth refused"
   ```

   `unknown client` means the name isn't in `api_clients` (a misspelling, an entry pasted
   inside another client's braces, or no restart since the edit). `signature mismatch` means
   the secrets differ. A timestamp message means the device's clock has drifted more than five
   minutes. A **403** is the client getting in without the right permit or account, and its
   response says which.

## The cold check

Run this after any change to Access, your tunnel, or your DNS. From something with no
cookies and no session:

| Request | Expect | What it proves |
|---|---|---|
| `/` | `302` to your team domain | the gate is live |
| `/portals` | `302` to Access | management is gated — the wildcard didn't leak |
| `/portal/badtoken` | `403` from the app | the portal bypass works |
| `POST /api/health-sync` | `401` JSON | the API bypass works, bearer auth still enforced |

Both carve-out failures are **silent** — nothing alarms, and you find out when someone emails
you or when a week of device data turns out to be missing. That's the whole reason to run the
check rather than assume.

## Reading this offline

Several of the failures above are "the network is down" or "you can't reach the Pi",
which is an awkward moment to need a web page. `make-manpage.py` in this repo renders
this file as a man page, so it stays readable from a terminal with nothing else
working:

```bash
python3 make-manpage.py --install
man sardinetracker
```

No dependencies beyond Python 3 — it renders the markdown directly rather than
pulling in a converter.

Optionally, create a `runbook-site.md` next to it holding your own installation's
details. It is folded in as the opening section, and a `## Substitutions` table in it
replaces the placeholders above with your real values throughout, so the commands can
be pasted without editing:

```markdown
## Substitutions

| Placeholder | Real |
|---|---|
| `app.yourdomain.com` | `app.example.com` |
| `<pi-hostname>` | `raspberrypi` |
```

That file is gitignored. Keep your own addresses out of version control.
