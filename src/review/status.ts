// What the status bar says about the services behind a review. Pure, so it's unit-tested
// (test/unit/status.test.ts).
//
// service_status arrives piecemeal (each message carries only what its handler learned),
// and the connect-time values for speech are optimistic until first use, so a service
// that hasn't reported is "unknown", not "down".

export interface Services {
  llm?: boolean;
  stt?: boolean;
  tts?: boolean;
  llm_input_tokens?: number;
  llm_output_tokens?: number;
  act_now?: { available: boolean; detail: string; agent?: string };
}

export interface StatusRow {
  name: string;
  state: "up" | "down" | "unknown" | "off";
  note?: string;
}

export function statusRows(s: Services): StatusRow[] {
  const flag = (v: boolean | undefined): StatusRow["state"] => (v === undefined ? "unknown" : v ? "up" : "down");
  const agent = s.act_now;
  return [
    { name: "Reviewer model", state: flag(s.llm) },
    { name: "Speech-to-text", state: flag(s.stt) },
    { name: "Text-to-speech", state: flag(s.tts) },
    agent
      ? agent.available
        ? { name: "Coding agent", state: "up", note: agent.agent }
        : { name: "Coding agent", state: "off", note: agent.detail }
      : { name: "Coding agent", state: "unknown" },
  ];
}

// The status bar's short form: which services are down, if any.
export function downNames(rows: StatusRow[]): string[] {
  return rows.filter((r) => r.state === "down").map((r) => r.name.toLowerCase());
}

export function tokenLine(s: Services): string | undefined {
  if (!s.llm_input_tokens && !s.llm_output_tokens) return undefined;
  const k = (n = 0): string => (n >= 10_000 ? `${Math.round(n / 1000)}k` : n.toLocaleString("en"));
  return `${k(s.llm_input_tokens)} tokens in · ${k(s.llm_output_tokens)} out this session`;
}
