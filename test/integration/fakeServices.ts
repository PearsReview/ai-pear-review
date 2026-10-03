// One local HTTP server standing in for everything the backend calls out to in the
// integration tests: Ollama (/api/tags, /api/show, /api/chat) and the speech service
// (/transcribe, /speech). Instant and deterministic, and it records every request so
// tests can check what reached the model (GET /__requests).
//
// Tests script it over HTTP: POST /__script {"replies": [...], "transcript": "..."}
// queues the next model replies and sets what speech-to-text hears.
import { createServer, type IncomingMessage, type Server } from "node:http";

export const FAKE_MODEL = "fake-model";
export const DEFAULT_REPLY = "This change looks fine. It calls `add` once, and the result is returned.";
export const DEFAULT_TRANSCRIPT = "What does this change do?";

interface Recorded {
  path: string;
  body: unknown;
}

interface Script {
  replies: string[];
  transcript: string;
}

// A JSON schema → the smallest instance that satisfies it, for requests that ask for
// structured output (the briefing uses Ollama's `format`).
export function instanceOf(schema: unknown): unknown {
  if (typeof schema !== "object" || schema === null) return null;
  const s = schema as Record<string, unknown>;
  if (Array.isArray(s.enum)) return s.enum[0];
  for (const key of ["anyOf", "oneOf"]) {
    const options = s[key];
    if (Array.isArray(options) && options.length) return instanceOf(options[0]);
  }
  const type: unknown = Array.isArray(s.type) ? (s.type as unknown[])[0] : s.type;
  switch (type) {
    case "object": {
      const properties = (s.properties ?? {}) as Record<string, unknown>;
      return Object.fromEntries(Object.entries(properties).map(([k, v]) => [k, instanceOf(v)]));
    }
    case "array":
      return [];
    case "string":
      return "fake";
    case "integer":
    case "number":
      return 0;
    case "boolean":
      return false;
    default:
      return null;
  }
}

// A 0.25 s silent 16-bit mono WAV: what /speech returns.
function silentWav(): Buffer {
  const rate = 22_050;
  const samples = Math.round(rate * 0.25);
  const data = samples * 2;
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + data, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(1, 22); // mono
  header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(data, 40);
  return Buffer.concat([header, Buffer.alloc(data)]);
}

async function readBody(req: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks);
}

export async function startFakeServices(): Promise<{ url: string; close: () => Promise<void> }> {
  const requests: Recorded[] = [];
  const script: Script = { replies: [], transcript: DEFAULT_TRANSCRIPT };

  const server: Server = createServer((req, res) => {
    void (async () => {
      const raw = await readBody(req);
      const path = (req.url ?? "/").split("?")[0] ?? "/";
      const json = (status: number, value: unknown): void => {
        res.writeHead(status, { "content-type": "application/json" });
        res.end(JSON.stringify(value));
      };
      const parsed = (): Record<string, unknown> => {
        try {
          return JSON.parse(raw.toString("utf8")) as Record<string, unknown>;
        } catch {
          return {};
        }
      };

      // --- test control ---------------------------------------------------------
      if (path === "/__requests") return json(200, requests);
      if (path === "/__reset") {
        requests.length = 0;
        script.replies = [];
        script.transcript = DEFAULT_TRANSCRIPT;
        return json(200, { ok: true });
      }
      if (path === "/__script") {
        const body = parsed();
        if (Array.isArray(body.replies)) script.replies.push(...body.replies.map(String));
        if (typeof body.transcript === "string") script.transcript = body.transcript;
        return json(200, { ok: true });
      }

      // --- Ollama ---------------------------------------------------------------
      if (path === "/api/tags") return json(200, { models: [{ name: FAKE_MODEL, model: FAKE_MODEL }] });
      if (path === "/api/show") {
        return json(200, {
          capabilities: ["completion"],
          model_info: { "fake.context_length": 32_768 },
          details: { parameter_size: "7B", family: "fake" },
        });
      }
      if (path === "/api/chat") {
        const body = parsed();
        requests.push({ path, body });
        const text = body.format ? JSON.stringify(instanceOf(body.format)) : (script.replies.shift() ?? DEFAULT_REPLY);
        res.writeHead(200, { "content-type": "application/x-ndjson" });
        // Two content chunks, then the closing chunk with usage counts, as Ollama streams.
        const half = Math.ceil(text.length / 2);
        for (const part of [text.slice(0, half), text.slice(half)]) {
          res.write(JSON.stringify({ message: { role: "assistant", content: part }, done: false }) + "\n");
        }
        res.end(
          JSON.stringify({
            message: { role: "assistant", content: "" },
            done: true,
            prompt_eval_count: 100,
            eval_count: 20,
          }) + "\n",
        );
        return;
      }

      // --- speech -----------------------------------------------------------------
      if (path === "/health") return json(200, { status: "ok" });
      if (path === "/transcribe") {
        requests.push({ path, body: { bytes: raw.length, wav: raw.includes(Buffer.from("WAVE")) } });
        return json(200, { text: script.transcript });
      }
      if (path === "/speech") {
        requests.push({ path, body: parsed() });
        res.writeHead(200, { "content-type": "audio/wav" });
        res.end(silentWav());
        return;
      }

      json(404, { error: `fake services: no route for ${path}` });
    })();
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  return {
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
