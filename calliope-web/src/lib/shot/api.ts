/** Typed fetch helpers for the /api/shots surface. */

export interface Capture {
	id: number;
	composition_id: number;
	kind: 'image' | 'video';
	label: string | null;
	file_path: string | null;
	meta_json?: string | null;
	meta?: Record<string, unknown> | null;
	created_at: string;
}

export interface GateRecordResult {
	ok: boolean;
	gate: 'brief' | 'cut';
	gates: {
		brief?: { approved_at: string; source?: string; note?: string | null } | null;
		cut?: { approved_at: string; source?: string; note?: string | null } | null;
	};
}

export const shotApi = {
	async listCaptures(compositionId: number): Promise<Capture[]> {
		const resp = await fetch(`/api/shots/${compositionId}/captures`);
		if (!resp.ok) return [];
		return resp.json();
	},

	async uploadCapture(compositionId: number, dataUrl: string, label: string): Promise<Capture | null> {
		const resp = await fetch(`/api/shots/${compositionId}/captures`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ data_url: dataUrl, label }),
		});
		if (!resp.ok) return null;
		return resp.json();
	},

	async deleteCapture(compositionId: number, captureId: number): Promise<boolean> {
		const resp = await fetch(`/api/shots/${compositionId}/captures/${captureId}`, {
			method: 'DELETE',
		});
		return resp.ok;
	},

	async setCaptureRequest(compositionId: number, label: string): Promise<void> {
		await fetch(`/api/shots/${compositionId}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ capture_request_json: JSON.stringify({ label, source: 'ui' }) }),
		});
	},

	/** Every composition (any session) — used by project pickers to collect captures. */
	async listCompositions(): Promise<Array<{ id: number; agent_session_id: number | null; title: string }>> {
		const resp = await fetch('/api/shots');
		if (!resp.ok) return [];
		return resp.json();
	},

	async deleteComposition(compositionId: number): Promise<boolean> {
		const resp = await fetch(`/api/shots/${compositionId}`, { method: 'DELETE' });
		return resp.ok;
	},

	/** Record Brief/Cut gate from the UI (source: 'ui'). Human at keyboard — no ask_user. */
	async recordGate(
		compositionId: number,
		gate: 'brief' | 'cut',
		opts: { note?: string; source?: string } = {},
	): Promise<GateRecordResult> {
		const resp = await fetch(`/api/shots/${compositionId}/gates`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				gate,
				note: opts.note ?? null,
				source: opts.source ?? 'ui',
			}),
		});
		if (!resp.ok) {
			let detail = `Gate record failed (${resp.status})`;
			try {
				const body = await resp.json();
				detail =
					typeof body?.detail === 'string'
						? body.detail
						: body?.detail?.message || body?.status || detail;
			} catch {
				/* ignore */
			}
			throw new Error(detail);
		}
		return resp.json();
	},

};
