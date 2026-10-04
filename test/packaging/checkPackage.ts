// What the .vsix must hold for the backend to start from an installed extension. The
// packaged file list comes from .vscodeignore, which excluded backend/static once:
// server.py mounts that folder on import, so the backend died on start.
//
//   node test/packaging/checkPackage.ts
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const REQUIRED = [
  "dist/extension.js",
  "python/launch.py",
  "python/player.py",
  "backend/run.py",
  "backend/requirements.txt",
  "backend/app/server.py",
  "backend/app/config.yaml",
  "backend/static/index.html",
  "media/chat/chat.js",
  "media/chat/audio.js",
  "media/chat/codicons/codicon.ttf",
  "media/preview/reading.js",
];
const FORBIDDEN = [
  /^src\//,
  /^test\//,
  /^node_modules\//,
  /^\.venv\//,
  /^backend\/tests\//,
  /\.map$/,
  /^\.vscode-test\//,
];

const vsce = fileURLToPath(new URL("../../node_modules/@vscode/vsce/vsce", import.meta.url));
const files = execFileSync(process.execPath, [vsce, "ls"], { encoding: "utf8" })
  .split(/\r?\n/)
  .map((line) => line.trim())
  .filter(Boolean);
const missing = REQUIRED.filter((file) => !files.includes(file));
const unwanted = files.filter((file) => FORBIDDEN.some((pattern) => pattern.test(file)));
if (missing.length || unwanted.length) {
  if (missing.length) console.error(`Missing from the package:\n  ${missing.join("\n  ")}`);
  if (unwanted.length) console.error(`Shouldn't be in the package:\n  ${unwanted.join("\n  ")}`);
  process.exit(1);
}
console.log(
  `The package holds the ${REQUIRED.length} files the backend needs, and nothing it shouldn't (${files.length} files).`,
);
