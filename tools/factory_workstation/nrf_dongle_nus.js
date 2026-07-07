/* eslint-disable no-console */
"use strict";

// Long-running nRF Dongle helper for the POC3A factory workstation.
//
// Scans, connects to a Nordic UART Service (NUS) device, enables
// notifications on the NUS TX characteristic and writes AT commands to
// the NUS RX characteristic - all through the nRF Dongle radio using
// pc-ble-driver-js. Communication with the Python host is JSON-lines
// over stdin/stdout: one JSON object per line.
//
// Commands (stdin, one JSON object per line):
//   {"cmd":"scan","filter":"AXI-P1-T","timeout":8}
//   {"cmd":"connect","address":"C8:B9:CA:AC:85:74","name":"AXI-P1-T","timeout":12}
//   {"cmd":"write","data":"AT+VER?\r\n"}
//   {"cmd":"disconnect"}
//   {"cmd":"close"}
//
// Events (stdout, one JSON object per line):
//   {"event":"log","msg":"..."}
//   {"event":"scan_result","devices":[{"name","address","rssi","source"},...]}
//   {"event":"connected","address":"..."}
//   {"event":"notify","line":"..."}            // complete NUS TX line, stripped
//   {"event":"write_ok"}
//   {"event":"disconnected"}
//   {"event":"error","msg":"..."}

const fs = require("fs");
const path = require("path");
const readline = require("readline");

const NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"; // device -> host (notify)
const NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"; // host -> device (write)
const SMP_CHAR_UUID = "da2e7828-fbce-4e01-ae9e-261174997c48";
const NRF_CONNECT_BLE_EXE_NAME = "nRF Connect for Desktop Bluetooth Low Energy.exe";
const NUS_WRITE_CHUNK_SIZE = 20;

function argValue(name, fallback) {
  const index = process.argv.indexOf(name);
  if (index >= 0 && index + 1 < process.argv.length) {
    return process.argv[index + 1];
  }
  return fallback === undefined ? "" : fallback;
}

const port = argValue("--port", process.env.AXI_BLE_DONGLE_PORT || "COM8");
const requestedSd = (argValue("--sd-version", process.env.AXI_BLE_DONGLE_SD_VERSION || "auto") || "auto").toLowerCase();
const sdVersions = requestedSd === "auto" || requestedSd === "" ? ["v5", "v2"] : [requestedSd];
const debugLogPath = argValue("--debug-log", "");

function debug(message) {
  if (debugLogPath) {
    try {
      fs.appendFileSync(debugLogPath, `[nrf-dongle-nus] ${message}\n`, "utf8");
    } catch (_err) {
      // best effort
    }
  }
}

function send(event) {
  process.stdout.write(`${JSON.stringify(event)}\n`);
}

function sendError(message) {
  send({ event: "error", msg: String(message || "") });
}

function normalizeName(value) {
  return String(value || "").trim();
}

function sameAddress(left, right) {
  return String(left || "").replace(/[:-]/g, "").toUpperCase() === String(right || "").replace(/[:-]/g, "").toUpperCase();
}

function errorMessage(error) {
  if (!error) {
    return "";
  }
  return error.description || error.message || String(error);
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function findNrfConnectBleDir() {
  const explicit = process.env.AXI_NRF_CONNECT_BLE_DIR || "";
  const candidates = [];
  if (explicit) {
    candidates.push(explicit);
  }
  if (process.env.LOCALAPPDATA) {
    candidates.push(path.join(process.env.LOCALAPPDATA, "Programs", "nrfconnect-bluetooth-low-energy"));
  }
  if (process.env.ProgramFiles) {
    candidates.push(path.join(process.env.ProgramFiles, "nrfconnect-bluetooth-low-energy"));
  }
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(path.join(candidate, "resources", "app.asar"))) {
      return candidate;
    }
  }
  return "";
}

