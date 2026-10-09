// What the managed Python environment was last filled with. Pure, so it's unit-tested
// (test/unit/requirements.test.ts).

import { createHash } from "node:crypto";

// Installed beside the backend's requirements.txt: the mic and the audio player.
export const EXTRA_PACKAGES = ["sounddevice>=0.4,<1.0"];

// Kept in the venv, so a venv made again starts without one.
export const STAMP_FILE = "pear-requirements.sha256";

// Changes whenever a release changes what the backend needs. Line endings don't count:
// the same file checked out on Windows mustn't look like an upgrade.
export function requirementsStamp(requirementsText: string): string {
  const normalized = requirementsText.replace(/\r\n/g, "\n").trim();
  return createHash("sha256")
    .update([normalized, ...EXTRA_PACKAGES].join("\n"))
    .digest("hex");
}
