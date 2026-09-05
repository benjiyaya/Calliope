<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { toStore } from 'svelte/store';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import {
		SvelteFlow,
		Controls,
		MiniMap,
		Background,
		BackgroundVariant,
		Panel,
		type Node,
		type Viewport,
	} from '@xyflow/svelte';
	import '@xyflow/svelte/dist/style.css';
	import '$lib/canvas/tokens.css';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import AgentChat from '$lib/components/agent/AgentChat.svelte';
	import AgentComposer from '$lib/components/agent/AgentComposer.svelte';
	import AgentSessionSidebar from '$lib/components/agent/AgentSessionSidebar.svelte';
	import AgentPlanPanel from '$lib/components/agent/AgentPlanPanel.svelte';
	import ImageLightbox from '$lib/components/ImageLightbox.svelte';
	import EntityNode from '$lib/canvas/EntityNode.svelte';
	import ArtifactNodeComp from '$lib/canvas/ArtifactNode.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import {
		agentApi,
		assetUrl,
		canvasApi,
		jobsApi,
		playgroundApi,
		projects,
		workflows,
		type AgentMessage,
		type AgentPlan,
		type AgentSession,
		type AgentTask,
		type Character,
		type Item,
		type Job,
		type Location,
		type Scene,
	} from '$lib/api';
	import type { AgentComposerPayload, SkillOption, WorkflowOption } from '$lib/agentComposer';
	import { connectEvents } from '$lib/events';
	import { toast } from '$lib/toast';

	const client = useQueryClient();

	const canvasId = Number(page.params.id);
	if (!Number.isFinite(canvasId)) {
		throw new Error('Invalid canvas id');
	}

	const RAIL_KEY = 'calliope.canvas.railCollapsed';
	const CHAT_KEY = 'calliope.canvas.chatCollapsed';
	const SESSION_KEY = 'calliope.agents.activeSession';

	// ---- canvas graph ----------------------------------------------------

	const canvasQuery = createQuery({
		queryKey: ['canvas', canvasId],
		queryFn: () => canvasApi.get(canvasId),
	});

	const canvas = $derived($canvasQuery.data?.canvas ?? null);
	const canvasNodes = $derived($canvasQuery.data?.nodes ?? []);
	const projectId = $derived(canvas?.project_id ?? null);

	const assetsQuery = createQuery(
		toStore(() => ({
			queryKey: ['canvas-assets', projectId ?? 'none'],
			queryFn: () => projects.getAssets(projectId!),
			enabled: projectId != null,
		})),
	);

	const scenesQuery = createQuery(
		toStore(() => ({
			queryKey: ['canvas-scenes', projectId ?? 'none'],
			queryFn: () => projects.getScenes(projectId!),
			enabled: projectId != null,
		})),
	);

	function mediaFor(
		node: (typeof canvasNodes)[number],
		characters: Character[],
		locations: Location[],
		items: Item[],
		scenes: Scene[],
	): { imagePath: string | null; videoPath: string | null } {
		if (node.entity_type === 'character') {
			const c = characters.find((x) => x.id === node.entity_id);
			return { imagePath: c?.sheet_path ?? c?.portrait_path ?? null, videoPath: null };
		}
		if (node.entity_type === 'location') {
			const l = locations.find((x) => x.id === node.entity_id);
			return { imagePath: l?.reference_image_path ?? null, videoPath: null };
		}
		if (node.entity_type === 'item') {
			const i = items.find((x) => x.id === node.entity_id);
			return { imagePath: i?.reference_image_path ?? null, videoPath: null };
		}
		if (node.entity_type === 'scene') {
			const s = scenes.find((x) => x.id === node.entity_id);
			// Project > Videos pattern: env image poster, clip fallback; only
			// real video files count (guard mirrors QueueStage.previewPath).
			const clip =
				s?.video_path && /\.(mp4|webm)$/i.test(s.video_path) ? s.video_path : null;
			return { imagePath: s?.env_image_path ?? null, videoPath: clip };
		}
		return { imagePath: null, videoPath: null };
	}

	const nodeTypes = {
		entity: EntityNode,
		image: ArtifactNodeComp,
		video: ArtifactNodeComp,
	};

	// $state.raw: Svelte Flow v1 treats node arrays as immutable — it mutates
	// internally, so proxies must never reach it.
	let nodes = $state.raw<Node[]>([]);

	// ---- media preview (lightbox) ------------------------------------------
	// Card clicks open the large preview here; nothing navigates away.
	let preview = $state<{ src: string; kind: 'image' | 'video'; caption: string } | null>(
		null,
	);

	function openMediaPreview(kind: 'image' | 'video', path: string | null, caption: string) {
		const src = assetUrl(path);
		if (!src) return;
		preview = { src, kind, caption };
	}

	// ---- graph sync ----------------------------------------------------------

	let syncVersion = $state(0);
	let loadedGraphAt = $state(0);
	// While a drag is active the persisted-graph effect must NOT rebuild: any
	// SSE-driven refetch (job.started, canvas.updated from another tab, …)
	// would replace `nodes` with stale DB positions mid-drag and snap the
	// dragged cards back. The rebuild is deferred until drag stop.
	let dragInProgress = $state(false);
	let rebuildPending = $state(false);
	$effect(() => {
		// Rebuild the Svelte Flow graph whenever the persisted graph or any
		// media source changes; drag positions win until the next refetch.
		void syncVersion;
		if (dragInProgress) {
			rebuildPending = true;
			return;
		}
		rebuildPending = false;
		const graph = $canvasQuery.data;
		if (!graph) {
			loadedGraphAt = 0;
			nodes = [];
			return;
		}
		loadedGraphAt = Date.now();
		const characters = $assetsQuery.data?.characters ?? [];
		const locations = $assetsQuery.data?.locations ?? [];
		const items = $assetsQuery.data?.items ?? [];
		const scenes = $scenesQuery.data?.scenes ?? [];
		const jobs = $jobsQuery.data ?? [];
		// An artifact node is "running" while its tracked job is queued/running.
		const nodeRunning = new Map<number, boolean>();
		for (const n of graph.nodes) {
			const tracked = n.job_id != null ? jobs.find((j) => j.id === n.job_id) : undefined;
			nodeRunning.set(
				n.id,
				tracked ? tracked.status === 'running' || tracked.status === 'pending' : false,
			);
		}
		nodes = graph.nodes
			.filter((n) => n.type !== 'workflow')
			.map((n) => {
				const media = mediaFor(n, characters, locations, items, scenes);
				if (n.type === 'image' || n.type === 'video') {
					return {
						id: `cn-${n.id}`,
						type: n.type,
						position: { x: n.x, y: n.y },
						data: {
							canvasNodeId: n.id,
							title: n.title ?? 'Artifact',
							artifactPath: n.artifact_path,
							kind: n.type,
							status: nodeRunning.get(n.id) ? 'running' : n.status,
							onOpenMedia: (kind: 'image' | 'video') =>
								openMediaPreview(kind, n.artifact_path, n.title ?? 'Artifact'),
						},
					};
				}
				return {
					id: `cn-${n.id}`,
					type: 'entity' as const,
					position: { x: n.x, y: n.y },
					data: {
						canvasNodeId: n.id,
						entityType: n.entity_type as 'character' | 'location' | 'item' | 'scene',
						title: n.title ?? 'Untitled',
						imagePath: media.imagePath,
						videoPath: media.videoPath,
						onOpenMedia: (kind: 'image' | 'video') =>
							kind === 'video'
								? openMediaPreview('video', media.videoPath, n.title ?? 'Untitled')
								: openMediaPreview('image', media.imagePath, n.title ?? 'Untitled'),
					},
				};
			});
	});

	async function persistNodePosition(node: Node) {
		const canvasNodeId = (node.data as { canvasNodeId?: number }).canvasNodeId;
		if (canvasNodeId == null) return;
		try {
			await canvasApi.patchNode(canvasId, canvasNodeId, {
				x: node.position.x,
				y: node.position.y,
			});
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not save position');
		}
	}

	/** Persist every dragged node. Group drags (shift/box-select + drag) carry
	 * the full selection in `nodes` with targetNode=null; persisting only the
	 * last node silently dropped the rest of the group's new positions. */
	async function persistNodePositions(moved: Node[]) {
		await Promise.allSettled(moved.map((n) => persistNodePosition(n)));
	}

	function onNodeDragStart() {
		dragInProgress = true;
	}

	function onNodeDragStop(e: {
		targetNode: Node | null;
		nodes?: Node[];
	}) {
		dragInProgress = false;
		// Group drags: targetNode is null, `nodes` is the whole selection.
		// Single drags: nodes[0] is the dragged node. Persist EVERYTHING —
		// persisting only the last node dropped the rest of a group's moves.
		const moved = e.nodes?.length ? e.nodes : e.targetNode ? [e.targetNode] : [];
		if (moved.length > 0) void persistNodePositions(moved);
		// A refetch landed during the drag: rebuild now that positions are
		// settled (and re-ordered by the just-saved values on the next fetch).
		if (rebuildPending) {
			void client.refetchQueries({ queryKey: ['canvas', canvasId] });
			rebuildPending = false;
		}
	}

	// ---- viewport persistence --------------------------------------------
	// Zoom/pan must survive reload, session switches (the {#key} layout
	// remounts this page on canvas id change), and navigation away/back.
	// fitView would fight the restore on every mount, so it's gone: the
	// bound viewport's mount-time value is the initial viewport, and oninit
	// re-asserts it once Svelte Flow's pan/zoom exists.

	let viewportTimeout: ReturnType<typeof setTimeout> | undefined;
	let initialViewport = $state<Viewport | undefined>(undefined);
	let viewport = $state<Viewport>({ x: 0, y: 0, zoom: 1 });

	function validViewport(v: unknown): v is Viewport {
		const p = v as Viewport | null;
		return (
			!!p &&
			typeof p.x === 'number' &&
			Number.isFinite(p.x) &&
			typeof p.y === 'number' &&
			Number.isFinite(p.y) &&
			typeof p.zoom === 'number' &&
			Number.isFinite(p.zoom) &&
			p.zoom > 0
		);
	}

	function viewportLocalStorageKey(id: number) {
		return `calliope.canvas.viewport.${id}`;
	}

	function restoreViewportFromStorage(): Viewport | null {
		if (typeof localStorage === 'undefined') return null;
		try {
			const raw = localStorage.getItem(viewportLocalStorageKey(canvasId));
			return raw ? (JSON.parse(raw) as Viewport) : null;
		} catch {
			return null;
		}
	}

	function scheduleViewportSave(v: Viewport) {
		viewport = v;
		clearTimeout(viewportTimeout);
		viewportTimeout = setTimeout(() => {
			const json = JSON.stringify(v);
			// localStorage first (synchronous, survives navigation + reloads
			// even if the request never lands), backend per canvas second.
			try {
				localStorage.setItem(viewportLocalStorageKey(canvasId), json);
			} catch {
				// storage full/blocked — backend save still applies
			}
			void canvasApi
				.patchCanvas(canvasId, { viewport_json: json })
				.catch(() => undefined);
		}, 500);
	}

	// Initial value must be set before Svelte Flow mounts: backend first
	// (authoritative), localStorage mirror as fallback for canvases whose
	// viewport was never saved (or fetched after mount).
	// The `$state`-read warnings are intentional: this runs once at
	// component init, before any subscription exists.
	// svelte-ignore state_referenced_locally
	if (initialViewport === undefined) {
		let parsed: Viewport | null = null;
		const storedJson = $canvasQuery.data?.canvas.viewport_json;
		if (storedJson) {
			try {
				parsed = JSON.parse(storedJson) as Viewport;
			} catch {
				parsed = null;
			}
		}
		const local = restoreViewportFromStorage();
		const chosen = validViewport(parsed) ? parsed : (local ?? undefined);
		// svelte-ignore state_referenced_locally
		initialViewport = chosen;
		if (chosen) viewport = chosen;
	}

	let initAsserted = false;
	function assertViewportOnInit() {
		// Race guard: a fetch that lands after mount can re-bind a stale
		// viewport — re-assert the chosen initial value exactly once.
		if (initAsserted) return;
		initAsserted = true;
		if (initialViewport) viewport = initialViewport;
	}

	// ---- sessions + chat (mirrors agents/+page.svelte) --------------------

	let activeId = $state<number | null>(null);
	let streaming = $state('');
	let streamingReasoning = $state('');
	let liveTools = $state<
		{ tool: string; args?: Record<string, unknown> | null; result?: unknown; phase: 'running' | 'done' | 'error' }[]
	>([]);
	let livePlan = $state<AgentPlan | null>(null);
	let composerDraft = $state('');
	let composerNonce = $state(0);
	let sessionScopeHandled = $state(false);

	// Live updates arrive via SSE (agent.session.updated / agent.message);
	// refreshes also fire at user trigger points (send, create, delete) and
	// on events.resync after a stream reconnect. No polling.
	const sessionsQuery = createQuery({
		queryKey: ['agent-sessions'],
		queryFn: () => agentApi.listSessions(),
	});

	const sessions = $derived($sessionsQuery.data ?? []);
	const activeSession = $derived(
		activeId != null ? (sessions.find((s) => s.id === activeId) ?? null) : null,
	);
	const running = $derived(Boolean(activeSession?.running || activeSession?.status === 'running'));

	const sessionQuery = createQuery(
		toStore(() => ({
			queryKey: ['agent-session', activeId],
			queryFn: () => agentApi.getSession(activeId!),
			enabled: activeId != null,
		})),
	);
	const messages = $derived($sessionQuery.data?.messages ?? []);
	const plan = $derived(livePlan ?? ($sessionQuery.data?.plan ?? null));

	const jobsQuery = createQuery(
		toStore(() => ({
			queryKey: ['agent-project-jobs', projectId ?? 'sandbox'],
			queryFn: () => (projectId != null ? jobsApi.list(projectId) : playgroundApi.jobs()),
			enabled: activeId != null,
		})),
	);
	const jobs = $derived($jobsQuery.data ?? []);

	const workflowsQuery = createQuery({
		queryKey: ['workflows'],
		queryFn: workflows.list,
	});
	const enabledWorkflows = $derived<WorkflowOption[]>(
		($workflowsQuery.data ?? [])
			.filter((w) => w.is_enabled)
			.map((w) => ({
				id: w.id,
				name: w.name,
				kind: w.kind,
				description: w.description,
				is_enabled: w.is_enabled,
			})),
	);

	const skillsQuery = createQuery({
		queryKey: ['agent-skills'],
		queryFn: agentApi.listSkills,
	});
	const skillOptions = $derived<SkillOption[]>($skillsQuery.data ?? []);

	// Initial session: ?session= param → stored → scope-matching default.
	$effect(() => {
		if (sessionScopeHandled || sessions.length === 0 || canvas == null) return;
		sessionScopeHandled = true;
		const paramSession = page.url.searchParams.get('session');
		const pid = Number(paramSession);
		if (paramSession != null && Number.isFinite(pid)) {
			activeId = sessions.some((s) => s.id === pid) ? pid : null;
			// Deep-link handoff (/agents?project=&task=) pre-fills the composer once.
			const prefill = sessionStorage.getItem('calliope.canvas.composerPrefill');
			if (prefill) {
				sessionStorage.removeItem('calliope.canvas.composerPrefill');
				setComposerDraft(prefill);
			}
			replaceUrlParam('session');
			return;
		}
		const stored = Number(localStorage.getItem(SESSION_KEY));
		if (Number.isFinite(stored) && sessions.some((s) => s.id === stored)) {
			const s = sessions.find((x) => x.id === stored)!;
			if ((s.project_id ?? null) === (canvas.project_id ?? null)) {
				activeId = s.id;
				return;
			}
		}
		// Scope-matched default: project canvas → newest project session;
		// sandbox canvas → newest sandbox session (or none).
		if (canvas.project_id != null) {
			const s = sessions.find((x) => x.project_id === canvas.project_id);
			activeId = s?.id ?? null;
		} else {
			const s = sessions.find((x) => x.project_id == null);
			activeId = s?.id ?? null;
		}
	});

	function replaceUrlParam(key: string) {
		const url = new URL(page.url);
		url.searchParams.delete(key);
		goto(url.pathname + (url.search || ''), {
			replaceState: true,
			keepFocus: true,
			noScroll: true,
		});
	}

	// Selecting a session from another scope navigates to that scope's canvas.
	async function selectSession(id: number) {
		const s = sessions.find((x) => x.id === id);
		if (!s || !canvas) return;
		const sameScope =
			(s.project_id ?? null) === (canvas.project_id ?? null) ||
			(canvas.project_id != null && s.project_id === canvas.project_id);
		if (sameScope) {
			activeId = id;
			resetLiveViews();
			return;
		}
		try {
			const graph = s.project_id
				? await canvasApi.ensureForProject(s.project_id)
				: await canvasApi.ensureForSession(s.id);
			goto(`/canvas/${graph.canvas.id}?session=${s.id}`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not open that canvas');
		}
	}

	function resetLiveViews() {
		streaming = '';
		streamingReasoning = '';
		liveTools = [];
		livePlan = null;
		composerDraft = '';
		composerNonce = 0;
	}

	function setComposerDraft(text: string) {
		composerDraft = text;
		composerNonce++;
	}

	function seedEmptySession(s: AgentSession) {
		client.setQueryData(['agent-session', s.id], { ...s, messages: [], plan: null });
	}

	async function newSandbox() {
		resetLiveViews();
		try {
			const s = await agentApi.createSession({});
			seedEmptySession(s);
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			const graph = await canvasApi.ensureForSession(s.id);
			goto(`/canvas/${graph.canvas.id}?session=${s.id}`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not create session');
		}
	}

	async function newProjectChat(pid: number) {
		resetLiveViews();
		try {
			const s = await agentApi.createSession({ project_id: pid });
			seedEmptySession(s);
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			if (canvas?.project_id === pid) {
				activeId = s.id;
			} else {
				const graph = await canvasApi.ensureForProject(pid);
				goto(`/canvas/${graph.canvas.id}?session=${s.id}`);
			}
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not create session');
		}
	}

	async function removeSession(id: number) {
		if (!confirm('Delete this chat and its message history?')) return;
		try {
			await agentApi.deleteSession(id);
			if (activeId === id) activeId = null;
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			toast.success('Session deleted');
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not delete session');
		}
	}

	async function unlinkActiveSession() {
		if (!activeSession || canvas?.project_id == null || running) return;
		const name = canvas.project?.title ?? 'this project';
		if (
			!confirm(
				`Unlink this chat from "${name}"? The chat keeps its history and becomes a Sandbox chat — the project itself is untouched.`,
			)
		)
			return;
		const sid = activeSession.id;
		try {
			await agentApi.patchSession(sid, { unlink: true });
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			// Follow the chat to its sandbox canvas (created on demand).
			const graph = await canvasApi.ensureForSession(sid);
			goto(`/canvas/${graph.canvas.id}?session=${sid}`);
			toast.success('Chat unlinked — moved to Sandbox');
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Unlink failed');
		}
	}

	const sendMutation = createMutation({
		mutationFn: (payload: AgentComposerPayload) => agentApi.postMessage(activeId!, payload),
		onMutate: () => {
			streaming = '';
			streamingReasoning = '';
			liveTools = [];
			livePlan = null;
			if (activeId != null) {
				client.setQueryData<AgentSession & { messages: AgentMessage[]; plan?: AgentPlan | null }>(
					['agent-session', activeId],
					(old) => (old ? { ...old, plan: null } : old),
				);
			}
		},
		onSuccess: async () => {
			await client.invalidateQueries({ queryKey: ['agent-session', activeId] });
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : 'Failed to send');
		},
	});

	async function cancelRun() {
		if (activeId == null) return;
		try {
			await agentApi.cancel(activeId);
			// The cancel endpoint now settles the session (status idle +
			// agent.session.updated), but don't rely on SSE timing: refetch
			// so the composer leaves its running state immediately.
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			await client.invalidateQueries({ queryKey: ['agent-session', activeId] });
			toast.info('Run cancelled');
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Cancel failed');
		}
	}

	async function handleSend(payload: AgentComposerPayload) {
		if (activeId != null) {
			$sendMutation.mutate(payload);
			return;
		}
		try {
			const s = await agentApi.createSession(canvas?.project_id ? { project_id: canvas.project_id } : {});
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			activeId = s.id;
			$sendMutation.mutate(payload);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not create session');
		}
	}

	/** Question-card click: the option text is the reply, stamped with the
	 * question seq so the backend records a structured approval. */
	function handleQuestionAnswer(option: string, _scope: string, questionSeq: number) {
		if (running || activeId == null) return;
		handleSend({ content: option, mentions: [], attachments: [], answer_to: questionSeq });
	}

	function upsertSession(s: AgentSession) {
		client.setQueryData<AgentSession[]>(['agent-sessions'], (old) => {
			const list = Array.isArray(old) ? old : [];
			return list.some((x) => x.id === s.id)
				? list.map((x) => (x.id === s.id ? { ...x, ...s } : x))
				: [...list, s];
		});
	}

	function appendMessage(sid: number, msg: AgentMessage) {
		if (sid !== activeId) return;
		client.setQueryData<AgentSession & { messages: AgentMessage[] }>(
			['agent-session', sid],
			(old) => {
				if (!old || !Array.isArray(old.messages)) return old;
				if (old.messages.some((m) => m.id === msg.id)) return old;
				return { ...old, messages: [...old.messages, msg] };
			},
		);
	}

	onMount(() => {
		const stop = connectEvents((ev) => {
			if (ev.type === 'events.resync') {
				// SSE stream just reconnected: refill anything missed while down.
				client.invalidateQueries({ queryKey: ['agent-sessions'] });
				if (activeId != null) {
					client.invalidateQueries({ queryKey: ['agent-session', activeId] });
					client.invalidateQueries({ queryKey: ['agent-project-jobs'] });
					client.invalidateQueries({ queryKey: ['playground-jobs'] });
				}
				client.invalidateQueries({ queryKey: ['canvas', canvasId] });
				return;
			} else if (ev.type === 'canvas.updated') {
				if ((ev.data?.canvas_id as number | undefined) === canvasId) {
					client.invalidateQueries({ queryKey: ['canvas', canvasId] });
				}
			} else if (ev.type === 'agent.session.updated') {
				const s = ev.data?.session as AgentSession | undefined;
				if (s) upsertSession(s);
			} else if (ev.type === 'agent.message') {
				const msg = ev.data?.message as AgentMessage | undefined;
				if (msg) {
					appendMessage(msg.session_id, msg);
					if (msg.session_id === activeId) {
						if (msg.role === 'tool') liveTools = [];
						if (msg.role === 'assistant' && !msg.agent_name) {
							streaming = '';
							streamingReasoning = '';
						}
					}
				}
				client.invalidateQueries({ queryKey: ['agent-sessions'] });
			} else if (ev.type === 'agent.token') {
				if (ev.data?.session_id === activeId && !ev.data?.agent_name) {
					streaming += String(ev.data?.content ?? '');
				}
			} else if (ev.type === 'agent.thinking') {
				if (ev.data?.session_id === activeId && !ev.data?.agent_name) {
					streamingReasoning += String(ev.data?.content ?? '');
				}
			} else if (ev.type === 'agent.tool') {
				if (ev.data?.session_id !== activeId) return;
				const name = String(ev.data?.tool ?? '');
				const phase = String(ev.data?.phase ?? '');
				if (phase === 'start') {
					liveTools.push({
						tool: name,
						args: (ev.data?.args as Record<string, unknown>) ?? null,
						phase: 'running',
					});
				} else if (phase === 'finish') {
					const idx = [...liveTools]
						.reverse()
						.findIndex((t) => t.tool === name && t.phase === 'running');
					if (idx >= 0) {
						const real = liveTools.length - 1 - idx;
						liveTools[real] = {
							...liveTools[real],
							result: ev.data?.result,
							phase:
								(ev.data?.result as { ok?: boolean } | undefined)?.ok === false
									? 'error'
									: 'done',
						};
					}
				}
			} else if (ev.type === 'agent.plan') {
				if (ev.data?.session_id !== activeId) return;
				const tasks = (ev.data?.tasks as AgentTask[] | undefined) ?? [];
				livePlan = {
					tasks: tasks.map((t) => ({ ...t, status: t.status ?? 'pending' })),
					note: (ev.data?.note as string | null) ?? null,
				};
			} else if (ev.type === 'agent.task') {
				if (ev.data?.session_id !== activeId) return;
				const index = ev.data?.index;
				const status = ev.data?.status as AgentTask['status'] | undefined;
				if (typeof index === 'number' && status) {
					livePlan = livePlan
						? {
								...livePlan,
								tasks: livePlan.tasks.map((t, i) =>
									i === index ? { ...t, status } : t,
								),
							}
						: livePlan;
				}
			} else if (
				ev.type === 'story.ready' ||
				ev.type === 'job.created' ||
				ev.type === 'job.started' ||
				ev.type === 'job.completed' ||
				ev.type === 'job.failed'
			) {
				client.invalidateQueries({ queryKey: ['agent-sessions'] });
				client.invalidateQueries({ queryKey: ['agent-project-jobs'] });
				client.invalidateQueries({ queryKey: ['playground-jobs'] });
				// New story entities need seeding; finished jobs refresh media.
				client.invalidateQueries({ queryKey: ['canvas', canvasId] });
				client.invalidateQueries({ queryKey: ['canvas-assets'] });
				client.invalidateQueries({ queryKey: ['canvas-scenes'] });
				// Preview board: a finished job materializes as an artifact card
				// (skipped when this page owns no session — the sandbox scratch
				// is a shared project, so its job.completed events fan out to
				// every open canvas and a mismatched project_id must not post).
				if (ev.type === 'job.completed') {
					void materializeJobArtifact(ev.data);
				}
			}
		});
		return stop;
	});

	// ---- artifacts from job events -------------------------------------------

	async function materializeJobArtifact(data: Record<string, unknown> | undefined) {
		const jobId = Number(data?.job_id ?? NaN);
		if (!Number.isFinite(jobId)) return;
		// Scope: project canvases take their own project's jobs; sandbox
		// canvases take jobs the agent enqueued (source:"agent" stamped by
		// the worker on the event — no race with the jobs poll). Other
		// sandbox tabs' scratch jobs cannot cross-post.
		const evPid = data?.project_id == null ? null : Number(data.project_id);
		if (canvas?.project_id != null) {
			if (evPid !== canvas.project_id) return;
		} else {
			if (!activeSession) return;
			if (data?.source !== 'agent') return;
		}
		try {
			const outputs = (data?.outputs as string[] | undefined) ?? [];
			const outPath = outputs.find((p) => p && p.trim()) ?? null;
			if (!outPath) return;
			// Idempotent: the artifact card is keyed by job id, so repeated
			// SSE deliveries or a refetch race can't duplicate cards.
			const graph = await canvasApi.get(canvasId);
			const existing = graph.nodes.find(
				(n) => (n.type === 'image' || n.type === 'video') && n.job_id === jobId,
			);
			if (existing) {
				if (!existing.artifact_path) {
					await canvasApi.patchNode(canvasId, existing.id, {
						artifact_path: outPath,
						status: 'done',
					});
					await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
				} else if (
					existing.title === `Job ${jobId} output` ||
					/^Job \d+ output$/.test(existing.title ?? '')
				) {
					// Legacy generic title → upgrade to the prompt snippet so
					// older cards stay comparable with newly created ones.
					const prompt = String(data?.prompt ?? '').trim();
					if (prompt) {
						const label = `${prompt.slice(0, 48)}${prompt.length > 48 ? '…' : ''} · #${jobId}`;
						await canvasApi.patchNode(canvasId, existing.id, { title: label });
						await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
					}
				}
				return;
			}
			const isVideo = /\.(mp4|webm)$/i.test(outPath);
			const kind = isVideo ? 'video' : 'image';
			// Title carries the prompt snippet so users can visually compare
			// multiple generations for the same scene/workflow and re-apply
			// the one they prefer (job history stays on the canvas).
			const prompt = String(data?.prompt ?? '').trim();
			const label = prompt
				? `${prompt.slice(0, 48)}${prompt.length > 48 ? '…' : ''} · #${jobId}`
				: `Job ${jobId} output`;
			// Backend picks the gallery slot when x/y are omitted — one layout
			// rule shared with the agent's post_artifact_to_canvas tool.
			await canvasApi.createNode(canvasId, {
				type: kind,
				title: label,
				artifact_path: outPath,
				job_id: jobId,
				status: 'done',
			});
			await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
		} catch {
			// background refresh failure is non-fatal; polling also corrects status
		}
	}

	// ---- collapsible panels ------------------------------------------------

	let tidying = $state(false);

	async function tidyCanvas() {
		if (tidying) return;
		tidying = true;
		try {
			const res = await canvasApi.tidy(canvasId);
			await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
			toast.success(`Tidied ${res.moved} cards into columns`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Tidy failed');
		} finally {
			tidying = false;
		}
	}

	let railCollapsed = $state(
		typeof localStorage !== 'undefined' && localStorage.getItem(RAIL_KEY) === '1',
	);
	let chatCollapsed = $state(
		typeof localStorage !== 'undefined' && localStorage.getItem(CHAT_KEY) === '1',
	);

	function toggleRail() {
		railCollapsed = !railCollapsed;
		localStorage.setItem(RAIL_KEY, railCollapsed ? '1' : '0');
	}
	function toggleChat() {
		chatCollapsed = !chatCollapsed;
		localStorage.setItem(CHAT_KEY, chatCollapsed ? '1' : '0');
	}

	// ---- draggable chat splitter ---------------------------------------------

	const CHAT_W_KEY = 'calliope.canvas.chatWidth';
	const CHAT_MIN_PX = 300;

	// Stored in px; capped against the live window so a resized window can
	// never restore an oversize panel (max 30% of window width).
	let chatWidth = $state(420);
	let dragging = $state(false);

	function clampChatWidth(px: number): number {
		const max = Math.max(CHAT_MIN_PX, Math.round(window.innerWidth * 0.3));
		return Math.min(Math.max(px, CHAT_MIN_PX), max);
	}

	$effect(() => {
		const raw = Number(localStorage.getItem(CHAT_W_KEY));
		if (Number.isFinite(raw) && raw > 0) chatWidth = clampChatWidth(raw);
		// Keep the panel within the cap when the window shrinks.
		const onResize = () => (chatWidth = clampChatWidth(chatWidth));
		window.addEventListener('resize', onResize);
		return () => window.removeEventListener('resize', onResize);
	});

	function startDrag(e: MouseEvent) {
		if (e.button !== 0) return;
		e.preventDefault();
		dragging = true;
		document.body.style.userSelect = 'none';
		document.body.style.cursor = 'col-resize';
		const startX = e.clientX;
		const startWidth = chatWidth;
		const onMove = (ev: MouseEvent) => {
			chatWidth = clampChatWidth(startWidth + (startX - ev.clientX));
		};
		const onUp = () => {
			dragging = false;
			document.body.style.userSelect = '';
			document.body.style.cursor = '';
			localStorage.setItem(CHAT_W_KEY, String(Math.round(chatWidth)));
			document.removeEventListener('mousemove', onMove);
			document.removeEventListener('mouseup', onUp);
		};
		document.addEventListener('mousemove', onMove);
		document.addEventListener('mouseup', onUp);
	}

	// ---- title card ---------------------------------------------------------

	let titleDraft = $state('');
	let titleEditing = $state(false);
	let savedFlash = $state(false);

	$effect(() => {
		if (!titleEditing) titleDraft = canvas?.title ?? '';
	});

	async function saveTitle() {
		const next = titleDraft.trim();
		titleEditing = false;
		if (!canvas || !next || next === canvas.title) return;
		try {
			await canvasApi.patchCanvas(canvasId, { title: next });
			await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
			savedFlash = true;
			setTimeout(() => (savedFlash = false), 1400);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Rename failed');
		}
	}

	const SUGGESTIONS: { label: string; prompt: string }[] = [
		{
			label: 'Create a new film',
			prompt: 'Create a new film project and guide me through drafting the story.',
		},
		{
			label: 'Draft a storyline',
			prompt: 'Draft a storyline — beats, characters, environments, and misc. items.',
		},
	];
</script>

<div class="canvas-root shell">
	<AppHeader active="canvas" />

	{#if $canvasQuery.isError}
		<div class="load-error" role="alert">
			Could not load this canvas
			{#if $canvasQuery.error instanceof Error}
				— {$canvasQuery.error.message}
			{/if}
		</div>
	{:else if canvas}
		<div class="workspace">
			<AgentSessionSidebar
				{sessions}
				{activeId}
				onSelect={selectSession}
				onNewSandbox={newSandbox}
				onNewProjectChat={newProjectChat}
				onDelete={removeSession}
				collapsed={railCollapsed}
				onToggleCollapse={toggleRail}
			/>

			<main class="flow-panel">
				<div class="titlecard" data-testid="titlecard">
					{#if titleEditing}
						<input
							class="title-input"
							bind:value={titleDraft}
							onblur={saveTitle}
							onkeydown={(e) => {
								if (e.key === 'Enter') saveTitle();
								if (e.key === 'Escape') titleEditing = false;
							}}
						/>
					{:else}
						<button
							type="button"
							class="title-btn"
							onclick={() => (titleEditing = true)}
							title="Rename canvas"
						>
							{canvas.title}
						</button>
					{/if}
				{#if savedFlash}
					<span class="saved">saved</span>
				{/if}
				{#if canvas.project}
					<a class="project-chip" href="/project/{canvas.project.id}">
						<Icon name="folder" size={12} />
						{canvas.project.title}
					</a>
					<button
						type="button"
						class="unlink-btn"
						onclick={unlinkActiveSession}
						disabled={!activeSession || running}
						title={running
							? 'Wait for the run to finish'
							: activeSession
								? `Unlink this chat from ${canvas.project.title}`
								: 'No chat on this canvas yet'}
						aria-label={`Unlink this chat from ${canvas.project.title}`}
					>
						<Icon name="link-off" size={12} />
						Unlink
					</button>
				{:else}
					<span class="sandbox-chip">
						<Icon name="sparkle" size={12} />
						Sandbox
					</span>
				{/if}
				</div>

				<SvelteFlow
					bind:nodes
					bind:viewport
					{nodeTypes}
					colorMode="dark"
					minZoom={0.05}
					maxZoom={4}
					{initialViewport}
					// Never-saved canvas: fit all cards once nodes are measured.
					// fitView only queues when it's set at store creation, so the
					// prop is evaluated once here — later changes are ignored (the
					// store keeps fitViewQueued latched from the first run).
					fitView={initialViewport === undefined}
					onnodedragstart={onNodeDragStart}
					onnodedragstop={onNodeDragStop}
					onmoveend={(_event, vp) => scheduleViewportSave(vp)}
					oninit={assertViewportOnInit}
				>
					<Background variant={BackgroundVariant.Dots} gap={26} size={1} />
					<Controls />
					<MiniMap />
					<Panel position="bottom-center">
						<button
							type="button"
							class="tidy-btn"
							disabled={tidying || nodes.length === 0}
							onclick={tidyCanvas}
							title="Re-arrange all cards into clean columns and grids"
						>
							<Icon name="drag" size={14} />
							Tidy layout
						</button>
					</Panel>
					{#if nodes.length === 0 && !$canvasQuery.isLoading}
						<Panel position="top-center">
							<div class="canvas-empty-hint">
								<strong>Empty board</strong>
								<span>
									Ask the agent on the right to create characters, scenes, or to
									generate an image or video — outputs land here as cards.
								</span>
							</div>
						</Panel>
					{/if}
				</SvelteFlow>
			</main>

			{#if !chatCollapsed}
				<button
					type="button"
					class="chat-splitter"
					class:dragging
					aria-label={`Resize chat panel (drag or arrow keys), currently ${Math.round(chatWidth)} pixels`}
					onmousedown={startDrag}
					onkeydown={(e) => {
						if (e.key === 'ArrowLeft') {
							e.preventDefault();
							chatWidth = clampChatWidth(chatWidth + 24);
							localStorage.setItem(CHAT_W_KEY, String(Math.round(chatWidth)));
						} else if (e.key === 'ArrowRight') {
							e.preventDefault();
							chatWidth = clampChatWidth(chatWidth - 24);
							localStorage.setItem(CHAT_W_KEY, String(Math.round(chatWidth)));
						}
					}}
				></button>
				<aside class="chat-panel" style:width="{chatWidth}px">
					<header class="chat-head">
						<div class="chat-title-block">
							{#if activeSession}
								<span class="chat-title">{activeSession.title}</span>
							{:else}
								<span class="chat-title muted">No chat selected</span>
							{/if}
						</div>
						<button
							type="button"
							class="chat-toggle"
							onclick={toggleChat}
							title="Collapse chat"
							aria-label="Collapse chat"
						>
							<Icon name="chevron-right" size={14} />
						</button>
					</header>
					<AgentChat
						{messages}
						{liveTools}
						{streaming}
						{streamingReasoning}
						{jobs}
						{running}
						loading={activeId != null && $sessionQuery.isLoading}
						suggestions={SUGGESTIONS}
						onSuggestion={setComposerDraft}
						onAnswer={handleQuestionAnswer}
					/>
					{#if running && plan && plan.tasks.length > 0}
						<AgentPlanPanel {plan} />
					{/if}
					{#key activeId}
					<AgentComposer
						{running}
						draft={composerDraft}
						draftNonce={composerNonce}
						workflows={enabledWorkflows}
						skills={skillOptions}
						onSend={handleSend}
						onCancel={cancelRun}
					/>
					{/key}
				</aside>
			{:else}
				<button
					type="button"
					class="chat-expand"
					onclick={toggleChat}
					title="Expand chat"
					aria-label="Expand chat"
				>
					<Icon name="chevron-left" size={14} />
				</button>
			{/if}
		</div>
	{/if}

	{#if preview}
		<ImageLightbox
			src={preview.src}
			kind={preview.kind}
			alt={preview.caption}
			caption={preview.caption}
			onClose={() => (preview = null)}
		/>
	{/if}
</div>

<style>
	.shell {
		display: flex;
		flex-direction: column;
		height: 100vh;
		overflow: hidden;
	}
	.workspace {
		flex: 1;
		display: flex;
		min-height: 0;
	}
	.flow-panel {
		flex: 1;
		min-width: 0;
		position: relative;
	}
	.load-error {
		padding: 24px;
		color: var(--text-secondary);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		margin: 24px;
	}
	.tidy-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		background: var(--bg-surface);
		color: var(--text-secondary);
		font-size: 12px;
		font-weight: 600;
		cursor: pointer;
		box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
	}
	.tidy-btn:hover:not(:disabled) {
		color: var(--text-primary);
		border-color: var(--accent);
	}
	.tidy-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.canvas-empty-hint {
		display: flex;
		flex-direction: column;
		gap: 4px;
		max-width: 340px;
		padding: 10px 14px;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
		font-size: 12px;
		color: var(--text-secondary);
	}
	.canvas-empty-hint strong {
		color: var(--text-primary);
		font-size: 12.5px;
	}
	.titlecard {
		position: absolute;
		top: 14px;
		left: 14px;
		z-index: 5;
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 12px;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
	}
	.title-btn {
		border: none;
		background: transparent;
		color: var(--text-primary);
		font-size: 14px;
		font-weight: 600;
		cursor: text;
		padding: 2px 4px;
		border-radius: var(--radius-sm);
		max-width: 280px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.title-btn:hover {
		background: var(--bg-elevated);
	}
	.title-input {
		border: 1px solid var(--accent);
		background: var(--bg-elevated);
		color: var(--text-primary);
		font-size: 14px;
		font-weight: 600;
		padding: 2px 6px;
		border-radius: var(--radius-sm);
		max-width: 280px;
		outline: none;
	}
	.saved {
		font-size: 10.5px;
		color: var(--text-muted);
		letter-spacing: 0.05em;
	}
	.project-chip,
	.sandbox-chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 11.5px;
		color: var(--text-secondary);
		text-decoration: none;
		padding: 3px 8px;
		border: 1px solid var(--border);
		border-radius: 999px;
		white-space: nowrap;
	}
	.project-chip:hover {
		border-color: var(--accent);
		color: var(--text-primary);
	}
	.unlink-btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		font-size: 11.5px;
		color: var(--text-secondary);
		padding: 3px 9px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: transparent;
		cursor: pointer;
		white-space: nowrap;
		transition:
			border-color 0.15s,
			color 0.15s;
	}
	.unlink-btn:hover:not(:disabled) {
		border-color: #f87171;
		color: #fca5a5;
	}
	.unlink-btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.chat-panel {
		flex-shrink: 0;
		border-left: 1px solid var(--border);
		background: var(--bg-surface);
		display: flex;
		flex-direction: column;
		min-height: 0;
		min-width: 0;
	}
	.chat-splitter {
		width: 7px;
		flex-shrink: 0;
		padding: 0;
		border: none;
		background: transparent;
		cursor: col-resize;
		margin-left: -3px;
		margin-right: -3px;
		z-index: 6;
		position: relative;
		transition: background 0.12s;
	}
	.chat-splitter:hover,
	.chat-splitter.dragging,
	.chat-splitter:focus-visible {
		background: color-mix(in srgb, var(--accent) 45%, transparent);
		outline: none;
	}
	.chat-splitter.dragging {
		/* Svelte Flow's pane captures pointer events while dragging near the
		   graph edge — suppress text selection app-wide during the drag. */
		cursor: col-resize;
	}
	.chat-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-sm);
		padding: 10px 14px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}
	.chat-title-block {
		min-width: 0;
	}
	.chat-title {
		font-size: 13px;
		font-weight: 600;
		color: var(--text-primary);
	}
	.chat-title.muted {
		color: var(--text-muted);
		font-weight: 400;
	}
	.chat-toggle,
	.chat-expand {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: 1px solid var(--border);
		background: var(--bg-surface);
		color: var(--text-secondary);
		border-radius: var(--radius-sm);
		cursor: pointer;
	}
	.chat-toggle {
		width: 26px;
		height: 26px;
	}
	.chat-toggle:hover,
	.chat-expand:hover {
		color: var(--text-primary);
		border-color: var(--accent);
	}
	.chat-expand {
		width: 34px;
		height: 34px;
		position: absolute;
		right: 14px;
		top: 14px;
		z-index: 5;
	}
</style>
