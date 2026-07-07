# Remote Access Guide — sardinetracker

## Read This First

This document describes how to access sardinetracker from outside your local network - from your phone on cellular, from work, from anywhere.

Before you do any of this, understand what you are doing:

**Anything connected to the internet is dangerous and insecure. The most secure place for your health data is in your own head with a tight lip. A faraday cage helps too.**

The default setup runs on localhost. Nothing leaves your computer. Nobody can reach it but via physical access to that machine, *on* that machine, on *that* network. That is the safest possible configuration and it is sufficient for most people.

This guide exists for people who understand the risk surface and want remote access anyway. If you are not comfortable with concepts like VPNs, reverse proxies, open ports, and what it means to expose a service to the internet, stop here. Use the local setup. It is genuinely good enough; for the gap between the doctor's waiting room or the ER for four hours and getting home to your own network, travel with a notebook. Seriously, it's great therapy and also provides notes for future-you when you want to log the whole thing in your tracker.

If you proceed, you accept that you are responsible for the security of your own data. The author of this software is not responsible for data exposure resulting from network configuration choices you make.

---

## What "Less Internet" Means and Doesn't Mean

There is no such thing as "connected to the internet but safe." There is only a spectrum of exposure.

The setup described in this guide is:

- More secure than a public URL
- More secure than port forwarding your router
- Not as secure as local only
- Not as secure as not having the data digitally at all

Proceed accordingly.

---

## The Setup: Raspberry Pi + Tailscale + Oracle VM

This is one specific approach to remote access that trades some security for genuine convenience. It does not expose a public IP directly. Traffic is routed through a private Tailscale mesh network to an Oracle Cloud VM which acts as an exit node.

### What you need

