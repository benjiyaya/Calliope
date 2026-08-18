export type ToastKind = 'success' | 'error' | 'info';

export type Toast = {
	id: number;
	message: string;
	kind: ToastKind;
	/** Auto-dismiss delay in ms. 0 = sticky until dismissed. */
	duration: number;
};

type Listener = (toasts: Toast[]) => void;

const DEFAULT_DURATION = 3200;

let seq = 0;
let toasts: Toast[] = [];
const listeners = new Set<Listener>();

type TimerState = {
	remaining: number;
	startedAt: number;
	handle: ReturnType<typeof setTimeout>;
};

const timers = new Map<number, TimerState>();

function emit() {
	const snapshot = [...toasts];
	for (const listener of listeners) listener(snapshot);
}

export function subscribeToasts(listener: Listener): () => void {
	listeners.add(listener);
	listener([...toasts]);
	return () => listeners.delete(listener);
}

export function dismissToast(id: number) {
	const timer = timers.get(id);
	if (timer) {
		clearTimeout(timer.handle);
		timers.delete(id);
	}
	toasts = toasts.filter((t) => t.id !== id);
	emit();
}

function schedule(id: number, delay: number) {
	timers.set(id, {
		remaining: delay,
		startedAt: Date.now(),
		handle: setTimeout(() => dismissToast(id), delay),
	});
}

export function showToast(
	message: string,
	kind: ToastKind = 'success',
	durationMs: number = DEFAULT_DURATION,
) {
	const id = ++seq;
	toasts = [...toasts, { id, message, kind, duration: durationMs }];
	emit();
	if (durationMs > 0) schedule(id, durationMs);
	return id;
}

/** Pause auto-dismiss (e.g. while the pointer or focus is on the toast). */
export function pauseToast(id: number) {
	const timer = timers.get(id);
	if (!timer) return;
	clearTimeout(timer.handle);
	timer.remaining = Math.max(0, timer.remaining - (Date.now() - timer.startedAt));
	timer.startedAt = Date.now();
}

/** Resume auto-dismiss after a pause. */
export function resumeToast(id: number) {
	const timer = timers.get(id);
	if (!timer) return;
	clearTimeout(timer.handle);
	const delay = Math.max(0, timer.remaining);
	timer.remaining = delay;
	timer.startedAt = Date.now();
	timer.handle = setTimeout(() => dismissToast(id), delay);
}

export const toast = {
	success: (message: string, duration?: number) => showToast(message, 'success', duration),
	error: (message: string, duration?: number) => showToast(message, 'error', duration),
	info: (message: string, duration?: number) => showToast(message, 'info', duration),
};
