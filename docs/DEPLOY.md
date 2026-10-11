# Putting Bhajan Studio on a server

Bhajan Studio normally runs on your own computer (see the [README](../README.md)). This guide runs it
on a Linux server instead, in Docker, so that a group can open it from a browser and nobody has to
install anything. It takes about ten minutes of your time, plus the first build.

**Please read this first.** A hosted copy downloads bhajans, recordings and sheet music from
[Sai Rhythms](https://sairhythms.org) for everyone who uses it, and serves them, and the shifted
and split audio made from them, from your server. That content belongs to its owners (Sai
Rhythms is run by the Sri Sathya Sai International Organization, SSSIO), and the sheet music is
shared for non-commercial devotional singing, learning and teaching. So:

- Ask the owners before you make a hosted copy available to a group, and use it only the way they
  approve.
- Always keep the password on (this guide makes you set one), and give it only to the people the
  owners are happy for you to share it with.
- Don't publish the server's address, and don't open it to the public.

## What you need

- A Linux server (x86-64 or ARM64) that you can log in to. Any ordinary VPS will do.
- [Docker Engine](https://docs.docker.com/engine/install/) with the Compose plugin (Docker's
  installer includes both).
- For HTTPS: a domain name you control. The next sections explain it.

**How big a server?** These are measured on the CPU image on an 8-core ARM machine, so treat them
as a guide:

| | Without Parts | With Parts |
|---|---|---|
| Disk | about 3 GB free | about 12 GB free |
| RAM | 2 GB is comfortable (the app idles at about 0.1 GB, and shifting a 5-minute recording takes about 0.2 GB more) | 8 GB recommended (a split peaks at about 5 GB) |
| CPU | 1 core. Shifting a 5-minute recording takes about 25 s | 4 or more cores |
| Image (unpacked) | 0.8 GB | 2.1 GB |

The disk figure for Parts is the image, plus the separation models (about 1.6 GB, downloaded the
first time someone splits a recording), plus the cache: split parts are kept to about 3 GB, and
rendered audio to about 500 MB. The cache lives in the data volume and prunes itself.

## Quick start

On the server:

1. **Install Docker.** On Ubuntu or Debian, Docker's own script does it:

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   ```

   (Other systems: follow the [install guide](https://docs.docker.com/engine/install/).)

2. **Get Bhajan Studio.**

   ```bash
   git clone https://github.com/PMN123/bhajan-studio.git && cd bhajan-studio
   ```

3. **Make your settings file.** Copy the example, then make a strong password and paste it in as
   `BHAJAN_PASSWORD`:

   ```bash
   cp .env.example .env
   openssl rand -base64 24
   nano .env
   ```

   Docker Compose refuses to start without a password, on purpose: a server on the internet must
   not be open. Every setting in `.env` has a comment that explains it.

4. **Start it.**

   ```bash
   sudo docker compose up -d
   ```

   The first time, this builds the image. That takes about 8 minutes on an 8-core ARM server (about
   3 without Parts; an x86-64 server has less to compile, so it should be faster) and downloads about
   0.5 GB, mostly PyTorch. Later starts take a few seconds. Check on it with
   `sudo docker compose ps` (the status becomes **healthy**) and `sudo docker compose logs -f`.

5. **Open it.** Go to `http://YOUR-SERVER-ADDRESS:8777`. You see a sign-in page: type your
   password. Then pick a bhajan.

That works, but the password then travels in plain text over the internet, where anyone on the
way can read it. For anything beyond a home network, add HTTPS (next section).

**The firewall.** Docker publishes its ports itself, and this skips firewalls such as `ufw`: if
the app's port is published, it is open to everyone, whatever `ufw status` says. The next section
closes it. Otherwise use your VPS provider's firewall (which sits outside the server and does work)
to allow only what you need.

## HTTPS, with a domain

[Caddy](https://caddyserver.com) sits in front of the app, gets a free certificate from Let's
Encrypt by itself, and renews it.

1. **Point your domain at the server.** At your domain provider, add an **A record** for a name
   such as `bhajan.example.com` with the server's IPv4 address (and an **AAAA record** if the
   server has IPv6). Wait until `ping bhajan.example.com` shows that address.
2. **Open ports 80 and 443** on the server's firewall. Caddy needs both: 80 to prove to Let's Encrypt
   that the domain is yours, and 443 for the site.
3. **Edit `.env`:**

   ```bash
   DOMAIN=bhajan.example.com
   BHAJAN_ALLOWED_HOSTS=bhajan.example.com
   BHAJAN_BIND=127.0.0.1
   ```

   `BHAJAN_BIND=127.0.0.1` makes the app's own port 8777 listen on the server only, so the only
   way in from outside is through Caddy and its HTTPS.
4. **Start both:**

   ```bash
   sudo docker compose --profile https up -d
   ```

   Watch Caddy get the certificate with `sudo docker compose logs -f caddy` (it takes under a
   minute), then open `https://bhajan.example.com`.

To always start Caddy without typing the profile, put `COMPOSE_PROFILES=https` in `.env`.

If the certificate doesn't arrive, the cause is nearly always DNS (the A record doesn't point
here yet) or a closed port 80 or 443.

## The password and allowed host names

- **`BHAJAN_PASSWORD`:** everything needs it. Visitors see a sign-in page, and stay signed in for
  30 days on that device. Scripts and uptime monitors can send the password as HTTP Basic auth
  instead (any user name: `curl -u me:YOUR-PASSWORD https://bhajan.example.com/api/search?q=sai`),
  or just check `/api/health`, which needs no password and only says that the server is running.
  To change the password, edit `.env` and run `sudo docker compose up -d` again. That also signs
  everyone out.
