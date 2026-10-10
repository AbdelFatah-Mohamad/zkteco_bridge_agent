# ZKTeco LAN Bridge

A small program that runs on a PC inside a customer's network and works the
biometric clocks on behalf of an Odoo server that cannot reach them.

**The premise, and everything follows from it:** the PC has no static address
and accepts no inbound connection. It polls Odoo outbound — every fifteen
minutes when idle, every twenty seconds while it has work in hand — carries out
whatever jobs come back, and posts the results. Odoo can never make it act; it
can only leave work where the bridge will find it.

The other half lives in `dps_zkteco_biometric_integration`. The wire contract
between them is `doc/BRIDGE_PROTOCOL.md` in that module, and it is the file to
change first when the protocol changes.

## What it does today

`identity` (test connection and read the device's own details), `list_users`,
and `pull_attendance`. Employee push, fingerprint enrolment and user deletion
are the next phase; the server already speaks them, and the agent declares its
`capabilities` on every poll so the server never hands out a job this build
cannot do.

## Running it

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m zkbridge --open
```

First start prints an access code and a URL. Open the page, enter the Odoo
address and the bridge token (Odoo: *Attendances → Configuration → LAN
Bridges → Generate Token* — it is shown once and cannot be recovered), and the
bridge starts working.

`--data-dir` puts settings, state and logs somewhere else, which is how the
tests and a second instance are run.

## Installing it as a service

Build once per platform, then use the script for that platform:

```sh
.venv/bin/pyinstaller packaging/zkbridge.spec        # -> dist/zkbridge/
sudo packaging/linux/install.sh                      # systemd
powershell -File packaging\windows\install.ps1       # WinSW, elevated
```

The PC needs **outbound 443** to Odoo and **outbound 4370** to each clock.
**Nothing inbound** — no firewall rule is created and none is needed. Say this
to the customer's IT before they helpfully open a port.

## Things that are the way they are on purpose

**It never calls `clear_attendance()`.** The terminal keeping its entire buffer
is what makes a week of bridge downtime a bandwidth problem instead of a
data-loss problem, and it is what makes every retry in the protocol safe. The
redundancy that makes the payload large is the same property that makes the
system recoverable. Any future optimisation that clears the device destroys it.

**It converts no timestamps and carries no timezone library.** Punches travel as
naive device-local wall clock exactly as the terminal reported them; Odoo owns
the conversion. That is both a smaller dependency surface and a structural
guarantee that the bridge cannot helpfully convert anything.

**Its own clock is depended on by nothing.** No timestamp window, no nonce. A
drifting office PC is the likeliest real-world condition there is, and a time
window would turn it into an outage. The clock is reported so a human can see
it is wrong, and that is all.

**`vendor/zk/` is byte-identical to the module's copy and must stay so.** The
error classifier matches on message text from that source, because the library
collapses every exception into one type — so a divergence would break
classification silently and turn every failure into `bridge_error`. Three
guards: `ZK_SHA256SUMS` checked by the test suite here, a hash reported to Odoo
on every poll, and the module's own test suite.

**One connection per job, never reused.** Firmwares drop idle sessions
silently, so a reused handle fails on the *next* command with an error
indistinguishable from a real fault — and `enroll_user` leaves a sixty-second
timeout on the socket when it raises.

**The local page is bound to 127.0.0.1 with no option to change it.** There is
no legitimate reason for it to be reachable from the LAN, and an option to make
it so would eventually be switched on. Loopback is not an authorisation
boundary on a shared machine, so an access code, a signed cookie, a Host/Origin
check and CSRF tokens guard it as well.

**The token is plaintext in a directory only administrators can read.** DPAPI
and `keyring` were considered and rejected: DPAPI ties the ciphertext to the
service account, so changing that account or restoring the PC from an image
silently bricks the bridge, and the recovery is "re-enter the token" — exactly
what plaintext costs anyway, without a Windows-only code path. Anyone who can
read that directory can read the token; the answer is one-click revocation in
Odoo, not local obfuscation.

## The setup guide

`doc/WINDOWS_SETUP_ar.html` is the step-by-step Windows guide for the person
installing the bridge at a customer site. The same page is opened from the
bridge's own **Guide** link (`/guide`, readable without the access code, since
it says where to find the code) and from the **Windows Setup Guide** button on
the bridge form in Odoo.

Three copies would drift within a release, so there is one source. Edit
`doc/guide_src/body.html` and the screenshots in `doc/guide_src/img/`, then:

```sh
python3 doc/guide_src/build_guide.py ../AlshayebCo/dps_zkteco_biometric_integration
```

which writes all three. The copy inside the program carries no web fonts: its
page allows no outside requests, and the PC may have no route to them anyway.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

Two of them carry most of the weight: `test_the_timestamp_is_naive_device_local`
pins the one thing the bridge must never do, and
`test_every_marker_still_exists_in_the_vendored_library` pins the coupling that
would otherwise rot into "every failure is `bridge_error`" without a single test
going red.

No test opens a socket to a real terminal.
