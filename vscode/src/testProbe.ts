// Read-only views of UI state for the integration tests (test/integration/). Modules
// publish a getter under a name; the extension hands this map to the tests from
// activate() only when PEAR_REVIEW_TEST=1, so nothing outside tests can reach it.
//
// A probe reports what a module already holds. It never changes behaviour, and a
// test that needs to act goes through commands or the chat's message handler, as a
// user would.

const probes = new Map<string, () => unknown>();

export function publish(name: string, read: () => unknown): void {
  probes.set(name, read);
}

export function read(name: string): unknown {
  const probe = probes.get(name);
  if (!probe) throw new Error(`No test probe named "${name}".`);
  return probe();
}

export const testMode = process.env.PEAR_REVIEW_TEST === "1";
