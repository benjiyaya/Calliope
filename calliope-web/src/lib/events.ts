export type CalliopeEvent = {
	type: string;
	data: Record<string, unknown>;
	ts: string;
};

/**
 * SSE connection lifecycle:
 * - `connecting`   — first attach attempt in flight
 * - `open`         — stream established (EventSource.onopen fired)
 * - `reconnecting` — stream dropped; the 3s re-attach loop is running
 */
export type EventConnectionState = 'connecting' | 'open' | 'reconnecting';

type Listener = (event: CalliopeEvent) => void;
type StateListener = (state: EventConnectionState) => void;

export function connectEvents(onEvent: Listener, onState?: StateListener): () => void {
	let es: EventSource | null = null;
	let closed = false;
	let firstAttach = true;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

	const attach = () => {
		if (closed) return;
		onState?.(firstAttach ? 'connecting' : 'reconnecting');
		firstAttach = false;
		es = new EventSource('/api/events');
		es.onopen = () => onState?.('open');
		const handler = (e: MessageEvent) => {
			try {
				const parsed = JSON.parse(e.data) as CalliopeEvent;
				onEvent(parsed);
			} catch {
				/* ignore */
			}
		};
		// Listen to named events + default message
		[
			'agent.thinking',
			'job.created',
			'job.started',
			'job.progress',
			'job.completed',
			'job.failed',
			'job.deleted',
			'asset.ready',
			'story.ready',
		].forEach((name) => es!.addEventListener(name, handler as EventListener));
		es.onmessage = handler;
		es.onerror = () => {
			onState?.('reconnecting');
			es?.close();
			if (!closed) {
				reconnectTimer = setTimeout(attach, 3000);
			}
		};
	};

	attach();

	return () => {
		closed = true;
		if (reconnectTimer) clearTimeout(reconnectTimer);
		es?.close();
	};
}