function loadPcBleDriver() {
  const explicit = process.env.AXI_PC_BLE_DRIVER_JS || "";
  const modulePaths = [];
  if (explicit) {
    modulePaths.push(explicit);
  }
  const nrfDir = findNrfConnectBleDir();
  if (nrfDir) {
    modulePaths.push(path.join(nrfDir, "resources", "app.asar", "node_modules", "pc-ble-driver-js"));
    modulePaths.push(path.join(nrfDir, "resources", "app.asar.unpacked", "node_modules", "pc-ble-driver-js"));
  }
  modulePaths.push("pc-ble-driver-js");

  let lastError = null;
  for (const modulePath of modulePaths) {
    try {
      debug(`loading ${modulePath}`);
      return require(modulePath);
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError || new Error("pc-ble-driver-js not found");
}

function openAdapter(adapter, mode) {
  return new Promise((resolve, reject) => {
    const options = {
      baudRate: 1000000,
      parity: "none",
      flowControl: "none",
      eventInterval: 0,
      logLevel: "error",
      retransmissionInterval: 250,
      responseTimeout: 1500,
      enableBLE: mode === "open-enable-ble",
    };
    debug(`open mode=${mode}`);
    adapter.open(options, error => {
      if (error) {
        reject(new Error(errorMessage(error)));
        return;
      }
      resolve();
    });
  });
}

function enableBle(adapter) {
  return new Promise((resolve, reject) => {
    debug("enable BLE");
    adapter.enableBLE({ logLevel: "error" }, error => {
      if (error) {
        reject(new Error(errorMessage(error)));
        return;
      }
      resolve();
    });
  });
}

function closeAdapter(adapter) {
  return new Promise(resolve => {
    if (!adapter) {
      resolve();
      return;
    }
    try {
      adapter.close(() => resolve());
      setTimeout(resolve, 1000);
    } catch (_err) {
      resolve();
    }
  });
}

// Global state kept across commands.
let ble = null;
let adapter = null;
let connectedDeviceId = ""; // device.instanceId of the connected peer
let nusTxCharId = ""; // characteristic.instanceId for NUS TX (notify)
let nusRxCharId = ""; // characteristic.instanceId for NUS RX (write)
let smpCharId = ""; // MCUmgr SMP characteristic (notify + write)
let notifyBuffer = Buffer.alloc(0);
let scanning = false;
function startScan(filter, timeoutS) {
  return new Promise((resolve, reject) => {
    const seen = new Map();
    let done = false;

    function complete(error) {
      if (done) {
        return;
      }
      done = true;
      scanning = false;
      try {
        adapter.stopScan(() => {});
      } catch (_err) {
        // best effort
      }
      adapter.removeListener("deviceDiscovered", onDevice);
      if (error) {
        reject(error);
      } else {
        const devices = Array.from(seen.values()).sort((left, right) => {
          const l = left.rssi === null ? -999 : left.rssi;
          const r = right.rssi === null ? -999 : right.rssi;
          return r - l;
        });
        resolve(devices);
      }
    }

    function onDevice(device) {
      const name = normalizeName(device.name);
      const address = normalizeName(device.address);
      if (!address) {
        return;
      }
      if (filter && !name.toLowerCase().includes(filter.toLowerCase())) {
        return;
      }
      const record = {
        name: name || "(no name)",
        address,
        address_type: device.addressType || "",
        rssi: Number.isFinite(device.rssi) ? device.rssi : null,
        source: `nRF dongle ${port}`,
      };
      seen.set(address.toUpperCase(), record);
    }

    scanning = true;
    adapter.on("deviceDiscovered", onDevice);
    adapter.startScan(
      {
        active: true,
        interval: 64,
        window: 48,
        timeout: timeoutS,
        use_whitelist: false,
        adv_dir_report: false,
      },
      error => {
        if (error) {
          complete(new Error(errorMessage(error)));
        }
      },
    );
    setTimeout(() => complete(null), (timeoutS + 1) * 1000);
  });
}

function findDeviceByAddress(address, timeoutS) {
  return new Promise((resolve, reject) => {
    const lower = String(address || "").replace(/[:-]/g, "").toUpperCase();
    if (!lower) {
      reject(new Error("address is empty"));
      return;
    }
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      try { adapter.stopScan(() => {}); } catch (_err) { /* best effort */ }
      adapter.removeListener("deviceDiscovered", onDevice);
      reject(new Error(`device ${address} not found in scan`));
    }, timeoutS * 1000);

    function onDevice(device) {
      const devAddr = String(device.address || "").replace(/[:-]/g, "").toUpperCase();
      if (devAddr === lower) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        try { adapter.stopScan(() => {}); } catch (_err) { /* best effort */ }
        adapter.removeListener("deviceDiscovered", onDevice);
        resolve(device);
      }
    }

    adapter.on("deviceDiscovered", onDevice);
    adapter.startScan(
      {
        active: true,
        interval: 64,
        window: 48,
        timeout: timeoutS,
        use_whitelist: false,
        adv_dir_report: false,
      },
      error => {
        if (error && !done) {
          done = true;
          clearTimeout(timer);
          adapter.removeListener("deviceDiscovered", onDevice);
          reject(new Error(errorMessage(error)));
        }
      },
    );
  });
}

