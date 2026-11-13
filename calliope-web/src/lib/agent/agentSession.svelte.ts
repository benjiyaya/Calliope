import { useQueryClient } from '@tanstack/svelte-query';
import type {
	AgentMessage,
	AgentPlan,
	AgentSession,
	AgentTask,
} from '$lib/api';
import type { CalliopeEvent } from '$lib/events';

export interface LiveTool {
	tool: string;
	args?: Record<string, unknown> | null;
	result?: unknown;
	phase: 'running' | 'done' | 'error';
}

export interface AgentSessionRuntimeHooks {
	/** Query key of the sessions LIST this page renders ('agent-sessions' | ['agent-sessions', 'scene']). */
	sessionsKey(): readonly unknown[];
	/** The session the chat is attached to, or null while loading. */
	activeId(): number | null;
	/** Page-specific handling of a finished tool: invalidations, shot-store syncs. */
	onToolFinish?(name: string, result: unknown): void;
	/** Called from resetLiveViews so pages clear their own extras (composer draft, nonce). */
	onResetLive?(): void;
}

/**
 * The agent chat's live view: streaming buffers, live tool cards, the plan
 * panel, and the SSE handling for the shared `agent.*` event family.
 *
 * Both AI Canvas and Build Scene host the same loop — this class is the one
 * implementation (the pages previously duplicated ~100 lines of wiring each).
 * Page-specific events stay in the page; everything agent.* lands here via
 * `handleAgentEvent`, which returns false for events it does not consume.
 */
export class AgentSessionRuntime {
	streaming = $state('');
	streamingReasoning = $state('');
	// Tracks which agent produced the last reasoning delta so the name prefix
	// is written once per transition, not per token.
	thinkingAgent: string | null = null;
	liveTools = $state<LiveTool[]>([]);
	livePlan = $state<AgentPlan | null>(null);

	#client = useQueryClient();
	#hooks: AgentSessionRuntimeHooks;

	constructor(hooks: AgentSessionRuntimeHooks) {
		this.#hooks = hooks;
	}

	/** Clear the live views only — no page extras. Used when steering (the
	 * composer keeps its draft while a run is in flight). */
	resetStream() {
		this.streaming = '';
		this.streamingReasoning = '';
		this.thinkingAgent = null;
		this.liveTools = [];
		this.livePlan = null;
	}

	resetLiveViews() {
		this.resetStream();
		this.#hooks.onResetLive?.();
	}

	upsertSession(s: AgentSession) {
		this.#client.setQueryData<AgentSession[]>(this.#hooks.sessionsKey(), (old) => {
			const list = Array.isArray(old) ? old : [];
			return list.some((x) => x.id === s.id)
				? list.map((x) => (x.id === s.id ? { ...x, ...s } : x))
				: [...list, s];
		});
	}

	appendMessage(sid: number, msg: AgentMessage) {
		if (sid !== this.#hooks.activeId()) return;
		this.#client.setQueryData<AgentSession & { messages: AgentMessage[] }>(
			['agent-session', sid],
			(old) => {
				if (!old || !Array.isArray(old.messages)) return old;
				if (old.messages.some((m) => m.id === msg.id)) return old;
				return { ...old, messages: [...old.messages, msg] };
			},
		);
	}

	/** Handle the shared `agent.*` event family. Returns false when unconsumed. */
	handleAgentEvent(ev: CalliopeEvent): boolean {
		const activeId = this.#hooks.activeId();
		switch (ev.type) {
			case 'agent.session.updated': {
				const s = ev.data?.session as AgentSession | undefined;
				if (s) this.upsertSession(s);
				return true;
			}
			case 'agent.message': {
				const msg = ev.data?.message as AgentMessage | undefined;
				if (msg) {
					this.appendMessage(msg.session_id, msg);
					if (msg.session_id === activeId) {
						if (msg.role === 'tool') this.liveTools = [];
						if (msg.role === 'assistant' && !msg.agent_name) {
							this.streaming = '';
							this.streamingReasoning = '';
							this.thinkingAgent = null;
						}
					}
				}
				this.#client.invalidateQueries({ queryKey: this.#hooks.sessionsKey() });
				return true;
			}
			case 'agent.token':
				if (ev.data?.session_id === activeId && !ev.data?.agent_name) {
					this.streaming += String(ev.data?.content ?? '');
				}
				return true;
			case 'agent.thinking': {
				// Reasoning is auxiliary: stream it from ANY agent (incl.
				// sub-agents) — a strict `!agent_name` filter would leave
				// delegated turns as a bare "working..." card. Prefix only on
				// agent change; deltas arrive per token.
				if (ev.data?.session_id === activeId) {
					const name = (ev.data?.agent_name as string | undefined) ?? null;
					if (name !== this.thinkingAgent) {
						this.streamingReasoning += name ? `${name}: ` : '';
						this.thinkingAgent = name;
					}
					// Sub-agents stream their reasoning — keep only the tail so a
					// whole swarm's thinking can't grow without bound.
					this.streamingReasoning = (
						this.streamingReasoning + String(ev.data?.content ?? '')
					).slice(-24000);
				}
				return true;
			}
			case 'agent.tool': {
				if (ev.data?.session_id !== activeId) return true;
				const name = String(ev.data?.tool ?? '');
				const phase = String(ev.data?.phase ?? '');
				if (phase === 'start') {
					this.liveTools.push({
						tool: name,
						args: (ev.data?.args as Record<string, unknown>) ?? null,
						phase: 'running',
					});
				} else if (phase === 'finish') {
					const idx = [...this.liveTools]
						.reverse()
						.findIndex((t) => t.tool === name && t.phase === 'running');
					if (idx >= 0) {
						const real = this.liveTools.length - 1 - idx;
						this.liveTools[real] = {
							...this.liveTools[real],
							result: ev.data?.result,
							phase:
								(ev.data?.result as { ok?: boolean } | undefined)?.ok === false
									? 'error'
									: 'done',
						};
					}
					this.#hooks.onToolFinish?.(name, ev.data?.result);
				}
				return true;
			}
			case 'agent.plan': {
				if (ev.data?.session_id !== activeId) return true;
				const tasks = (ev.data?.tasks as AgentTask[] | undefined) ?? [];
				this.livePlan = {
					tasks: tasks.map((t) => ({ ...t, status: t.status ?? 'pending' })),
					note: (ev.data?.note as string | null) ?? null,
				};
				return true;
			}
			case 'agent.task': {
				if (ev.data?.session_id !== activeId) return true;
				const index = ev.data?.index;
				const status = ev.data?.status as AgentTask['status'] | undefined;
				if (typeof index === 'number' && status) {
					this.livePlan = this.livePlan
						? {
								...this.livePlan,
								tasks: this.livePlan.tasks.map((t, i) =>
									i === index ? { ...t, status } : t,
								),
							}
						: this.livePlan;
				}
				return true;
			}
			default:
				return false;
		}
	}
}
