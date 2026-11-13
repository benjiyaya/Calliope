import type { QueryClient } from '@tanstack/svelte-query';

/**
 * Coalesced query invalidation for SSE bursts.
 *
 * Every `agent.tool` finish used to invalidate canvas+scenes+assets and every
 * `job.*` event 5–6 query families — a multi-tool turn rebuilt the node graph
 * over and over. This helper coalesces a burst into ONE invalidation per
 * distinct query key per window.
 */
export function createCoalescedInvalidator(
	client: QueryClient,
	delayMs = 150,
): (key: readonly unknown[]) => void {
	const pending = new Map<string, readonly unknown[]>();
	let timer: ReturnType<typeof setTimeout> | null = null;
	function flush() {
		timer = null;
		const keys = [...pending.values()];
		pending.clear();
		for (const key of keys) void client.invalidateQueries({ queryKey: [...key] });
	}
	return (key: readonly unknown[]) => {
		pending.set(JSON.stringify(key), key);
		if (timer == null) {
			timer = setTimeout(flush, delayMs);
		}
	};
}