function connectDevice(device) {
  return new Promise((resolve, reject) => {
    const addressArg = {
      address: device.address,
      type: device.addressType || "BLE_GAP_ADDR_TYPE_RANDOM_STATIC",
    };
    const options = {
      scanParams: { interval: 100, window: 50, timeout: 20, active: true },
      connParams: { min_conn_interval: 10, max_conn_interval: 20, slave_latency: 0, conn_sup_timeout: 4000 },
    };
    debug(`connect ${device.address} type=${addressArg.type}`);
    let settled = false;
    function onConnected(connectedDevice) {
      if (settled) return;
      if (!connectedDevice || !sameAddress(connectedDevice.address, device.address)) {
        return;
      }
      settled = true;
      adapter.removeListener("deviceConnected", onConnected);
      resolve(connectedDevice);
    }
    adapter.on("deviceConnected", onConnected);
    adapter.connect(addressArg, options, error => {
      if (error && !settled) {
        settled = true;
        adapter.removeListener("deviceConnected", onConnected);
        reject(new Error(errorMessage(error)));
      }
    });
  });
}

function getServices(deviceId) {
  return new Promise((resolve, reject) => {
    adapter.getServices(deviceId, (error, services) => {
      if (error) {
        reject(new Error(errorMessage(error)));
        return;
      }
      resolve(services || []);
    });
  });
}

function getCharacteristics(serviceId) {
  return new Promise((resolve, reject) => {
    adapter.getCharacteristics(serviceId, (error, characteristics) => {
      if (error) {
        reject(new Error(errorMessage(error)));
        return;
      }
      resolve(characteristics || []);
    });
  });
}

function startNotifications(characteristicId) {
  return new Promise((resolve, reject) => {
    adapter.startCharacteristicsNotifications(characteristicId, true, error => {
      if (error) {
        reject(new Error(errorMessage(error)));
        return;
      }
      resolve();
    });
  });
}

function discoverDescriptors(characteristicId) {
  return new Promise((resolve, reject) => {
    adapter.getDescriptors(characteristicId, (error, descriptors) => {
      if (error) {
        reject(new Error(errorMessage(error)));
        return;
      }
      const list = descriptors || [];
      for (const d of list) {
        debug(`descriptor uuid=${d.uuid} instanceId=${d.instanceId}`);
      }
      resolve(list);
    });
  });
}

function writeCharacteristic(characteristicId, dataBuffer, withResponse) {
  return new Promise((resolve, reject) => {
    const value = Array.from(dataBuffer);
    adapter.writeCharacteristicValue(characteristicId, value, Boolean(withResponse), error => {
      if (error) {
        reject(new Error(errorMessage(error)));
        return;
      }
      resolve();
    });
  });
}

function writeDescriptorValue(descriptorId, dataBuffer) {
  return new Promise((resolve, reject) => {
    const value = Array.from(dataBuffer);
    adapter.writeDescriptorValue(descriptorId, value, true, error => {
      if (error) {
        reject(new Error(errorMessage(error)));
        return;
      }
      resolve();
    });
  });
}

function uuidMatch(left, right) {
  const a = String(left || "").replace(/-/g, "").toLowerCase();
  const b = String(right || "").replace(/-/g, "").toLowerCase();
  return a === b;
}

