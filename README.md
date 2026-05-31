# stratatools

> Part of the **Strata** platform.

`stratatools` is the single entry point for local Strata bring-up and release.

- `st-setup` clones sibling repos and verifies local prerequisites
- `st-bootstrap` builds local CLIs, deploys bootstrap MonoFS + Guardian, and can start local devdns
- `st-release` builds, stamps, and reconciles partitions
- `st-image` builds or distributes partition images directly
- `st-aws-setup` provisions optional AWS bootstrap prerequisites

## Quick Start

Use this for a fresh machine with Docker, kubectl, Go, Python, and a working Kubernetes context.

```bash
git clone <your-stratatools-repo-url>
cd stratatools
uv sync
uv run st-setup

# optional local overrides
# cp bootstrap.local.env.example bootstrap.local.env

# optional AWS bootstrap prerequisites
# aws configure sso --profile admin-prod
# aws sso login --profile admin-prod
# uv run st-aws-setup --aws-profile admin-prod --aws-default-region us-east-1

uv run st-bootstrap deploy

# if you want full .strata DNS for local hostnames like http://doctor.strata/
# uv run st-bootstrap deploy --dns

uv run st-release --all --bump --wait
```

What that does:

1. `st-setup` clones sibling repos beside `stratatools` and seeds `../monofs/.env` with `MONOFS_ENCRYPTION_KEY`.
2. `st-bootstrap deploy` builds local CLIs into `~/bin`, deploys bootstrap MonoFS + Guardian, and stamps current bootstrap URLs into checked-in partition config.
3. `st-release --all --bump --wait` builds, distributes, stamps, pushes, and reconciles all managed partitions.

## Common Commands

Rebuild bootstrap binaries and images only:

```bash
uv run st-bootstrap build
```

Rebuild bootstrap binaries/images and local devdns binaries:

```bash
uv run st-bootstrap build --dns
```

Refresh stamped URLs after bootstrap endpoints change:

```bash
uv run st-bootstrap stamp-urls
```

Restart bootstrap workloads:

```bash
uv run st-bootstrap rollout
```

Restart bootstrap workloads and resync local devdns routes:

```bash
uv run st-bootstrap rollout --dns
```

Release a single partition:

```bash
uv run st-release --partition doctor --bump --wait
```

Run the local dogfood ingest flow:

```bash
uv run st-dogfood --router localhost:9090
```

## Local DNS

If you run bootstrap with `--dns`, `stratatools` also builds `devdns` and `devdnsctl`, starts local devdns, and keeps declared `DevDNSRoute` assets synced through local `kubectl port-forward` processes.

Bootstrap tries `127.0.0.1:80` first. If that bind is not allowed, it falls back to `127.0.0.1:18080` automatically.

Refresh DNS-backed stamped URLs with:

```bash
uv run st-bootstrap stamp-urls --dns
```

Current hostname exposed this way:

- Doctor query UI: `http://doctor.strata/`

If you want `doctor.strata` to work from any machine on your local network, do not point each machine at its own loopback address. Run `devdns` on one reachable LAN host and point clients or your router at that host.

### LAN Host

Pick one machine on your LAN to be the shared DNS/proxy host. Native Linux or native Windows is the right choice. WSL is not a good default for a LAN-wide server because WSL2 is usually NATed.

Assume this host has LAN IP `192.168.1.50`. Configure bootstrap like this:

```bash
cp bootstrap.local.env.example bootstrap.local.env
printf '\nDEVDNS_SERVER_IP=192.168.1.50\nDEVDNS_DNS_ADDR=0.0.0.0:53\nDEVDNS_PROXY_ADDR=0.0.0.0:80\n' >> bootstrap.local.env
uv run st-bootstrap build --dns
uv run st-bootstrap deploy --dns
uv run st-bootstrap stamp-urls --dns
```

On Linux, allow the binary to bind low ports:

```bash
sudo setcap cap_net_bind_service=+ep "$HOME/bin/devdns"
```