- **`BHAJAN_ALLOWED_HOSTS`:** the host names the server answers to, separated by commas. Requests
  addressed to any other name get a `403`. Put your domain here. To also use the server by its
  address, add that: `bhajan.example.com,203.0.113.7`. Leave it empty to accept any name.
  `127.0.0.1` and `localhost` always work.

## The data volume, and backups

Everything the app downloads or works out (the song cache, recordings, sheets, the separation
models, split parts) is in one Docker volume called `bhajan-studio_bhajan-data`, mounted at `/data`
in the container. Nothing in it is irreplaceable: it is all a cache that is fetched again as
needed. (Your weekly session list is kept in each person's browser, not on the server. The password
is in `.env`, which you should keep a copy of somewhere safe.) A backup only saves time, mostly
the 1.6 GB model download and the splits.

Back it up into a file in your home folder:

```bash
sudo docker run --rm -v bhajan-studio_bhajan-data:/data:ro -v "$HOME":/backup alpine \
  tar czf /backup/bhajan-data-$(date +%F).tar.gz -C /data .
```

Put it back into a new, empty volume:

```bash
sudo docker run --rm -v bhajan-studio_bhajan-data:/data -v "$HOME":/backup alpine \
  sh -c "tar xzf /backup/bhajan-data-2026-10-11.tar.gz -C /data && chown -R 1000:1000 /data"
```

(Use your own file name. The app runs as user 1000 in the container, so the files need that owner.)

To start from nothing, run `sudo docker compose down -v`. The `-v` deletes the volume.

If you would rather keep the data in a folder of your own, replace `bhajan-data:/data` with
`/srv/bhajan-data:/data` under `volumes:` in `docker-compose.yml`, and run
`sudo chown 1000:1000 /srv/bhajan-data` once.

## Updating

```bash
cd bhajan-studio
git pull
sudo docker compose up -d --build
```

The data volume is kept. When only the app's own files changed, the rebuild takes seconds; when
the dependencies changed, it takes a few minutes. `sudo docker image prune -f` removes the old
image afterwards.

## Parts: CPU or GPU

Splitting a recording into singing, tabla and harmonium is the heavy part: it runs two PyTorch
models, one recording at a time, in a separate process.

- **On the CPU** (the default image) it works, but it is slower than on a Mac with Apple Silicon,
  which takes about 1 to 3 minutes per bhajan. Measured on 8 ARM cores: a 4-minute bhajan took
  13 minutes, about three times its length (plus 6 minutes, the first time, to download the
  models). Shifting the split parts to another key then takes about 10 seconds, and exporting a mix
  under a second.
  More cores help. Everyone who clicks **Split into parts** shares one queue, so give people a
  heads-up.
- **On an NVIDIA GPU** it is much faster. You need a server with a GPU, the NVIDIA driver and the
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
  Then:

  ```bash
  sudo docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
  ```

  This builds PyTorch for CUDA 13 (driver 580 or newer), and the image is several GB larger. For an
  older driver or GPU (the GTX 10 series, the V100), put
  `TORCH_INDEX=https://download.pytorch.org/whl/cu126` in `.env` first. Add `--profile https`
  as well if you use HTTPS. (This build isn't tested here yet: it needs a machine with a GPU.)

The first split downloads the two models (about 1.6 GB) into the data volume, so it is slower
than the rest.

## Without Parts

If you only need key detection, pitch shifting and the sheet music, leave Parts out. The image is
much smaller (0.8 GB instead of 2.1 GB) and builds faster (3 minutes instead of 8), and there is no **Split into parts**.
Put `WITH_PARTS=0` in `.env` and build again:

```bash
sudo docker compose up -d --build
```

Switch back by setting `WITH_PARTS=1` and building again.

## Going easy on Sai Rhythms

The server waits 10 seconds between its requests to sairhythms.org (their `robots.txt` asks for
that), with `BHAJAN_SITE_GAP=10`. So the first time anyone opens a song that isn't cached yet, it
takes a few seconds longer: the song page comes first, then the sheet music and the recording
each wait their turn. Everything is cached afterwards, and the full song list ships with the app, so
search is instant. Please don't lower the gap.

## Something wrong?

- **`required variable BHAJAN_PASSWORD is missing a value`:** you haven't set the password in `.env`.
- **The sign-in page says the password is not right:** use the `BHAJAN_PASSWORD` in `.env`. After
  you change it, run `sudo docker compose up -d` so the server picks it up.
- **The browser says `403`:** the address you typed isn't in `BHAJAN_ALLOWED_HOSTS`.
- **`permission denied` on `/data`, with your own folder:** run `sudo chown 1000:1000` on it.
- **The status stays `starting` or `unhealthy`:** read `sudo docker compose logs bhajan-studio`.
- **A split fails right away, or the buttons are missing:** the image was built with `WITH_PARTS=0`,
  or ran out of memory. `curl http://127.0.0.1:8777/api/health` shows `"parts": true` when Parts
  is available.
- **Check it by hand:** `curl http://127.0.0.1:8777/api/health` on the server prints
  `{"ok":true,"version":...}`.

## Licences

Bhajan Studio's own code is MIT. The container image also bundles other people's software under
other licences: a GPL build of FFmpeg (inside imageio-ffmpeg), the Rubber Band library (GPL v2 or
later, through pylibrb), PyMuPDF (AGPL v3), and, with Parts, diffq (CC BY-NC 4.0, for non-commercial
use only). The README's "Acknowledgements & licenses" has the full list.

Building the image yourself and running it for your group is fine. **Publishing an image** (on
Docker Hub, GitHub, or anywhere else) means passing those components on, under their licences, and
is for the owner of the repository to decide. This guide doesn't publish one: everyone builds
their own with `docker compose up -d`.