async function discoverNus(deviceId) {
  const services = await getServices(deviceId);
  let txId = "";
  let rxId = "";
  for (const service of services) {
    let chars = [];
    try {
      chars = await getCharacteristics(service.instanceId);
    } catch (err) {
      debug(`getCharacteristics failed for ${service.instanceId}: ${errorMessage(err)}`);
      continue;
    }
    for (const char of chars) {
      if (uuidMatch(char.uuid, NUS_TX_UUID)) {
        txId = char.instanceId;
      } else if (uuidMatch(char.uuid, NUS_RX_UUID)) {
        rxId = char.instanceId;
      }
    }
    if (txId && rxId) {
      break;
    }
  }
  if (!txId) {
    throw new Error("NUS TX characteristic (6e400003) not found");
  }
  if (!rxId) {
    throw new Error("NUS RX characteristic (6e400002) not found");
  }
  return { txId, rxId };
}

async function discoverSmp(deviceId) {
  const services = await getServices(deviceId);
  for (const service of services) {
    let chars = [];
    try {
      chars = await getCharacteristics(service.instanceId);
    } catch (err) {
      debug(`getCharacteristics failed for ${service.instanceId}: ${errorMessage(err)}`);
      continue;
    }
    for (const char of chars) {
      if (uuidMatch(char.uuid, SMP_CHAR_UUID)) {
        return char.instanceId;
      }
    }
  }
  throw new Error("SMP characteristic (da2e7828) not found");
}

async function enableCharacteristicNotifications(characteristicId) {
  await discoverDescriptors(characteristicId);
  const characteristic = adapter.getCharacteristic(characteristicId);
  let cccdId = "";
  if (characteristic) {
    const cccd = adapter._getCCCDOfCharacteristic(characteristic);
    if (cccd) cccdId = cccd.instanceId;
  }
  if (!cccdId) {
    for (const key in adapter._descriptors) {
      const d = adapter._descriptors[key];
      if (d && uuidMatch(d.uuid, "2902") && d.characteristicInstanceId === characteristicId) {
        cccdId = d.instanceId;
        break;
      }
    }
  }
  if (cccdId) {
    debug(`writing CCCD ${cccdId} to enable notifications`);
    await writeDescriptorValue(cccdId, Buffer.from([0x01, 0x00]));
    debug(`CCCD write ok`);
  } else {
    debug(`CCCD not found, falling back to startCharacteristicsNotifications`);
    await startNotifications(characteristicId);
  }
}

function emitNotifyLines() {
  let idx;
  while ((idx = notifyBuffer.indexOf(0x0a)) >= 0) {
    const line = notifyBuffer.slice(0, idx).toString("utf8").replace(/\r$/, "").trim();
    notifyBuffer = notifyBuffer.slice(idx + 1);
    if (line) {
      send({ event: "notify", line });
    }
  }
}

function onCharacteristicValueChanged(characteristic) {
  debug(`characteristicValueChanged id=${characteristic && characteristic.instanceId} nusTx=${nusTxCharId} smp=${smpCharId}`);
  if (!characteristic) {
    return;
  }
  const value = characteristic.value;
  if (!value || !value.length) {
    return;
  }
  if (smpCharId && characteristic.instanceId === smpCharId) {
    send({ event: "smp_notify", data: Buffer.from(value).toString("base64") });
    return;
  }
  if (!nusTxCharId || characteristic.instanceId !== nusTxCharId) {
    return;
  }
  notifyBuffer = Buffer.concat([notifyBuffer, Buffer.from(value)]);
  emitNotifyLines();
}

