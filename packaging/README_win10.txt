Axi Factory Workstation portable package

Run:
  Axi Factory Workstation.exe

Files:
  config.json        Portable workstation settings. Relative paths are resolved from this folder.
  .env.template      Token/password template. Rename to .env if you want to preconfigure engineering credentials.
  factory_records/   Created automatically when SN/record mode is enabled.

Notes:
  - No engineering token or password is included in this package.
  - UART defaults to COM18 @ 460800. Change it in the GUI settings or config.json on the target PC.
  - For temporary bring-up, keep SN/record disabled. For production records, enable SN/record in the GUI.
  - BLE target name defaults to AXI-P1-T.
