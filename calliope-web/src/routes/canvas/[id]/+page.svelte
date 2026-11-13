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
		type Edge,
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
	import WorkflowNode from '$lib/canvas/WorkflowNode.svelte';
	import type { ComposerReference } from '$lib/canvas/NodeComposer.svelte';
	import ArtifactNodeComp from '$lib/canvas/ArtifactNode.svelte';
	import { clipPresentation, currentClipPaths, storyEdges } from '$lib/canvas/storyEdges';
	import Icon from '$lib/components/ui/Icon.svelte';
	import { t } from '$lib/i18n.svelte';
	import { storageGet, storageSet } from '$lib/storage';
	import {
		agentApi,
		assetUrl,
		canvasApi,
		jobsApi,
		playgroundApi,
		projects,
		settings as settingsApi,
		workflows,
		type AgentMessage,
		type AgentPlan,
		type AgentSession,
		type AgentTask,
		type CanvasEdge,
		type Character,
		type Item,
		type Job,
		type Location,
		type Scene,
	} from '$lib/api';
	import type { AgentComposerPayload, SkillOption, WorkflowOption } from '$lib/agentComposer';
	import { AgentSessionRuntime } from '$lib/agent/agentSession.svelte';
	import { createCoalescedInvalidator } from '$lib/queryCoalesce';
	import { connectEvents } from '$lib/events';
	import { toast } from '$lib/toast';

	const client = useQueryClient();

	const canvasId = Number(page.params.id);
	if (!Number.isFinite(canvasId)) {
		throw new Error(t('canvas.invalidId'));
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
			// The scene card is the screenplay unit. Its video lives on the
			// clip cards; this card keeps the environment still.
			return { imagePath: s?.env_image_path ?? null, videoPath: null };
		}
		if (node.entity_type === 'clip') {
			for (const s of scenes) {
				const clip = (s.clips ?? []).find((c) => c.id === node.entity_id);
				if (!clip) continue;
				const video =
					clip.clip_path && /\.(mp4|webm)$/i.test(clip.clip_path) ? clip.clip_path : null;
				return { imagePath: null, videoPath: video };
			}
		}
		return { imagePath: null, videoPath: null };
	}

	const nodeTypes = {
		entity: EntityNode,
		image: ArtifactNodeComp,
		video: ArtifactNodeComp,
		workflow: WorkflowNode,
	};

	// $state.raw: Svelte Flow v1 treats node arrays as immutable — it mutates
	// internally, so proxies must never reach it.
	let nodes = $state.raw<Node[]>([]);
	let edges = $state.raw<Edge[]>([]);

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

	function longestText(values: Record<string, unknown> | null | undefined): string {
		if (!values) return '';
		const strings = Object.values(values).filter(
			(value): value is string => typeof value === 'string' && value.trim().length > 0,
		);
		strings.sort((a, b) => b.length - a.length);
		return strings[0] ?? '';
	}

	function mediaRefs(values: Record<string, unknown> | null | undefined) {
		const out: { kind: 'image' | 'audio' | 'video'; url: string; name: string }[] = [];
		if (!values) return out;
		const visit = (value: unknown) => {
			if (typeof value === 'string') {
				const lower = value.toLowerCase();
				const kind = /\.(mp4|webm|mov)$/.test(lower)
					? 'video'
					: /\.(mp3|wav|m4a|ogg|flac|aac)$/.test(lower)
						? 'audio'
						: /\.(png|jpe?g|webp|gif)$/.test(lower)
							? 'image'
							: null;
				const url = kind ? assetUrl(value) : null;
				if (kind && url) {
					out.push({ kind, url, name: value.split(/[/\\]/).pop() ?? value });
				}
				return;
			}
			if (Array.isArray(value)) value.forEach(visit);
			else if (value && typeof value === 'object') {
				Object.values(value as Record<string, unknown>).forEach(visit);
			}
		};
		Object.values(values).forEach(visit);
		return out;
	}

	function jobPrompt(job: Job | undefined): { prompt: string; workflowId: number | null } {
		const payload = job?.payload ?? {};
		const typed = typeof payload.prompt === 'string' ? payload.prompt.trim() : '';
		const values =
			payload.input_values && typeof payload.input_values === 'object'
				? (payload.input_values as Record<string, unknown>)
				: null;
		return {
			prompt: typed || longestText(values),
			workflowId: job?.workflow_id ?? null,
		};
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

	/** Stored canvas edges (data + link) as renderable xyflow edges, on top of
	 * the derived story edges. Edge ids carry the DB id for deletion. */
	function storedFlowEdges(rows: CanvasEdge[]): Edge[] {
		return rows.map((e) => ({
			id: `stored-${e.id}`,
			source: `cn-${e.src_node_id}`,
			target: `cn-${e.dst_node_id}`,
			sourceHandle: 'asset',
			targetHandle: 'in',
			type: 'smoothstep',
			label: e.kind === 'link' ? (e.label ?? 'link') : (e.dst_role ?? ''),
			data: { storedEdgeId: e.id, edgeKind: e.kind },
			deletable: true,
			selectable: true,
			style:
				e.kind === 'link'
					? 'stroke: #d4b46a; stroke-dasharray: 5 4; stroke-width: 1.5px'
					: 'stroke: #7dd3fc; stroke-dasharray: 4 3; stroke-width: 1.5px',
		}));
	}

	/** Run a workflow node — the manual queue path (not the agent review gate). */
	async function runWorkflowNode(nodeId: number) {
		try {
			await canvasApi.runNode(canvasId, nodeId);
			toast.success(t('canvas.runQueued'));
			await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
			await client.invalidateQueries({ queryKey: ['agent-project-jobs'] });
			await client.invalidateQueries({ queryKey: ['playground-jobs'] });
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.runFailed'));
		}
	}

	// ---- wiring (data + link edges) ------------------------------------------
	// Drag from a media card's asset handle onto a workflow node → the chooser
	// picks a data role (stored, then resolved onto the workflow's role slots
	// at run time) or a labeled link annotation.

	const EDGE_ROLES = ['prompt', 'character', 'location', 'image', 'negative', 'audio', 'video'] as const;
	let edgeChooser = $state<{
		source: number;
		target: number;
		sourceTitle: string;
		targetTitle: string;
	} | null>(null);
	let linkLabel = $state('');

	function flowNodeId(id: string): number | null {
		const n = Number(id.replace(/^cn-/, ''));
		return Number.isFinite(n) && n > 0 ? n : null;
	}

	function nodeTitle(id: number): string {
		const n = nodes.find((x) => (x.data as { canvasNodeId?: number }).canvasNodeId === id);
		const title = (n?.data as { title?: string } | undefined)?.title;
		return title ?? String(id);
	}

	function onFlowConnect(connection: { source: string | null; target: string | null }) {
		const src = connection.source != null ? flowNodeId(connection.source) : null;
		const dst = connection.target != null ? flowNodeId(connection.target) : null;
		if (src == null || dst == null) return;
		if (src === dst) {
			toast.error(t('canvas.selfLoopBlocked'));
			return;
		}
		edgeChooser = {
			source: src,
			target: dst,
			sourceTitle: nodeTitle(src),
			targetTitle: nodeTitle(dst),
		};
		linkLabel = '';
	}

	/** Only workflow nodes accept wires — media cards are always sources. */
	function validFlowConnection(connection: { target: string | null }) {
		const dst = connection.target != null ? flowNodeId(connection.target) : null;
		if (dst == null) return false;
		const dstNode = nodes.find((x) => (x.data as { canvasNodeId?: number }).canvasNodeId === dst);
		return dstNode?.type === 'workflow';
	}

	async function createDataEdge(role: string) {
		const c = edgeChooser;
		if (!c) return;
		try {
			await canvasApi.createEdge(canvasId, {
				src_node_id: c.source,
				dst_node_id: c.target,
				kind: 'data',
				dst_role: role,
			});
			edgeChooser = null;
			await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.edgeCreateFailed'));
		}
	}

	async function createLinkEdge() {
		const c = edgeChooser;
		const label = linkLabel.trim();
		if (!c || !label) return;
		try {
			await canvasApi.createEdge(canvasId, {
				src_node_id: c.source,
				dst_node_id: c.target,
				kind: 'link',
				label,
			});
			edgeChooser = null;
			await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.edgeCreateFailed'));
		}
	}

	// ---- ＋ workflow node ------------------------------------------------------

	let addWorkflowId = $state<string>('');
	let addBusy = $state(false);

	async function addWorkflowNode() {
		const wid = Number(addWorkflowId);
		if (!Number.isFinite(wid) || wid <= 0 || addBusy) return;
		addBusy = true;
		try {
			// Stack new nodes slightly so they never land pixel-identical.
			const offset = (nodes.length % 6) * 36;
			await canvasApi.createNode(canvasId, {
				type: 'workflow',
				workflow_id: wid,
				x: 120 + offset,
				y: 120 + offset,
			});
			addWorkflowId = '';
			await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.nodeCreateFailed'));
		} finally {
			addBusy = false;
		}
	}

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
			edges = [];
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
		const liveClipPaths = currentClipPaths(scenes);
		edges = [...storyEdges(graph.nodes, scenes), ...storedFlowEdges(graph.edges)];
		nodes = graph.nodes
			.filter((n) => {
				// The clip card already shows this file. An older artifact
				// with the same path would be a second copy of the current take.
				if (
					(n.type === 'image' || n.type === 'video') &&
					n.artifact_path &&
					liveClipPaths.has(n.artifact_path)
				) {
					return false;
				}
				return true;
			})
			.map((n) => {
				const media = mediaFor(n, characters, locations, items, scenes);
				const clipView =
					n.entity_type === 'clip' && n.entity_id != null
						? clipPresentation(scenes, n.entity_id)
						: null;
				if (n.type === 'workflow') {
					const workflowName =
						($workflowsQuery.data ?? []).find((w) => w.id === n.workflow_id)?.name ??
						n.title ??
						'Workflow';
					const roles = graph.edges
						.filter((e) => e.dst_node_id === n.id && e.kind === 'data')
						.map((e) => e.dst_role ?? '');
					return {
						id: `cn-${n.id}`,
						type: 'workflow' as const,
						position: { x: n.x, y: n.y },
						data: {
							canvasNodeId: n.id,
							title: workflowName,
							workflowId: n.workflow_id ?? 0,
							status: nodeRunning.get(n.id) ? 'running' : n.status,
							roles: roles.filter((r) => r.length > 0),
							busy: nodeRunning.get(n.id) === true,
							onRun: () => void runWorkflowNode(n.id),
						},
					};
				}
				if (n.type === 'image' || n.type === 'video') {
					const job =
						n.job_id != null ? jobs.find((row) => row.id === n.job_id) : undefined;
					const fromJob = jobPrompt(job);
					let stored: Record<string, unknown> = {};
					try {
						stored = JSON.parse(n.input_values_json || '{}') as Record<string, unknown>;
					} catch {
						stored = {};
					}
					const title = (n.title ?? 'Artifact').replace(/\s· #\d+$/, '');
					const prompt = fromJob.prompt || longestText(stored) || title;
					const workflowId = n.workflow_id ?? fromJob.workflowId;
					const payloadValues =
						job?.payload?.input_values && typeof job.payload.input_values === 'object'
							? (job.payload.input_values as Record<string, unknown>)
							: {};
					const references = mediaRefs({ ...stored, ...payloadValues });
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
							prompt,
							workflowId,
							references,
							onPreview: () =>
								openMediaPreview(n.type as 'image' | 'video', n.artifact_path, title),
							onRegenerate: (next: string) => regenerateCard(n.id, next),
							onRequestReview: (next: string) =>
								requestGenerationReview({
									nodeId: n.id,
									prompt: next,
									mediaUrl: n.artifact_path ? assetUrl(n.artifact_path) : null,
									references,
								}),
							onSendToAgent: (next: string) => setComposerDraft(next),
						},
					};
				}
				const caption = clipView
					? `${clipView.mention} · ${clipView.subtitle}`
					: (n.title ?? 'Untitled');
				const clip =
					n.entity_type === 'clip' && n.entity_id != null
						? scenes
								.flatMap((scene) => scene.clips ?? [])
								.find((row) => row.id === n.entity_id)
						: undefined;
				const clipPrompt =
					clip?.video_settings?.prompt_draft?.trim() ||
					clip?.description?.trim() ||
					'';
				const clipReferences = mediaRefs(
					(clip?.video_settings as { input_values?: Record<string, unknown> } | undefined)
						?.input_values,
				);
				return {
					id: `cn-${n.id}`,
					type: 'entity' as const,
					position: { x: n.x, y: n.y },
					data: {
						canvasNodeId: n.id,
						entityType: n.entity_type as
							| 'character'
							| 'location'
							| 'item'
							| 'scene'
							| 'clip',
						title: clipView?.subtitle ?? n.title ?? 'Untitled',
						mention: clipView?.mention ?? null,
						imagePath: media.imagePath,
						videoPath: media.videoPath,
						prompt: clipPrompt,
						workflowId: clip?.workflow_id ?? null,
						references: clipReferences,
						onOpenMedia: (kind: 'image' | 'video') =>
							kind === 'video'
								? openMediaPreview('video', media.videoPath, caption)
								: openMediaPreview('image', media.imagePath, caption),
						onPreview: () =>
							media.videoPath
								? openMediaPreview('video', media.videoPath, caption)
								: openMediaPreview('image', media.imagePath, caption),
						onRegenerate: (next: string) => regenerateCard(n.id, next),
						onRequestReview: (next: string) =>
							requestGenerationReview({
								nodeId: n.id,
								prompt: next,
								mediaUrl: assetUrl(media.videoPath) ?? assetUrl(media.imagePath),
								references: clipReferences,
							}),
						onSendToAgent: (next: string) => setComposerDraft(next),
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
			toast.error(err instanceof Error ? err.message : t('canvas.savePosFailed'));
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

	// ---- node deletion (Backspace/Delete on selection) ----------------------
	// Svelte Flow removes deleted nodes from the local store on its own; the
	// handlers below make the removal real (persist + file cleanup) or veto it.

	function nodeServerId(node: Node): number | null {
		const id = (node.data as { canvasNodeId?: number }).canvasNodeId;
		return typeof id === 'number' ? id : null;
	}

	async function onBeforeDeleteCanvas({ nodes }: { nodes: Node[] }): Promise<boolean> {
		const blocked = nodes.filter(
			(n) =>
				n.type === 'entity' ||
				(n.data as { status?: string }).status === 'running' ||
				(n.data as { status?: string }).status === 'queued',
		);
		if (blocked.length > 0) {
			toast.error(
				t('canvas.deleteBlocked', { count: blocked.length }),
			);
			return false;
		}
		return true;
	}

	async function onDeleteCanvas({ nodes, edges }: { nodes: Node[]; edges: Edge[] }) {
		// Selected wires (data/link) delete server-side; entity tombstones and
		// artifact cards keep their richer handling below.
		const edgeIds = edges
			.map((e) => (e.data as { storedEdgeId?: number } | undefined)?.storedEdgeId)
			.filter((id): id is number => id != null);
		if (edgeIds.length > 0) {
			const results = await Promise.allSettled(
				edgeIds.map((id) => canvasApi.deleteEdge(canvasId, id)),
			);
			await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
			const failed = results.filter((r) => r.status === 'rejected').length;
			if (failed > 0) toast.error(t('canvas.edgeDeleteFailed', { count: failed }));
			if (nodes.length === 0) return;
		}
		const ids = nodes
			.map(nodeServerId)
			.filter((id): id is number => id != null);
		if (ids.length === 0) return;
		let fileDeletedCount = 0;
		let keptCount = 0;
		const results = await Promise.allSettled(
			ids.map((id) => canvasApi.deleteNode(canvasId, id)),
		);
		for (const r of results) {
			if (r.status === 'fulfilled') {
				if (r.value.file_deleted) fileDeletedCount++;
				else keptCount++;
			}
		}
		await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
		const failed = results.filter((r) => r.status === 'rejected').length;
		if (failed > 0) {
			toast.error(t('canvas.deleteFailed', { count: failed }));
			return;
		}
		if (fileDeletedCount > 0 && keptCount > 0) {
			toast.success(t('canvas.deletedMixed', { files: fileDeletedCount, kept: keptCount }));
		} else if (fileDeletedCount > 0) {
			toast.success(t('canvas.deletedWithFile', { count: fileDeletedCount }));
		} else {
			toast.success(t('canvas.deletedCardOnly', { count: keptCount }));
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
		const raw = storageGet(viewportLocalStorageKey(canvasId));
		if (raw == null) return null;
		try {
			return JSON.parse(raw) as Viewport;
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
			storageSet(viewportLocalStorageKey(canvasId), json);
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

	// ---- sessions + chat ----------------------------------------------------

	let activeId = $state<number | null>(null);
	// Coalesced invalidation: a multi-tool turn or a job-event burst must not
	// rebuild the node graph over and over — one refetch per key per window.
	const invalidateLater = createCoalescedInvalidator(client);
	// The shared agent-chat live view (streaming buffers, tool cards, plan,
	// and the agent.* SSE handling). Canvas-specific events stay below.
	const agentRuntime = new AgentSessionRuntime({
		sessionsKey: () => ['agent-sessions'],
		activeId: () => activeId,
		onResetLive: () => {
			composerDraft = '';
			composerNonce = 0;
		},
		onToolFinish: (_name, result) => {
			// Script and cast edits land as tool results. Refetch the board so
			// new clip cards and story lines appear without a reload. A
			// create_project result also leaves the sandbox.
			invalidateLater(['canvas', canvasId]);
			invalidateLater(['canvas-scenes']);
			invalidateLater(['canvas-assets']);
			const linked = Number((result as { project_id?: number } | undefined)?.project_id);
			if (Number.isFinite(linked) && linked > 0 && activeId != null) {
				void followProject(linked, activeId);
			}
		},
	});
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
	const plan = $derived(agentRuntime.livePlan ?? ($sessionQuery.data?.plan ?? null));

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
	const settingsQuery = createQuery({
		queryKey: ['settings'],
		queryFn: settingsApi.get,
	});
	const routerRowLabel = $derived(
		$settingsQuery.data?.comfy_api_key && $settingsQuery.data?.comfy_router_enabled
			? t('router.minimaxH3')
			: null,
	);
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
		if (paramSession != null && Number.isFinite(pid) && pid > 0) {
			// Trust the deep-link param even when the (possibly cached) sessions
			// list doesn't contain it yet: /agents?project=&task= hands us a
			// session created milliseconds ago. Validating against a stale list
			// nulled activeId and orphaned the chat — the next Send then created
			// a DUPLICATE session (empty 905 vs working 906 signature). A truly
			// gone session just 404s in the session query, which is recoverable.
			activeId = pid;
			// Remember the last open session — the /canvas and /agents redirects
			// fall back to it so plain navigation lands where the user left off.
			storageSet(SESSION_KEY, String(pid));
			// Deep-link handoff (/agents?project=&task=) pre-fills the composer once.
			const prefill = sessionStorage.getItem('calliope.canvas.composerPrefill');
			if (prefill) {
				sessionStorage.removeItem('calliope.canvas.composerPrefill');
				setComposerDraft(prefill);
			}
			replaceUrlParam('session');
			return;
		}
		const stored = Number(storageGet(SESSION_KEY));
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

	// Once a sandbox chat is linked, leave this board for the project canvas.
	// The session list regroups from the same session payload; the project
	// canvas GET seeds the new cards. A reload is not required.
	let followedProjectId: number | null = null;

	async function followProject(projectId: number, sessionId: number) {
		if (canvas?.project_id === projectId || followedProjectId === projectId) return;
		followedProjectId = projectId;
		try {
			const graph = await canvasApi.ensureForProject(projectId);
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			if (graph.canvas.id === canvasId) {
				await client.invalidateQueries({ queryKey: ['canvas', canvasId] });
				await client.invalidateQueries({ queryKey: ['canvas-scenes'] });
				await client.invalidateQueries({ queryKey: ['canvas-assets'] });
				return;
			}
			goto(`/canvas/${graph.canvas.id}?session=${sessionId}`);
		} catch (err) {
			followedProjectId = null;
			toast.error(err instanceof Error ? err.message : t('canvas.openThatFailed'));
		}
	}

	$effect(() => {
		const pid = activeSession?.project_id ?? null;
		const sid = activeSession?.id ?? null;
		const canvasPid = canvas?.project_id ?? null;
		if (pid == null) return;
		if (sid == null || pid === canvasPid) return;
		void followProject(pid, sid);
	});

	// Selecting a session from another scope navigates to that scope's canvas.
	async function selectSession(id: number) {
		const s = sessions.find((x) => x.id === id);
		if (!s || !canvas) return;
		storageSet(SESSION_KEY, String(id));
		const sameScope =
			(s.project_id ?? null) === (canvas.project_id ?? null) ||
			(canvas.project_id != null && s.project_id === canvas.project_id);
		if (sameScope) {
			activeId = id;
			agentRuntime.resetLiveViews();
			return;
		}
		try {
			const graph = s.project_id
				? await canvasApi.ensureForProject(s.project_id)
				: await canvasApi.ensureForSession(s.id);
			goto(`/canvas/${graph.canvas.id}?session=${s.id}`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.openThatFailed'));
		}
	}

	let generationReview = $state<{
		nodeId: number;
		prompt: string;
		mediaUrl: string | null;
		references: ComposerReference[];
	} | null>(null);

	function requestGenerationReview(review: NonNullable<typeof generationReview>) {
		generationReview = review;
	}

	async function confirmGenerationReview() {
		const review = generationReview;
		if (!review || !review.prompt.trim()) return;
		await regenerateCard(review.nodeId, review.prompt.trim());
		generationReview = null;
	}

	async function regenerateCard(nodeId: number, prompt: string) {
		try {
			await canvasApi.regenerate(canvasId, nodeId, prompt);
			toast.success(t('canvas.regenQueued'));
			await client.invalidateQueries({ queryKey: ['agent-project-jobs'] });
			await client.invalidateQueries({ queryKey: ['playground-jobs'] });
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.regenFailed'));
			throw err;
		}
	}

	function setComposerDraft(text: string) {
		composerDraft = text;
		composerNonce++;
	}

	function seedEmptySession(s: AgentSession) {
		client.setQueryData(['agent-session', s.id], { ...s, messages: [], plan: null });
	}

	async function newSandbox() {
		agentRuntime.resetLiveViews();
		try {
			const s = await agentApi.createSession({});
			seedEmptySession(s);
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			const graph = await canvasApi.ensureForSession(s.id);
			goto(`/canvas/${graph.canvas.id}?session=${s.id}`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.sessionCreateFailed'));
		}
	}

	async function newProjectChat(pid: number) {
		agentRuntime.resetLiveViews();
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
			toast.error(err instanceof Error ? err.message : t('canvas.sessionCreateFailed'));
		}
	}

	async function removeSession(id: number) {
		if (!confirm(t('canvas.confirmDeleteChat'))) return;
		try {
			await agentApi.deleteSession(id);
			if (activeId === id) activeId = null;
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			toast.success(t('canvas.sessionDeleted'));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.sessionDeleteFailed'));
		}
	}

	// Manual sandbox link: the agent can link via create_project/link_project,
	// but a user picking a project from the composer side needs a direct path
	// too (the orphaned listLinkableProjects client method finally gets a caller).
	const linkableProjectsQuery = createQuery({
		queryKey: ['agent-linkable-projects'],
		queryFn: agentApi.listLinkableProjects,
		enabled: activeSession?.project_id == null,
	});
	let linkProjectId = $state('');

	async function linkActiveSession() {
		const pid = Number(linkProjectId);
		linkProjectId = '';
		if (!Number.isFinite(pid) || pid <= 0 || !activeSession || running) return;
		try {
			await agentApi.patchSession(activeSession.id, { project_id: pid });
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			await client.invalidateQueries({ queryKey: ['agent-session', activeSession.id] });
			// The followProject effect picks this up and moves to the project canvas.
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.linkFailed'));
		}
	}

	async function renameSession(id: number, title: string) {
		try {
			await agentApi.patchSession(id, { title });
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			if (activeId === id) {
				await client.invalidateQueries({ queryKey: ['agent-session', id] });
			}
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.sessionRenameFailed'));
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
			toast.success(t('canvas.chatUnlinked'));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.unlinkFailed'));
		}
	}

	const sendMutation = createMutation({
		mutationFn: (payload: AgentComposerPayload) => agentApi.postMessage(activeId!, payload),
		onMutate: () => {
			if (activeId != null) {
				const s = sessions.find((x) => x.id === activeId);
				const steering = Boolean(s?.running || s?.status === 'running');
				if (!steering) {
					agentRuntime.resetStream();
					client.setQueryData<AgentSession & { messages: AgentMessage[]; plan?: AgentPlan | null }>(
						['agent-session', activeId],
						(old) => (old ? { ...old, plan: null } : old),
					);
				}
			}
		},
		onSuccess: async () => {
			await client.invalidateQueries({ queryKey: ['agent-session', activeId] });
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('canvas.sendFailed'));
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
			toast.info(t('canvas.runCancelled'));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.cancelFailed'));
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
			toast.error(err instanceof Error ? err.message : t('canvas.sessionCreateFailed'));
		}
	}

	/** Question-card click: the option text is the reply, stamped with the
	 * question seq so the backend records a structured approval. */
	function handleQuestionAnswer(option: string, _scope: string, questionSeq: number) {
		if (running || activeId == null) return;
		handleSend({ content: option, mentions: [], attachments: [], answer_to: questionSeq });
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
				return;
			} else if (agentRuntime.handleAgentEvent(ev)) {
				return;
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
		// A project clip render already has a card. Posting the file again
		// would leave an unconnected duplicate beside it.
		const clipId = Number(data?.clip_id);
		if (Number.isFinite(clipId) && clipId > 0) return;
		// Scope: project canvases take their own project's jobs; sandbox
		// canvases take jobs the agent enqueued (source:"agent" stamped by
		// the worker on the event — no race with the jobs poll). Other
		// sandbox tabs' scratch jobs cannot cross-post.
		const evPid = data?.project_id == null ? null : Number(data.project_id);
		const evCanvas = Number(data?.canvas_id);
		if (Number.isFinite(evCanvas)) {
			if (evCanvas !== canvasId) return;
		} else if (canvas?.project_id != null) {
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
			toast.success(t('canvas.tidied', { count: res.moved }));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('canvas.tidyFailed'));
		} finally {
			tidying = false;
		}
	}

	let railCollapsed = $state(storageGet(RAIL_KEY) === '1');
	let chatCollapsed = $state(storageGet(CHAT_KEY) === '1');

	function toggleRail() {
		railCollapsed = !railCollapsed;
		storageSet(RAIL_KEY, railCollapsed ? '1' : '0');
	}
	function toggleChat() {
		chatCollapsed = !chatCollapsed;
		storageSet(CHAT_KEY, chatCollapsed ? '1' : '0');
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
		const raw = Number(storageGet(CHAT_W_KEY));
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
			storageSet(CHAT_W_KEY, String(Math.round(chatWidth)));
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
			toast.error(err instanceof Error ? err.message : t('canvas.renameFailed'));
		}
	}

	const SUGGESTIONS: { label: string; prompt: string }[] = [
		{
			label: t('canvas.suggestFilm'),
			prompt: 'Create a new film project and guide me through drafting the story.',
		},
		{
			label: t('canvas.suggestStory'),
			prompt: 'Draft a storyline — beats, characters, environments, and misc. items.',
		},
	];
</script>

<div class="canvas-root shell">
	<AppHeader active="canvas" />

	{#if $canvasQuery.isError}
		<div class="load-error" role="alert">
			{t('canvas.loadFailed')}
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
				onRename={renameSession}
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
							title={t('canvas.rename')}
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
			? t('canvas.waitFinish')
			: activeSession
			? t('canvas.unlinkFrom', { title: canvas.project.title })
			: t('canvas.noChatYet')}
						aria-label={t('canvas.unlinkFrom', { title: canvas.project.title })}
					>
						<Icon name="link-off" size={12} />
						{t('canvas.unlinkButton')}
					</button>
				{:else}
					<span class="sandbox-chip">
						<Icon name="sparkle" size={12} />
						Sandbox
					</span>
					{#if activeSession && !running}
						<select
							class="link-project"
							bind:value={linkProjectId}
							onchange={() => void linkActiveSession()}
							aria-label={t('canvas.linkToProject')}
						>
							<option value="">{t('canvas.linkToProject')}</option>
							{#each $linkableProjectsQuery.data ?? [] as p (p.id)}
								<option value={String(p.id)}>{p.title}</option>
							{/each}
						</select>
					{/if}
				{/if}
				</div>

				<SvelteFlow
					bind:nodes
					bind:edges
					bind:viewport
					{nodeTypes}
					nodesConnectable={true}
					isValidConnection={validFlowConnection}
					onconnect={onFlowConnect}
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
					onbeforedelete={onBeforeDeleteCanvas}
					ondelete={onDeleteCanvas}
					onmoveend={(_event, vp) => scheduleViewportSave(vp)}
					oninit={assertViewportOnInit}
				>
					<Background variant={BackgroundVariant.Dots} gap={26} size={1} />
					<Controls />
					<MiniMap />
					<Panel position="bottom-left">
						<div class="add-workflow">
							<select bind:value={addWorkflowId} aria-label={t('canvas.addWorkflow')}>
								<option value="">{t('canvas.addWorkflow')}</option>
								{#each enabledWorkflows as w (w.id)}
									<option value={String(w.id)}>{w.name}</option>
								{/each}
							</select>
							<button
								type="button"
								class="add-btn"
								disabled={addBusy || !addWorkflowId}
								onclick={addWorkflowNode}
								title={t('canvas.addWorkflowTitle')}
							>
								＋
							</button>
						</div>
					</Panel>
					<Panel position="bottom-center">
						<button
							type="button"
							class="tidy-btn"
							disabled={tidying || nodes.length === 0}
							onclick={tidyCanvas}
							title={t('canvas.tidyTitle')}
						>
							<Icon name="drag" size={14} />
							{t('canvas.tidy')}
						</button>
					</Panel>
					{#if nodes.length === 0 && !$canvasQuery.isLoading}
						<Panel position="top-center">
							<div class="canvas-empty-hint">
								<strong>{t('canvas.emptyBoard')}</strong>
								<span>{t('canvas.emptyHint')}</span>
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
					aria-label={t('canvas.resizePanel', { width: Math.round(chatWidth) })}
					onmousedown={startDrag}
					onkeydown={(e) => {
						if (e.key === 'ArrowLeft') {
							e.preventDefault();
							chatWidth = clampChatWidth(chatWidth + 24);
							storageSet(CHAT_W_KEY, String(Math.round(chatWidth)));
						} else if (e.key === 'ArrowRight') {
							e.preventDefault();
							chatWidth = clampChatWidth(chatWidth - 24);
							storageSet(CHAT_W_KEY, String(Math.round(chatWidth)));
						}
					}}
				></button>
				<aside class="chat-panel" style:width="{chatWidth}px">
					<header class="chat-head">
						<div class="chat-title-block">
							{#if activeSession}
								<span class="chat-title">{activeSession.title}</span>
							{:else}
								<span class="chat-title muted">{t('canvas.noChatSelected')}</span>
							{/if}
						</div>
						<button
							type="button"
							class="chat-toggle"
							onclick={toggleChat}
							title={t('canvas.collapseChat')}
							aria-label={t('canvas.collapseChat')}
						>
							<Icon name="chevron-right" size={16} />
						</button>
					</header>
					<AgentChat
						{messages}
						liveTools={agentRuntime.liveTools}
						streaming={agentRuntime.streaming}
						streamingReasoning={agentRuntime.streamingReasoning}
						{jobs}
						{running}
						loading={activeId != null && $sessionQuery.isLoading}
						suggestions={SUGGESTIONS}
						onSuggestion={setComposerDraft}
						onAnswer={handleQuestionAnswer}
						sessionId={activeId}
						onReviewed={async (summary) => {
							toast.success(summary);
							await client.invalidateQueries({ queryKey: ['agent-project-jobs'] });
							await client.invalidateQueries({ queryKey: ['playground-jobs'] });
							if (activeId != null) {
								await client.invalidateQueries({ queryKey: ['agent-session', activeId] });
							}
						}}
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
						routerOption={routerRowLabel}
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
					title={t('canvas.expandChat')}
					aria-label={t('canvas.expandChat')}
				>
					<Icon name="sparkle" size={14} />
					<span>{t('canvas.agentButton')}</span>
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

	{#if generationReview}
		<div class="generation-review" role="dialog" aria-label={t('agentChat.reviewHint')}>
			<p class="generation-title">{t('agentChat.reviewHint')}</p>
			<textarea
				class="generation-prompt"
				rows="10"
				aria-label={t('canvas.prompt')}
				value={generationReview.prompt}
				oninput={(event) => {
					if (!generationReview) return;
					generationReview = {
						...generationReview,
						prompt: (event.currentTarget as HTMLTextAreaElement).value,
					};
				}}
			></textarea>
			<div class="generation-media">
				{#if generationReview.mediaUrl}
					{#if generationReview.mediaUrl.match(/\.(mp4|webm|mov)(\?|#|$)/i)}
						<!-- svelte-ignore a11y_media_has_caption -->
						<video controls src={generationReview.mediaUrl}></video>
					{:else}
						<img src={generationReview.mediaUrl} alt="" />
					{/if}
				{/if}
				{#each generationReview.references as ref (`${ref.kind}-${ref.url}`)}
					{#if ref.kind === 'audio'}
						<audio controls src={ref.url}></audio>
					{:else if ref.kind === 'video'}
						<!-- svelte-ignore a11y_media_has_caption -->
						<video controls src={ref.url}></video>
					{:else}
						<img src={ref.url} alt={ref.name ?? ''} />
					{/if}
				{/each}
			</div>
			<div class="generation-actions">
				<button type="button" class="generation-go" onclick={confirmGenerationReview}>
					{t('agentChat.generateNow')}
				</button>
				<button type="button" onclick={() => (generationReview = null)}>
					{t('agentChat.revise')}
				</button>
			</div>
		</div>
	{/if}

	{#if edgeChooser}
		<div class="edge-chooser" role="dialog" aria-label={t('canvas.edgeChooserTitle')}>
			<p class="generation-title">{t('canvas.edgeChooserTitle')}</p>
			<p class="edge-src">{edgeChooser.sourceTitle} → {edgeChooser.targetTitle}</p>
			<p class="edge-kind-label">{t('canvas.edgeDataRole')}</p>
			<div class="edge-roles">
				{#each EDGE_ROLES as role (role)}
					<button type="button" class="role-pick" onclick={() => void createDataEdge(role)}>
						{role}
					</button>
				{/each}
			</div>
			<p class="edge-kind-label">{t('canvas.edgeLinkLabel')}</p>
			<input
				class="edge-label-input"
				type="text"
				maxlength="80"
				placeholder={t('canvas.edgeLinkPlaceholder')}
				bind:value={linkLabel}
				onkeydown={(e) => {
					if (e.key === 'Enter') void createLinkEdge();
				}}
			/>
			<button
				type="button"
				class="link-go"
				disabled={!linkLabel.trim()}
				onclick={() => void createLinkEdge()}
			>
				{t('canvas.edgeLinkCreate')}
			</button>
			<button type="button" class="edge-cancel" onclick={() => (edgeChooser = null)}>
				{t('common.cancel')}
			</button>
		</div>
	{/if}
</div>

<style>
	.generation-review {
		position: fixed;
		z-index: 40;
		top: 72px;
		right: 24px;
		width: min(560px, calc(100vw - 40px));
		max-height: calc(100vh - 96px);
		overflow: auto;
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 16px;
		background: var(--bg-elevated);
		border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
		border-radius: var(--radius-md);
		box-shadow: 0 18px 50px rgba(0, 0, 0, 0.45);
	}
	.generation-title {
		margin: 0;
		font-size: 13px;
		font-weight: 650;
	}
	.generation-prompt {
		width: 100%;
		box-sizing: border-box;
		resize: vertical;
		min-height: 160px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg-surface);
		color: var(--text-primary);
		font: inherit;
		font-size: 13px;
		line-height: 1.45;
		padding: 8px 10px;
	}
	.generation-media {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.generation-media img,
	.generation-media video,
	.generation-media audio {
		width: 100%;
		max-height: 240px;
		border-radius: 8px;
		background: black;
		object-fit: contain;
	}
	.generation-actions {
		display: flex;
		gap: 8px;
	}
	.generation-actions button {
		border: 1px solid var(--border);
		border-radius: 999px;
		background: transparent;
		color: var(--text-primary);
		font: inherit;
		font-size: 13px;
		font-weight: 650;
		padding: 6px 14px;
		cursor: pointer;
	}
	.generation-go {
		background: var(--accent) !important;
		border-color: var(--accent) !important;
		color: white !important;
	}
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
		/* Anchor for the collapsed-chat button — without it the button's
		   absolute positioning resolves against the viewport and lands on
		   top of the AppHeader's language dropdown. */
		position: relative;
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
		gap: 6px;
		border: 1px solid var(--border);
		background: var(--bg-elevated);
		color: var(--text-secondary);
		border-radius: 999px;
		cursor: pointer;
		font-family: inherit;
		flex-shrink: 0;
	}
	.chat-toggle {
		width: 30px;
		height: 30px;
	}
	.chat-toggle:hover,
	.chat-expand:hover {
		color: var(--text-primary);
		border-color: var(--accent);
	}
	.chat-toggle:focus-visible,
	.chat-expand:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.chat-expand {
		position: absolute;
		right: 14px;
		top: 14px;
		z-index: 5;
		padding: 8px 14px;
		font-size: 12px;
		font-weight: 600;
		color: var(--text-primary);
		box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
	}
	.chat-expand:hover {
		color: var(--accent);
	}

	.add-workflow {
		display: flex;
		gap: 6px;
		align-items: center;
	}
	.add-workflow select {
		max-width: 220px;
		font-size: 11.5px;
		color: var(--text-primary);
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 5px 8px;
	}
	.add-workflow .add-btn {
		font-size: 14px;
		line-height: 1;
		color: #eaf6ff;
		background: color-mix(in srgb, var(--accent) 26%, transparent);
		border: 1px solid color-mix(in srgb, var(--accent) 55%, transparent);
		border-radius: 8px;
		padding: 5px 10px;
		cursor: pointer;
	}
	.add-workflow .add-btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.edge-chooser {
		position: fixed;
		z-index: 42;
		top: 72px;
		left: 24px;
		width: min(420px, calc(100vw - 40px));
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 16px;
		background: var(--bg-elevated);
		border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
		border-radius: 14px;
		box-shadow: 0 18px 50px rgba(0, 0, 0, 0.45);
	}
	.edge-chooser .edge-src {
		margin: 0;
		font-size: 12px;
		color: var(--text-secondary);
	}
	.edge-kind-label {
		margin: 2px 0 0;
		font-size: 10px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.edge-roles {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.role-pick {
		font-size: 11.5px;
		color: #eaf6ff;
		background: color-mix(in srgb, var(--accent) 22%, transparent);
		border: 1px solid color-mix(in srgb, var(--accent) 50%, transparent);
		border-radius: 999px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.role-pick:hover {
		background: color-mix(in srgb, var(--accent) 36%, transparent);
	}
	.edge-label-input {
		font-size: 12px;
		color: var(--text-primary);
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 6px 10px;
	}
	.link-go {
		font-size: 12px;
		font-weight: 600;
		color: #f4ecd8;
		background: rgba(212, 180, 106, 0.25);
		border: 1px solid rgba(212, 180, 106, 0.5);
		border-radius: 8px;
		padding: 6px 0;
		cursor: pointer;
	}
	.link-go:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.edge-cancel {
		font-size: 12px;
		color: var(--text-secondary);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 5px 0;
		cursor: pointer;
	}
	.link-project {
		max-width: 180px;
		font-size: 11px;
		color: var(--text-secondary);
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 3px 8px;
	}
</style>
