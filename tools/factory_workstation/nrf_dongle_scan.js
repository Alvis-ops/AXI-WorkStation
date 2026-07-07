/* eslint-disable no-console */
"use strict";

const fs = require("fs");
const path = require("path");

function argValue(name, fallback = "") {
  const index = process.argv.indexOf(name);
  if (index >= 0 && index + 1 < process.argv.length) {
    return process.argv[index + 1];
  }
  return fallback;
}

function hasArg(name) {
  return process.argv.includes(name);
}

const debugLogPath = argValue("--debug-log", process.env.AXI_NRF_DONGLE_SCAN_LOG || "");

function normalizeName(value) {
  return String(value || "").trim();
}

function debug(message) {
  if (!hasArg("--debug")) {
    return;
  }
  const line = `[nrf-dongle-scan] ${message}\n`;
  if (debugLogPath) {
    try {
      fs.appendFileSync(debugLogPath, line, "utf8");
    } catch (_err) {
      // Best effort only.
    }
  }
  try {
    process.stderr.write(line);
  } catch (_err) {
    // Best effort only.
  }
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

function errorMessage(error) {
  if (!error) {
    return "";
  }
  return error.description || error.message || String(error);
}

const port = argValue("--port", process.env.AXI_BLE_DONGLE_PORT || "COM8");
const filter = normalizeName(argValue("--filter", process.env.AXI_BLE_NAME_FILTER || "AXI-P1-T"));
const timeoutS = Math.max(1, Number(argValue("--timeout", "8")) || 8);
const outPath = argValue("--out", "");
const requestedSd = normalizeName(argValue("--sd-version", process.env.AXI_BLE_DONGLE_SD_VERSION || "auto")).toLowerCase();
const sdVersions = requestedSd === "auto" || requestedSd === "" ? ["v5", "v2"] : [requestedSd];

const seen = new Map();
let lastAdapter = null;

function rememberDevice(device) {
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
  const key = address.toUpperCase();
  const previous = seen.get(key);
  if (!previous || (record.rssi !== null && (previous.rssi === null || record.rssi > previous.rssi))) {
    seen.set(key, record);
  }
  debug(`device ${record.name} ${record.address} rssi=${record.rssi}`);
}

function sortedDevices() {
  return Array.from(seen.values()).sort((left, right) => {
    const leftRssi = left.rssi === null ? -999 : left.rssi;
    const rightRssi = right.rssi === null ? -999 : right.rssi;
    if (rightRssi !== leftRssi) {
      return rightRssi - leftRssi;
    }
    return `${left.name} ${left.address}`.localeCompare(`${right.name} ${right.address}`);
  });
}

function emitResult(result) {
  const text = `${JSON.stringify(result, null, 2)}\n`;
  if (outPath) {
    fs.writeFileSync(outPath, text, "utf8");
    debug(`wrote ${outPath}`);
    return;
  }
  process.stdout.write(text);
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

function scanWithAdapter(adapter) {
  return new Promise((resolve, reject) => {
    let done = false;
    const timer = setTimeout(() => complete(null), (timeoutS + 1) * 1000);

    function complete(error) {
      if (done) {
        return;
      }
      done = true;
      clearTimeout(timer);
      if (error) {
        reject(error);
      } else {
        resolve();
      }
    }

    adapter.on("deviceDiscovered", rememberDevice);
    adapter.on("scanTimedOut", () => {
      debug("scan timed out");
      complete(null);
    });
    adapter.on("error", error => {
      debug(`adapter error: ${errorMessage(error)}`);
    });

    debug("start scan");
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
  });
}

async function attemptScan(ble, sdVersion, mode) {
  const factory = ble.AdapterFactory.getInstance(undefined, { enablePolling: false });
  const adapter = factory.createAdapter(sdVersion, port, `poc3a-${process.pid}-${port}-${sdVersion}-${mode}`);
  lastAdapter = adapter;
  try {
    await openAdapter(adapter, mode);
    if (mode === "open-then-enable") {
      await enableBle(adapter);
    }
    await scanWithAdapter(adapter);
    await closeAdapter(adapter);
    lastAdapter = null;
    return;
  } catch (err) {
    await closeAdapter(adapter);
    lastAdapter = null;
    throw err;
  }
}

async function main() {
  debug(`start port=${port} filter=${filter} timeout=${timeoutS}s sd=${sdVersions.join("/")}`);
  const ble = loadPcBleDriver();
  const errors = [];
  for (const sdVersion of sdVersions) {
    for (const mode of ["open-then-enable", "open-enable-ble"]) {
      try {
        debug(`attempt sd=${sdVersion} mode=${mode}`);
        await attemptScan(ble, sdVersion, mode);
        emitResult({ ok: true, port, filter, devices: sortedDevices() });
        return 0;
      } catch (err) {
        const message = `${sdVersion}/${mode}: ${errorMessage(err)}`;
        debug(message);
        errors.push(message);
      }
    }
  }
  emitResult({ ok: false, port, filter, devices: sortedDevices(), error: errors.join("; ") });
  return 2;
}

const hardExit = setTimeout(async () => {
  try {
    await closeAdapter(lastAdapter);
  } finally {
    emitResult({ ok: false, port, filter, devices: sortedDevices(), error: "scan helper hard timeout" });
    process.exit(4);
  }
}, (timeoutS + 15) * 1000);

main()
  .then(code => {
    clearTimeout(hardExit);
    process.exit(code);
  })
  .catch(async err => {
    clearTimeout(hardExit);
    try {
      await closeAdapter(lastAdapter);
    } finally {
      emitResult({ ok: false, port, filter, devices: sortedDevices(), error: errorMessage(err) });
      process.exit(1);
    }
  });
