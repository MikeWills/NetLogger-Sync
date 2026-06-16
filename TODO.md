# TODO

## Add N1MM Logger+ support

- [x] Research N1MM Logger+'s inbound QSO interface — N1MM receives QSOs via
      WSJT-X binary UDP "Log QSO" packets (type 5, schema 3) on port 2237,
      the same mechanism used by GridTracker2 and JTAlert. N1MM must have
      "Enable WSJT-X Decode List" checked in Config > Configure Ports > WSJT-X tab.
- [x] Add `send_to_n1mm()` sender in `netlogger_bridge.py` (binary WSJT-X UDP protocol).
- [x] Add `[n1mm]` section to `SAMPLE_CONFIG` / `config.ini` (enabled, host, port, my_call).
- [x] Add N1MM fields to the GUI, following the WaveLog/N3FJP `LabelFrame` pattern.
- [x] Update README.md setup instructions and CLAUDE.md architecture notes.

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
