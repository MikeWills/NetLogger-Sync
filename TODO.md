# TODO

## Add N1MM Logger+ support

**Research finding: N1MM Logger+ has no inbound QSO API.**

N1MM's networking interface is outbound-only — it broadcasts `ContactInfo`,
`ContactReplace`, and `ContactDelete` XML packets via UDP so that *other*
programs can react to QSOs logged in N1MM. There is no documented mechanism
for an external program to *submit* a QSO to N1MM over the network. The only
inbound control is `Radio_SetFrequency` (frequency change only).

Support for N1MM as an output target is not feasible without N1MM adding an
inbound QSO API. No action items remain.

Reference: https://n1mmwp.hamdocs.com/appendices/external-udp-broadcasts/

## Add Ham Radio Deluxe (HRD) Logbook support

- [x] Research HRD Logbook's API — HRD accepts N1MM-compatible UDP XML
      ContactInfo packets on port 12060 (Tools > QSO Forwarding > N1MM).
- [x] Add `send_to_hrd()` sender in `netlogger_bridge.py`.
- [x] Add `[hrd]` section to `SAMPLE_CONFIG` / `config.ini` (enabled, host,
      port, my_call).
- [x] Add HRD fields to the GUI, following the WaveLog/N3FJP `LabelFrame` pattern.
- [x] Update README.md setup instructions and CLAUDE.md architecture notes.

## Add Log4OM support

- [x] Research Log4OM's UDP inbound ADIF service — sends plain ADIF record as
      a UDP datagram to a user-configured port (Communicator > Inbound Connections).
- [x] Add `send_to_log4om()` sender in `netlogger_bridge.py`.
- [x] Add `[log4om]` section to `SAMPLE_CONFIG` / `config.ini` (enabled, host, port).
- [x] Add Log4OM fields to the GUI, following the WaveLog/N3FJP `LabelFrame` pattern.
- [x] Update README.md setup instructions and CLAUDE.md architecture notes.

## Add DXLab Suite (DXKeeper) support

- [x] Research DXKeeper's TCP `externallog` command — TCP on port 52001
      (base port 52000 + 1); message format uses DXLab ADIF field encoding.
- [x] Add `send_to_dxkeeper()` sender in `netlogger_bridge.py`.
- [x] Add `[dxkeeper]` section to `SAMPLE_CONFIG` / `config.ini` (enabled, host, port).
- [x] Add DXKeeper fields to the GUI, following the WaveLog/N3FJP `LabelFrame` pattern.
- [x] Update README.md setup instructions and CLAUDE.md architecture notes.
