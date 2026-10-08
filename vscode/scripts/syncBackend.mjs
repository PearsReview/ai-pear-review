// Copies the backend (the repo root: app/, static/, run.py, requirements.txt) into
// backend/ so the .vsix holds what it needs to start. vsce only packs files inside
// this folder. backend/ is generated and gitignored; run.py, python/launch.py and
// the packaging check all expect this layout.
import { cpSync, mkdirSync, rmSync } from "node:fs";
import * as path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const repo = path.resolve(root, "..");
const dest = path.join(root, "backend");

rmSync(dest, { recursive: true, force: true });
mkdirSync(dest, { recursive: true });
for (const part of ["app", "static", "run.py", "requirements.txt"]) {
  cpSync(path.join(repo, part), path.join(dest, part), {
    recursive: true,
    filter: (src) => !src.includes("__pycache__"),
  });
}