async function openAnyAdapter() {
  const errors = [];
  // open-enable-ble mode is required for connect to work; open-then-enable
  // leaves the SoftDevice conn_cfg in a state that rejects gapConnect with
  // NRF_ERROR_INVALID_PARAM.
  for (const sdVersion of sdVersions) {
    for (const mode of ["open-enable-ble", "open-then-enable"]) {
      try {
        debug(`attempt open sd=${sdVersion} mode=${mode}`);
        const candidate = ble.AdapterFactory.getInstance(undefined, { enablePolling: false }).createAdapter(
          sdVersion,
          port,
          `poc3a-nus-${process.pid}-${port}-${sdVersion}-${mode}`,
        );
        await openAdapter(candidate, mode);
        if (mode === "open-then-enable") {
          await enableBle(candidate);
        }
        adapter = candidate;
        adapter.on("error", error => {
          debug(`adapter error: ${errorMessage(error)}`);
        });
        adapter.on("characteristicValueChanged", onCharacteristicValueChanged);
        adapter.on("deviceDisconnected", device => {
          if (device && device.instanceId === connectedDeviceId) {
            connectedDeviceId = "";
            nusTxCharId = "";
            nusRxCharId = "";
            smpCharId = "";
            notifyBuffer = Buffer.alloc(0);
            send({ event: "disconnected" });
          }
        });
        debug(`adapter ready sd=${sdVersion} mode=${mode}`);
        return;
      } catch (err) {
        const message = `${sdVersion}/${mode}: ${errorMessage(err)}`;
        debug(message);
        errors.push(message);
      }
    }
  }
  throw new Error(`adapter open failed: ${errors.join("; ")}`);
}

async function handleScan(cmd) {
  const filter = normalizeName(cmd.filter || "");
  const timeoutS = Math.max(1, Number(cmd.timeout || 8) || 8);
  if (scanning) {
    sendError("scan already in progress");
    return;
  }
  try {
    const devices = await startScan(filter, timeoutS);
    send({ event: "scan_result", devices });
  } catch (err) {
    sendError(`scan failed: ${errorMessage(err)}`);
  }
}

async function handleConnect(cmd) {
  const address = normalizeName(cmd.address || "");
  const timeoutS = Math.max(1, Number(cmd.timeout || 12) || 12);
  if (!address) {
    sendError("connect requires address");
    return;
  }
  if (connectedDeviceId) {
    if (sameAddress(address, connectedDeviceId)) {
      send({ event: "connected", address });
      return;
    }
    try {
      await disconnectDevice();
    } catch (_err) {
      // best effort
    }
  }
  try {
    send({ event: "log", msg: `scanning for ${address}` });
    const device = await findDeviceByAddress(address, timeoutS);
    send({ event: "log", msg: `found ${device.address}, connecting` });
    const connected = await connectDevice(device);
    connectedDeviceId = connected.instanceId;
    send({ event: "log", msg: "connected, discovering NUS" });
    const { txId, rxId } = await discoverNus(connected.instanceId);
    nusTxCharId = txId;
    nusRxCharId = rxId;
    smpCharId = "";
    debug(`NUS discovered tx=${txId} rx=${rxId}`);
    await enableCharacteristicNotifications(txId);
    debug(`notifications enabled on tx`);
    notifyBuffer = Buffer.alloc(0);
    send({ event: "connected", address: connected.address || device.address || address });
  } catch (err) {
    connectedDeviceId = "";
    nusTxCharId = "";
    nusRxCharId = "";
    smpCharId = "";
    sendError(`connect failed: ${errorMessage(err)}`);
  }
}

async function handleWrite(cmd) {
  const data = String(cmd.data || "");
  if (!connectedDeviceId || !nusRxCharId) {
    sendError("not connected");
    return;
  }
  try {
    const payload = Buffer.from(data, "utf8");
    debug(`writing ${payload.length} bytes to rx=${nusRxCharId}: ${JSON.stringify(data)}`);
    for (let offset = 0; offset < payload.length; offset += NUS_WRITE_CHUNK_SIZE) {
      const chunk = payload.slice(offset, offset + NUS_WRITE_CHUNK_SIZE);
      debug(`write chunk offset=${offset} len=${chunk.length}`);
      await writeCharacteristic(nusRxCharId, chunk, false);
      if (offset + NUS_WRITE_CHUNK_SIZE < payload.length) {
        await delay(12);
      }
    }
    debug(`write ok`);
    send({ event: "write_ok" });
  } catch (err) {
    sendError(`write failed: ${errorMessage(err)}`);
  }
}

