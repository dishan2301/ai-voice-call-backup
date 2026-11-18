# Call Morning Mail from Linphone

1. Connect the phone and laptop to the same trusted Wi-Fi. Read the listener
   address with `/usr/bin/python3 .local-asterisk/network_manager.py status`.
2. In Linphone, add a third-party SIP account. Set username and authentication
   username to `softphone`, domain to the laptop's reported LAN IP with port
   `5062`, transport to UDP, and password to the existing private SIP password.
   The laptop keeps that password in the private isolated Asterisk directory.
3. Wait for registration, then dial `700`. If Linphone kept credentials from a
   previous setup, remove and re-add the isolated account with the current address
   and password. Check the phone is on the same Wi-Fi if registration times out.
4. After the greeting, ask “Do I have any meeting emails?” and try one follow-up.
   Use the website for sync, message drafts, and reviewed sends.

The endpoint uses the laptop's existing Gmail read-only token. Start the bridge
with `systemctl --user start morningpa-sip-gmail.service` and check it with
`systemctl --user status morningpa-sip-gmail.service`. Disable automatic
startup and stop it with `systemctl --user disable --now morningpa-sip-gmail.service`.
Leave the isolated Wi-Fi manager running for SIP registration. Its trusted profile
policy limits the listener to the selected Wi-Fi network. SIP/RTP uses ordinary
UDP on that network, so use it only on the trusted LAN. Keep the bridge port on
loopback and keep the dedicated endpoint credential private.

For troubleshooting, compare the stages in order: Linphone registration,
Asterisk answer, Gemini greeting, and spoken Gmail result. Logs report call
admission, audio frame counts, and result state/count. The Asterisk 22 dialplan
application carries 8 kHz PCM. The bridge sends 16 kHz PCM to Gemini and converts
the returned 24 kHz PCM to 8 kHz telephone audio. Playback sends one 20 ms frame
at a time and flushes the final partial frame. Actual handset intelligibility
and latency should be checked by ear on the target phone.

The continuous pilot can be selected with `SIP_GMAIL_CONTINUOUS=1` in the SIP
bridge service environment. Roll back by removing that setting and restarting
only the bridge after the call ends. The pilot keeps listening across follow-ups
and has no application call-duration timer. Gemini may reconnect within the
call using its session resumption handle. If it cannot resume, the bridge gives
a spoken notice. Each call starts with an empty memory and clears it on hangup.

The pilot uses the existing Gmail read grant to fetch a 20-message inbox
snapshot and refresh it every 45 seconds. Search returns metadata and reads
message bodies separately. Log entries include lookup time, cache hit, first
audio and interruption events. Elapsed time from provider activity end is
recorded only when Gemini sends that signal.

Set `SIP_GMAIL_IST_COUNTS=1` alongside `SIP_GMAIL_CONTINUOUS=1` to enable
the optional third tool, `count_mail`. To roll back dates and counts alone,
remove that setting and restart the bridge once the line is idle. The continuous
conversation stays enabled.

Date filters resolve in Asia/Kolkata on every tool call. Both search and count
use the same filter builder. A count enumerates unique Gmail message IDs and
reports an incomplete lookup when it cannot read the last page. Ordinary searches
return one metadata page and use the warm cache for latest inbox mail. Received
scope includes archived non-sent mail and inbox copies with the sent label.
An archived message bearing the sent label cannot be classified as received
using these labels alone. The same-filter comparison in Gmail's web UI should
use individual messages because conversation view groups messages into threads.

Render serves the browser preview. Its public web endpoint accepts HTTP and
WebSocket traffic. The Linphone account in this guide requires an Asterisk host
that accepts SIP and RTP traffic. Publishing this source does not provision
the SIP host or point Linphone at Render.
