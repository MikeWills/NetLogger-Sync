# TODO

## Add N1MM Logger+ support

- [ ] Research how N1MM Logger+ accepts incoming contact data (UDP broadcast
      XML on port 12060 is used for radio/contact info between programs —
      confirm whether it can be used to *add* a QSO, or whether N1MM only
      consumes ADIF via file import).
- [ ] Add a `send_to_n1mm()` sender in `netlogger_bridge.py` following the
      pattern of `send_to_n3fjp()`.
- [ ] Add `[n1mm]` section to `SAMPLE_CONFIG` / `config.ini` (enabled, host,
      port, and whatever else the chosen integration method needs).
- [ ] Add N1MM fields to the GUI (`netlogger_gui.py`) — enable checkbox +
      connection settings, following the WaveLog/N3FJP `LabelFrame` pattern.
- [ ] Update README.md setup instructions and CLAUDE.md architecture notes.

## Add Ham Radio Deluxe (HRD) Logbook support

- [ ] Research HRD Logbook's API for adding QSOs (TCP/DDE interface — confirm
      command format, default port, and response handling, similar to how
      N3FJP's `ADDADIFRECORD` was reverse-engineered).
- [ ] Add a `send_to_hrd()` sender in `netlogger_bridge.py`.
- [ ] Add `[hrd]` section to `SAMPLE_CONFIG` / `config.ini` (enabled, host,
      port).
- [ ] Add HRD fields to the GUI, following the WaveLog/N3FJP `LabelFrame`
      pattern.
- [ ] Update README.md setup instructions and CLAUDE.md architecture notes.

## Add Log4OM support

- [ ] Research Log4OM's local TCP/UDP command API for adding QSOs (confirm
      command format, default port, and response handling, similar to how
      N3FJP's `ADDADIFRECORD` was reverse-engineered).
- [ ] Add a `send_to_log4om()` sender in `netlogger_bridge.py`.
- [ ] Add `[log4om]` section to `SAMPLE_CONFIG` / `config.ini` (enabled, host,
      port).
- [ ] Add Log4OM fields to the GUI, following the WaveLog/N3FJP `LabelFrame`
      pattern.
- [ ] Update README.md setup instructions and CLAUDE.md architecture notes.

## Add DXLab Suite (DXKeeper) support

- [ ] Research DXKeeper's TCP command interface for adding QSOs (DXLab
      Suite's commander API — confirm command format, default port, and
      response handling, similar to how N3FJP's `ADDADIFRECORD` was
      reverse-engineered).
- [ ] Add a `send_to_dxkeeper()` sender in `netlogger_bridge.py`.
- [ ] Add `[dxkeeper]` section to `SAMPLE_CONFIG` / `config.ini` (enabled,
      host, port).
- [ ] Add DXKeeper fields to the GUI, following the WaveLog/N3FJP
      `LabelFrame` pattern.
- [ ] Update README.md setup instructions and CLAUDE.md architecture notes.