- A Raspberry Pi (any model capable of running Python 3.9+, a Pi 4 or newer is comfortable. But if you want to get weird with it, host it on Plan 9 inside a gameboy color. I'll buy you lunch.)
- A Tailscale account (free tier is sufficient)
- An Oracle Cloud account (free tier VM is sufficient, though I ended up getting the 14$/month plan because I got lazy and tired of menu options. Warning: Oracle isn't user friendly)
- A DNS name if you want a human-readable URL (optional, duckdns.org is a great resource for a free domain)
- Basic comfort with SSH and the Linux command line (man -k is your friend)
- Starlink or any ISP — this setup works without a static public IP, which is the point

### How it works

```
Your phone / laptop (anywhere)
        |
        | (Tailscale encrypted tunnel)
        |
Oracle Cloud VM (public IP, Tailscale exit node)
        |
        | (Tailscale encrypted tunnel)
        |
Raspberry Pi (your home network, running biotracking)
        |
        | (localhost)
        |
biotracking app + SQLite database
```

Your database never leaves the Raspberry Pi. The Oracle VM sees only encrypted Tailscale traffic - it cannot read the contents. Your phone connects to the Oracle VM's public IP, which forwards traffic through the Tailscale mesh to the Pi.

### Step 1: Set up sardinetracker on your Raspberry Pi

Follow the standard installation instructions in the README. Verify it runs on `http://localhost:5000` from the Pi itself before touching any networking. If you don't have a keyboard or screen, install faceless debian onto the pi and ssh into it. You don't need a UI for any of this.

### Step 2: Install Tailscale on the Raspberry Pi

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the authentication link. The Pi will appear in your Tailscale admin console with a Tailscale IP (usually in the `100.x.x.x` range).

### Step 3: Set up an Oracle Cloud Free Tier VM

1. Create an account at [cloud.oracle.com](https://cloud.oracle.com) — the always-free tier includes a small VM that is sufficient for this purpose
2. Create a VM instance (Ubuntu 22.04 LTS is a reasonable choice, but I ended up with RedHat. Choose your own adventure.)
3. Note the public IP assigned to your VM, and dont forget to save your key!
4. SSH into the VM and install Tailscale:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Both the Pi and the VM should now appear in your Tailscale admin console.

### Step 4: Configure the Oracle VM as a reverse proxy

Install nginx on the Oracle VM:

```bash
sudo apt update && sudo apt install nginx -y
```

Create a config file at `/etc/nginx/sites-available/biotracking`:

```nginx
server {
    listen 80;
    server_name YOUR_ORACLE_PUBLIC_IP;

    # Basic auth is strongly recommended — see security notes below
    # auth_basic "<TRACKING_APP_NAME>";
    # auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://YOUR_PI_TAILSCALE_IP:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Replace `YOUR_ORACLE_PUBLIC_IP` with your VM's public IP and `YOUR_PI_TAILSCALE_IP` with your Pi's Tailscale IP (e.g., `100.x.x.x`).

Enable the config:

```bash
sudo ln -s /etc/nginx/sites-available/biotracking /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### Step 5: Open the firewall on Oracle Cloud

Oracle Cloud has two layers of firewall, the OS-level firewall and the cloud-level security list. You need to open port 80 (and 443 if you add HTTPS, which you should) in both.

**OS firewall:**

```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

**Cloud security list:**

In the Oracle Cloud console, go to your VM's subnet, edit the security list, and add an ingress rule for TCP port 80 (and 443) from source `0.0.0.0/0`.

### Step 6: Start biotracking on the Pi to listen on all interfaces

By default biotracking listens on localhost only. To allow Tailscale traffic, edit `app.py` and change:

```python
host='127.0.0.1'
```

to:

```python
host='0.0.0.0'
```

Restart the app. You should now be able to reach it from your Oracle VM's public IP. And if you dig around the tailscale options you can even give it a nifty easy-to-remember-name.

**Keep it running:**
Use systemd, screen, or tmux so the app doesn't die when you close SSH:

```bash
screen -S biotracking
python3 app.py
# Ctrl+A, D to detach
```

---

## Security Notes — Please Read These

### Add authentication

The nginx config above has basic auth commented out. Uncomment it. Without authentication, anyone who finds your Oracle VM's IP can reach your health data.

To create a password file:

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd yourname
```

A strong, unique password. Not your email password. Not your phone PIN.

### Add HTTPS

HTTP transmits data in plaintext. Add a TLS certificate via Let's Encrypt:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your.domain.com
```

This requires a domain name pointed at your Oracle VM. Free domains are available from services like Duck DNS if you don't want to pay for one.

Without HTTPS, your health data travels in plaintext between your phone and the Oracle VM. Tailscale encrypts the Pi-to-VM leg. The VM-to-you leg is unencrypted without TLS.

### Keep software updated

```bash
sudo apt update && sudo apt upgrade -y
```

Run this on your Oracle VM regularly. Unpatched software is how things go wrong.

### Understand your Tailscale ACLs

By default Tailscale allows all devices in your network to talk to each other. Review your ACL settings in the Tailscale admin console and restrict access to only what is needed.

### Monitor your Oracle VM

Oracle Cloud provides basic monitoring. Check it occasionally. Unusual traffic patterns are worth investigating.

---

## On Starlink Specifically

Starlink uses carrier-grade NAT (CGNAT) which means you do not get a static public IP and cannot port-forward directly. This is exactly why the Oracle VM approach is useful, it provides the stable public IP that Starlink doesn't give you. The Tailscale tunnel handles the rest.

This setup will work on any ISP that uses CGNAT, not just Starlink.

---

## You Can Always Go Back

If any of this feels wrong, too complex, or like you've exposed something you didn't mean to -- stop. Turn off the nginx service on the Oracle VM. Your data on the Pi is unaffected. The local setup is always there.

```bash
sudo systemctl stop nginx
```

Done. You're local only again.

---

## Auto-Sync from Apple Health (iOS Shortcut)

Instead of manually reading values off your Apple Health app and typing them into biotracker, you can set up an iOS Shortcut that pulls your health data and sends it automatically.

**What it syncs:** steps, HRV, resting heart rate, and basal body temperature (delta).

**What it doesn't sync:** sleep (enter manually -- Apple Health struggles with polyphasic sleep and sleepwalking), sun exposure minutes (Apple doesn't expose Time in Daylight to Shortcuts despite tracking it on the watch), and period flow (use the biotracker directly -- it's better at cycle tracking than Apple Health anyway in my experience).

### Setup

Your tracker instance has an API endpoint at `/api/health-sync` that accepts health data via a secure token. The token is in your `config.json` file on the Pi (generated when you run `setup.py`). Treat this token like a password.

### Building the Shortcut

Open the **Shortcuts** app on your iPhone and create a new shortcut:

**1. Get today's date**
- Add a **Date** action
- Add a **Format Date** action, set format to **Custom**: `yyyy-MM-dd`

**2. Pull health data**

Add four **Find Health Samples** actions, one for each metric:

| Action | Sample Type | Sort By | Limit |
|--------|------------|---------|-------|
| 1 | Step Count | Start Date, Most Recent | 1 |
| 2 | Heart Rate Variability | Start Date, Most Recent | 1 |
| 3 | Resting Heart Rate | Start Date, Most Recent | 1 |
| 4 | Body Temperature | Start Date, Most Recent | 1 |

For Step Count, make sure you're getting the **sum for the day**, not just the most recent sample.

**3. Build the request**

Add a **Dictionary** action with these keys:

| Key | Type | Value |
|-----|------|-------|
| user_id | Number | Your user ID (usually 1) |
| date | Text | *select the Formatted Date from step 1* |
| steps | Number | *select result from step 2, action 1* |
| hrv | Number | *select result from step 2, action 2* |
| resting_heart_rate | Number | *select result from step 2, action 3* |
| basal_temp_delta | Number | *select result from step 2, action 4* |

**4. Send it**

Add a **Get Contents of URL** action:
- URL: `https://<YOUR_SERVER>/api/health-sync`
- Method: **POST**
- Headers:
  - `Authorization`: `Bearer YOUR_TOKEN_HERE`
  - `Content-Type`: `application/json`
- Request Body: **JSON** -- select the Dictionary from step 3

**5. Test it**

Tap the play button to run the shortcut. You should see a response like `{"ok": true, "fields_updated": ["steps", "hrv", ...]}`. Check your biotracker daily entry to confirm the values appeared.

**6. Automate it**

Go to the **Automation** tab in the Shortcuts app:
- Tap **+**, choose a trigger:
  - **"Bedtime begins"** -- syncs when your wind-down starts (recommended)
  - **"Time of Day"** -- set to 11:50 PM daily
- Set to **Run Immediately** so it doesn't ask for confirmation
- Select your Health Sync shortcut

Once set up, your phone will quietly sync your health data every night without you lifting a finger. On bad days, that's one less thing to worry about.

### Security Note

The API token in your Shortcut has write access to your health data. It can only write a limited set of biometric fields (steps, HRV, heart rate, temperature, sun minutes) and cannot touch symptoms, flare status, medications, or notes. But still -- don't share your Shortcut with anyone unless you trust them with your biotracker login.

---

## Final Note

This guide describes one specific setup that one person uses. It is not the only way to do remote access and it may not be the right way for you. Security is not a product, it's a practice. The threat model for your health data is yours to assess.

If you are a domestic violence survivor, a person in an unsafe living situation, or someone whose health data could be used against them in any context -- think carefully before putting any of this on the internet in any form. Local only may be the right choice permanently.

Take care of your data the way you take care of yourself. Carefully, with attention, and with the understanding that you are worth protecting.

---

*This document is provided for informational purposes only. The author is not responsible for security outcomes resulting from network configuration choices made by users of this software.*