async function handleConnectSmp(cmd) {
  const address = normalizeName(cmd.address || "");
  const timeoutS = Math.max(1, Number(cmd.timeout || 12) || 12);
  if (!address) {
    sendError("connect_smp requires address");
    return;
  }
  if (connectedDeviceId) {
    try {
      await disconnectDevice();
    } catch (_err) {
      // best effort
    }
  }
  try {
    send({ event: "log", msg: `scanning for ${address}` });
    const device = await findDeviceByAddress(address, timeoutS);
    send({ event: "log", msg: `found ${device.address}, connecting` });
    const connected = await connectDevice(device);
    connectedDeviceId = connected.instanceId;
    send({ event: "log", msg: "connected, discovering SMP" });
    smpCharId = await discoverSmp(connected.instanceId);
    nusTxCharId = "";
    nusRxCharId = "";
    notifyBuffer = Buffer.alloc(0);
    debug(`SMP discovered char=${smpCharId}`);
    await enableCharacteristicNotifications(smpCharId);
    debug(`notifications enabled on smp`);
    send({ event: "connected", address: connected.address || device.address || address });
  } catch (err) {
    connectedDeviceId = "";
    nusTxCharId = "";
    nusRxCharId = "";
    smpCharId = "";
    sendError(`connect_smp failed: ${errorMessage(err)}`);
  }
}

async function handleWriteSmp(cmd) {
  if (!connectedDeviceId || !smpCharId) {
    sendError("not connected");
    return;
  }
  try {
    const payload = Buffer.from(String(cmd.data || ""), "base64");
    debug(`writing ${payload.length} bytes to smp=${smpCharId}`);
    await writeCharacteristic(smpCharId, payload, payload.length > 20);
    send({ event: "write_ok" });
  } catch (err) {
    sendError(`write_smp failed: ${errorMessage(err)}`);
  }
}

function disconnectDevice() {
  return new Promise((resolve, reject) => {
    if (!connectedDeviceId) {
      resolve();
      return;
    }
    const deviceId = connectedDeviceId;
    adapter.disconnect(deviceId, error => {
      connectedDeviceId = "";
      nusTxCharId = "";
      nusRxCharId = "";
      smpCharId = "";
      notifyBuffer = Buffer.alloc(0);
      if (error) {
        reject(new Error(errorMessage(error)));
        return;
      }
      resolve();
    });
  });
}

async function handleDisconnect() {
  try {
    await disconnectDevice();
    send({ event: "disconnected" });
  } catch (err) {
    sendError(`disconnect failed: ${errorMessage(err)}`);
  }
}

async function handleClose() {
  try {
    await disconnectDevice();
  } catch (_err) {
    // best effort
  }
  try {
    await closeAdapter(adapter);
  } catch (_err) {
    // best effort
  }
  send({ event: "log", msg: "closed" });
  process.exit(0);
}

async function dispatch(cmd) {
  switch (cmd.cmd) {
    case "scan":
      await handleScan(cmd);
      break;
    case "connect":
      await handleConnect(cmd);
      break;
    case "connect_smp":
      await handleConnectSmp(cmd);
      break;
    case "write":
      await handleWrite(cmd);
      break;
    case "write_smp":
      await handleWriteSmp(cmd);
      break;
    case "disconnect":
      await handleDisconnect();
      break;
    case "close":
      await handleClose();
      break;
    default:
      sendError(`unknown command: ${cmd.cmd || "(empty)"}`);
  }
}

async function main() {
  debug(`start port=${port} sd=${sdVersions.join("/")}`);
  ble = loadPcBleDriver();
  await openAnyAdapter();
  send({ event: "log", msg: `adapter ready on ${port}` });

  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  rl.on("line", line => {
    if (!line.trim()) {
      return;
    }
    let cmd;
    try {
      cmd = JSON.parse(line);
    } catch (err) {
      sendError(`invalid command json: ${errorMessage(err)}`);
      return;
    }
    dispatch(cmd).catch(err => {
      sendError(`command error: ${errorMessage(err)}`);
    });
  });
  rl.on("close", async () => {
    await handleClose();
  });
}

process.on("uncaughtException", err => {
  sendError(`uncaught: ${errorMessage(err)}`);
});
process.on("unhandledRejection", err => {
  sendError(`unhandledRejection: ${errorMessage(err)}`);
});

main().catch(err => {
  sendError(`init failed: ${errorMessage(err)}`);
  setTimeout(() => process.exit(1), 200);
});