Run `setcap` again after each rebuild of `~/bin/devdns`, because rebuilding the binary can clear the capability.

If the host has multiple private interfaces or bootstrap picks the wrong address, keep `DEVDNS_SERVER_IP` set explicitly to the LAN IP you want clients to use.

If you cannot bind port `80`, set `DEVDNS_PROXY_ADDR` to a high port instead. DNS will still resolve, but clients will need `http://doctor.strata:<port>/`.

### Router Or DHCP

This is the best way to make `doctor.strata` work from anywhere on the LAN.

Set your router or DHCP server to advertise the LAN host IP as the DNS server, for example `192.168.1.50`. After that, reconnect clients or renew their DHCP lease so they pick up the new DNS server.

### Linux Client

If you are not changing router DHCP, point the Linux client at the LAN host DNS server directly.

With `systemd-resolved`, the usual shape is:

```bash
sudo resolvectl dns <iface> 192.168.1.50
sudo resolvectl domain <iface> '~strata'
```

Example:

```bash
sudo resolvectl dns enp3s0 192.168.1.50
sudo resolvectl domain enp3s0 '~strata'
resolvectl query doctor.strata
```

If your distro does not use `systemd-resolved`, configure the active network connection in NetworkManager or your distro resolver to use `192.168.1.50` as DNS.

NetworkManager example:

```bash
nmcli connection show
sudo nmcli connection modify "Wired connection 1" ipv4.ignore-auto-dns yes ipv4.dns "192.168.1.50"
sudo nmcli connection up "Wired connection 1"
getent hosts doctor.strata
```

### Windows Client

If you are not changing router DHCP, point the Windows adapter DNS server at the LAN host:

```powershell
Set-DnsClientServerAddress -InterfaceAlias "Wi-Fi" -ServerAddresses 192.168.1.50
```

Use the correct interface alias for your machine, for example `Ethernet` instead of `Wi-Fi`.

Example:

```powershell
Get-DnsClient | Select-Object InterfaceAlias,InterfaceIndex
Set-DnsClientServerAddress -InterfaceAlias "Wi-Fi" -ServerAddresses 192.168.1.50
ipconfig /flushdns
Resolve-DnsName doctor.strata
```

To switch the adapter back to DHCP-provided DNS later:

```powershell
Set-DnsClientServerAddress -InterfaceAlias "Wi-Fi" -ResetServerAddresses
```

### WSL Client

For WSL to use the LAN host DNS server, stop WSL from regenerating `resolv.conf` and write the LAN DNS server explicitly:

```bash
cat <<'EOF' | sudo tee /etc/wsl.conf
[network]
generateResolvConf=false
EOF
printf 'nameserver 192.168.1.50\n' | sudo tee /etc/resolv.conf
```

Then restart WSL from Windows:

```powershell
wsl --shutdown
```

### WSL As The Server

If the Strata stack runs inside WSL and you want other LAN devices to reach `doctor.strata`, do not use the default WSL networking as your LAN-wide server path. Use one of these instead:

- run `devdns` natively on Windows
- run `devdns` on a native Linux host
- use WSL mirrored networking or explicit Windows port proxy and firewall rules for UDP `53` and TCP `80`

## Notes

- `bootstrap.local.env` is git-ignored and auto-loaded by `st-bootstrap build`, `deploy`, `rollout`, and `stamp-urls`.
- AWS pusher deployment is opt-in and only happens when `GUARDIAN_AWS_ACCOUNT` is set.
- Keep the same `MONOFS_ENCRYPTION_KEY` after MonoFS has ingested data. Rotating it later can make existing blobs unreadable until they are re-ingested.
- `st-dogfood` excludes `agent` from the default ingest set.
- After `dev-workspace` is released locally, the intended endpoints are `http://localhost:8888/` and `ssh developer@localhost -p 2222`.
- SSH access still requires your public key in the `ssh-authorized-keys` config for the `dev-workspace` partition.

See [docs/USAGE.md](docs/USAGE.md) for the fuller command reference.
